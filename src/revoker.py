import logging
import os

import boto3

import sso
import organizations
import config
from dynamodb import log_operation_to_dynamodb
import slack

logging.basicConfig()
logger = logging.getLogger(__name__)
log_level = os.environ.get("LOG_LEVEL", "INFO")
logger.setLevel(logging.getLevelName(log_level))


def lambda_handler(_, __):
    cfg = config.Config()  # type: ignore
    client = boto3.client("sso-admin")  # type: ignore
    sso_instance_arn = sso.get_sso_instance_arn(client, cfg)
    logger.debug(f"selected SSO instance: {sso_instance_arn}")

    for account in cfg.lookup("accounts"):
        logger.info(f'Revoking tmp permissions for account {account["id"]}')
        for permision_set in cfg.lookup("permission_sets"):
            account_assignments = sso.list_account_assignments(
                client=client,
                instance_arn=sso_instance_arn,
                account_id=account["id"],
                permission_set_arn=permision_set["arn"],
            )
            for account_assigment in account_assignments:
                if account_assigment.principal_type == "GROUP":
                    continue

                slack_id = cfg.lookup("users", "sso_id", account_assigment.principal_id, "slack_id")
                handle_account_assignment_deletion(
                    slack_id=slack_id,
                    account_assigment=sso.UserAccountAssignment(
                        account_id=account["id"],
                        permission_set_arn=account_assigment.permission_set_arn,
                        user_principal_id=account_assigment.principal_id,
                        instance_arn=sso_instance_arn,
                    ),
                )


def handle_account_assignment_deletion(
    account_assigment: sso.UserAccountAssignment,
    slack_id: str,
):
    logger.info(f"Got account assignment for deletion: {account_assigment}")

    sso_client = boto3.client("sso-admin")  # type: ignore
    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assigment,
    )

    org_client = boto3.client("organizations")  # type: ignore
    account = organizations.describe_account(org_client, account_assigment.account_id)

    permission_set = sso.describe_permission_set(
        sso_client,
        account_assigment.instance_arn,
        account_assigment.permission_set_arn,
    )

    cfg = config.Config()  # type: ignore
    response = log_operation_to_dynamodb(
        logger,
        cfg.dynamodb_table_name,
        audit_entry={
            "role_name": permission_set.name,
            "account_id": account_assigment.account_id,
            "reason": "automated revocation",
            "requester_slack_id": "NA",
            "requester_email": "NA",
            "request_id": assignment_status.request_id,
            "approver_slack_id": "NA",
            "approver_email": "NA",
            "operation_type": "revoke",
        },
    )
    logger.debug(response)

    if cfg.post_update_to_slack:
        slack_cfg = config.SlackConfig()  # type: ignore
        slack.post_message(
            api_path="/api/chat.postMessage",
            message={
                "channel": slack_cfg.channel_id,
                "text": f"Revoked role {permission_set.name} for user <@{slack_id}> in account {account.name}",
            },
            token=slack_cfg.bot_token,
        )

