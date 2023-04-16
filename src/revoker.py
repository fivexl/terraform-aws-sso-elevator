import os
from dataclasses import dataclass

import boto3
import slack_sdk
from aws_lambda_powertools import Logger

import config
import dynamodb
import entities
import organizations
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

    configured_accounts = cfg.accounts
    if "*" in configured_accounts:
        accounts = organizations.list_accounts(org_client)
    else:
        accounts = [ac for ac in organizations.list_accounts(org_client) if ac.id in configured_accounts]

    configured_permission_sets = cfg.permission_sets
    if "*" in configured_permission_sets:
        permission_sets = sso.list_permission_sets(sso_client, cfg.sso_instance_arn)
    else:
        permission_sets = [ps for ps in sso.list_permission_sets(sso_client, cfg.sso_instance_arn) if ps.name in configured_permission_sets]

    for account in accounts:
        logger.info(f"Revoking tmp permissions for account {account.id}")
        for permision_set in permission_sets:
            account_assignments = sso.list_account_assignments(
                sso_client=sso_client,
                instance_arn=sso_instance.arn,
                account_id=account.id,
                permission_set_arn=permision_set.arn,
            )
            for account_assignment in account_assignments:
                if account_assignment.principal_type == "GROUP":
                    continue

                handle_account_assignment_deletion(
                    account_assignment=sso.UserAccountAssignment(
                        account_id=account.id,
                        permission_set_arn=account_assignment.permission_set_arn,
                        user_principal_id=account_assignment.principal_id,
                        instance_arn=sso_instance.arn,
                    ),
                    cfg=cfg,
                )


def handle_account_assignment_deletion(account_assignment: sso.UserAccountAssignment, cfg: config.Config):
    logger.info(f"Got account assignment for deletion: {account_assignment}")

    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )

    permission_set = sso.describe_permission_set(
        sso_client,
        account_assignment.instance_arn,
        account_assignment.permission_set_arn,
    )

    response = dynamodb.log_operation(
        logger,
        cfg.dynamodb_table_name,
        dynamodb.AuditEntry(
            role_name=permission_set.name,
            account_id=account_assignment.account_id,
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
        account = organizations.describe_account(org_client, account_assignment.account_id)
        slack_notify_user_on_revoke(cfg, account_assignment, permission_set, account)


def slack_notify_user_on_revoke(
    cfg, account_assignment: sso.UserAccountAssignment, permission_set: entities.aws.PermissionSet, account: entities.aws.Account
):
    slack_client = slack_sdk.WebClient(token=cfg.slack_bot_token)
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)

    aws_user_emails = sso.get_user_emails(
        identity_center_client,
        sso_instance.identity_store_id,
        account_assignment.user_principal_id,
    )
    user_slack_id = None

    for email in aws_user_emails:
        try:
            slack_user = slack.get_user_by_email(slack_client, email)
            user_slack_id = slack_user.id
        except Exception:
            continue

    mention = f"<@{user_slack_id}>" if user_slack_id is not None else aws_user_emails[0]
    slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=f"Revoked role {permission_set.name} for user {mention} in account {account.name}",
    )


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

    schedule_client.delete_schedule(Name=event.schedule_name)

    if cfg.post_update_to_slack:
        account = organizations.describe_account(org_client, account_assignment.account_id)
        slack_notify_user_on_revoke(cfg, account_assignment, permission_set, account)


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
