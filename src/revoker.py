import os
from dataclasses import dataclass

import boto3
from aws_lambda_powertools import Logger

import config
import dynamodb
import organizations
import schedule
import slack
import sso

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

org_client = boto3.client("organizations")  # type: ignore
sso_client = boto3.client("sso-admin")  # type: ignore
identity_center_client = boto3.client("identitystore")  # type: ignore


def lambda_handler(event, __):
    cfg = config.Config()  # type: ignore
    if "Scheduled_revoke" in event:
        return handle_scheduled_account_assignment_deletion(event, sso_client, cfg)
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    avialable_accounts = config.get_accounts_from_statements(cfg.statements, org_client)
    avialable_permission_sets = config.get_permission_sets_from_statements(statements, sso_client, sso_instance.arn)
    for account in avialable_accounts:
        logger.info(f"Revoking tmp permissions for account {account.id}")
        for permision_set in avialable_permission_sets:
            account_assignments = sso.list_account_assignments(
                client=sso_client,
                instance_arn=sso_instance.arn,
                account_id=account.id,
                permission_set_arn=permision_set.arn,
            )
            for account_assigment in account_assignments:
                if account_assigment.principal_type == "GROUP":
                    continue

                handle_account_assignment_deletion(
                    account_assigment=sso.UserAccountAssignment(
                        account_id=account.id,
                        permission_set_arn=account_assigment.permission_set_arn,
                        user_principal_id=account_assigment.principal_id,
                        instance_arn=sso_instance.arn,
                    ),
                    cfg=cfg,
                )


def handle_account_assignment_deletion(account_assigment: sso.UserAccountAssignment, cfg: config.Config):
    logger.info(f"Got account assignment for deletion: {account_assigment}")

    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assigment,
    )
    account = organizations.describe_account(org_client, account_assigment.account_id)

    permission_set = sso.describe_permission_set(
        sso_client,
        account_assigment.instance_arn,
        account_assigment.permission_set_arn,
    )

    response = dynamodb.log_operation(
        logger,
        cfg.dynamodb_table_name,
        dynamodb.AuditEntry(
            role_name=permission_set.name,
            account_id=account_assigment.account_id,
            reason="automated revocation",
            requester_slack_id="NA",
            requester_email="NA",
            request_id=assignment_status.request_id,
            approver_slack_id="NA",
            approver_email="NA",
            operation_type="revoke",
        ),
    )
    logger.debug(response)

    if cfg.post_update_to_slack:
        slack_cfg = config.SlackConfig()  # type: ignore
        slack_client = slack.Slack(slack_cfg.bot_token, slack_cfg.channel_id)

        slack_client.get_user_by_id(account_assigment.user_principal_id)
        user_emails = sso.get_user_emails(
            identity_center_client,
            account_assigment.instance_arn,
            account_assigment.user_principal_id,
        )
        slack_client.post_message(text=f"Revoked role {permission_set.name} for user {user_emails} in account {account.name}")


def handle_scheduled_account_assignment_deletion(event, sso_client, cfg: config.Config):
    event = EventBrigeRevokeEvent.from_scheduler_event(event)
    account_assignment = sso.UserAccountAssignment(
        account_id=event.account_id,
        permission_set_arn=event.permission_set_arn,
        user_principal_id=event.user_principal_id,
        instance_arn=event.instance_arn,
    )
    logger.info(f"Got account assignment for deletion: {account_assignment}")
    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )
    permission_set = sso.describe_permission_set(
        sso_client,
        sso_instance_arn=event.instance_arn,
        permission_set_arn=event.permission_set_arn,
    )

    dynamodb.log_operation(
        logger,
        cfg.dynamodb_table_name,
        dynamodb.AuditEntry(
            role_name=permission_set.name,
            account_id=event.account_id,
            reason="scheduled_revocation",
            requester_slack_id=event.requester_slack_id,
            requester_email=event.requester_email,
            request_id=assignment_status.request_id,
            approver_slack_id=event.approver_slack_id,
            approver_email=event.approver_email,
            operation_type="revoke",
        ),
    )
    schedule_client = boto3.client("scheduler")  # type: ignore
    schedule.delete_schedule(
        schedule_name=event.schedule_name,
        schedule_client=schedule_client,
    )
    if cfg.post_update_to_slack:
        slack_cfg = config.SlackConfig()  # type: ignore
        slack_client = slack.Slack(slack_cfg.bot_token, slack_cfg.channel_id)

        slack_client.get_user_by_id(event.user_principal_id)
        user_emails = sso.get_user_emails(
            identity_center_client,
            event.instance_arn,
            event.user_principal_id,
        )
        account_name = organizations.describe_account(org_client, event.account_id).name
        slack_client.post_message(text=f"Revoked role {permission_set.name} for user {user_emails} in account {account_name}")


@dataclass(frozen=True)
class EventBrigeRevokeEvent:
    schedule_name: str
    scheduleExpression: str
    instance_arn: str
    account_id: str
    permission_set_arn: str
    user_principal_id: str
    requester_slack_id: str
    requester_email: str
    approver_slack_id: str
    approver_email: str

    @staticmethod
    def from_scheduler_event(body: dict) -> "EventBrigeRevokeEvent":
        return EventBrigeRevokeEvent(
            schedule_name=body["Schedule_name"],
            scheduleExpression=body["ScheduleExpression"],
            instance_arn=body["Scheduled_revoke"]["instance_arn"],
            account_id=body["Scheduled_revoke"]["account_id"],
            permission_set_arn=body["Scheduled_revoke"]["permission_set_arn"],
            user_principal_id=body["Scheduled_revoke"]["user_principal_id"],
            requester_slack_id=body["Scheduled_revoke"]["requester_slack_id"],
            requester_email=body["Scheduled_revoke"]["requester_email"],
            approver_slack_id=body["Scheduled_revoke"]["approver_slack_id"],
            approver_email=body["Scheduled_revoke"]["approver_email"],
        )
