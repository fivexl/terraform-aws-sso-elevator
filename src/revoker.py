import os

import boto3
import slack_sdk
from aws_lambda_powertools import Logger

import config
import dynamodb
import entities
import organizations
import slack
import sso
import schedule

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

org_client = boto3.client("organizations")  # type: ignore
sso_client = boto3.client("sso-admin")  # type: ignore
identitystore_client = boto3.client("identitystore")  # type: ignore


def lambda_handler(event, __):
    cfg = config.Config()  # type: ignore
    logger.info(f"Got event: {event}")
    if revoke_event := schedule.RevokeEvent.parse_obj(event):
        return handle_scheduled_account_assignment_deletion(revoke_event, sso_client, cfg)
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

    accounts_ids = [ac.id for ac in accounts]
    permission_sets_arns = [ps.arn for ps in permission_sets]
    for account_assignment in sso.list_user_account_assignments(sso_client, sso_instance.arn, accounts_ids, permission_sets_arns):
        handle_account_assignment_deletion(
            account_assignment=sso.UserAccountAssignment(
                account_id=account_assignment.account_id,
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
        identitystore_client,
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


def handle_scheduled_account_assignment_deletion(revoke_event: schedule.RevokeEvent, sso_client, cfg: config.Config):
    account_assignment = revoke_event.user_account_assignment
    logger.info(f"Got account assignment for deletion: {account_assignment}")
    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )
    permission_set = sso.describe_permission_set(
        sso_client,
        sso_instance_arn=account_assignment.instance_arn,
        permission_set_arn=account_assignment.permission_set_arn,
    )

    dynamodb.log_operation(
        logger,
        cfg.dynamodb_table_name,
        dynamodb.AuditEntry(
            role_name=permission_set.name,
            account_id=account_assignment.account_id,
            reason="scheduled_revocation",
            requester_slack_id=revoke_event.requester.id,
            requester_email=revoke_event.requester.email,
            request_id=assignment_status.request_id,
            approver_slack_id=revoke_event.approver.id,
            approver_email=revoke_event.approver.email,
            operation_type="revoke",
        ),
    )
    schedule_client = boto3.client("scheduler")  # type: ignore

    schedule_client.delete_schedule(Name=revoke_event.schedule_name)

    if cfg.post_update_to_slack:
        account = organizations.describe_account(org_client, account_assignment.account_id)
        slack_notify_user_on_revoke(cfg, account_assignment, permission_set, account)

