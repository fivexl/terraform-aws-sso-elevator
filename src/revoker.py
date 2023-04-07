import os

import boto3
from aws_lambda_powertools import Logger

import config
import dynamodb
import organizations
import slack
import sso

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

org_client = boto3.client("organizations")  # type: ignore
sso_client = boto3.client("sso-admin")  # type: ignore
identity_center_client = boto3.client("identitystore")  # type: ignore


def lambda_handler(_, __):
    cfg = config.Config()  # type: ignore
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    statements = cfg.get_statements()
    avialable_accounts = config.get_accounts_from_statements(statements, org_client)
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
                )


def handle_account_assignment_deletion(account_assigment: sso.UserAccountAssignment):
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

    cfg = config.Config()  # type: ignore
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
