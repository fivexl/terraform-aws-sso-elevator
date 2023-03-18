import json
import logging
import os

import base
import boto3
from config import config_lookup
from dynamodb import log_operation_to_dynamodb
from slack_helpers import post_slack_message
from sso import delete_account_assigment, list_sso_instances

logging.basicConfig()
logger = logging.getLogger(__name__)
log_level = os.environ.get("LOG_LEVEL", "INFO")
logger.setLevel(logging.getLevelName(log_level))


def lambda_handler(event, context):
    # parameters

    DYNAMODB_TABLE_NAME = base.read_env_variable_or_die("DYNAMODB_TABLE_NAME")

    POST_UPDATE_TO_SLACK = os.environ.get("POST_UPDATE_TO_SLACK", "")
    POST_UPDATE_TO_SLACK = True if POST_UPDATE_TO_SLACK != "" else False
    SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
    if POST_UPDATE_TO_SLACK and SLACK_CHANNEL_ID == "":
        error = f"POST_UPDATE_TO_SLACK is set and thus SLACK_CHANNEL_ID is required but it is empty"
        logger.error(error)
        raise Exception(error)

    TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    if POST_UPDATE_TO_SLACK and TOKEN == "":
        error = (
            f"POST_UPDATE_TO_SLACK is set and thus TOKEN is required but it is empty"
        )
        logger.error(error)
        raise Exception(error)

    client = boto3.client("sso-admin")
    sso_instances = list_sso_instances(client, logger)
    sso_instance_arn = sso_instances["Instances"][0]["InstanceArn"]
    logger.debug(f"selected SSO instance: {sso_instance_arn}")

    for account in config_lookup("accounts"):
        logger.info(f'Revoking tmp permissions for account {account["id"]}')
        for permision_set in config_lookup("permission_sets"):
            response = client.list_account_assignments(
                InstanceArn=sso_instance_arn,
                AccountId=account["id"],
                PermissionSetArn=permision_set["arn"],
            )
            logger.debug(response)
            for account_assigment in response["AccountAssignments"]:
                logger.debug(f"handle: {account_assigment}")
                if account_assigment["PrincipalType"] == "GROUP":
                    # skip groups
                    continue
                role_name = config_lookup(
                    "permission_sets",
                    "arn",
                    account_assigment["PermissionSetArn"],
                    "name",
                )
                email = config_lookup(
                    "users", "sso_id", account_assigment["PrincipalId"], "email"
                )
                slack_id = config_lookup(
                    "users", "sso_id", account_assigment["PrincipalId"], "slack_id"
                )
                logger.info(f"revoking role {role_name} for user {email}")
                request_id = delete_account_assigment(
                    logger,
                    client,
                    sso_instance_arn,
                    account["id"],
                    account_assigment["PermissionSetArn"],
                    account_assigment["PrincipalId"],
                )

                audit_entry = {
                    "role_name": role_name,
                    "account_id": account["id"],
                    "reason": "automated revocation",
                    "requester_slack_id": "NA",
                    "requester_email": "NA",
                    "request_id": request_id,
                    "approver_slack_id": "NA",
                    "approver_email": "NA",
                    "operation_type": "revoke",
                }

                response = log_operation_to_dynamodb(
                    logger, DYNAMODB_TABLE_NAME, audit_entry
                )
                logger.debug(response)

                if POST_UPDATE_TO_SLACK:
                    status_message = {
                        "channel": SLACK_CHANNEL_ID,
                        "text": f'Revoked role {role_name} for user <@{slack_id}> in account {account["name"]}',
                    }
                    post_slack_message("/api/chat.postMessage", status_message, TOKEN)

    print("Done")


if __name__ == "__main__":
    lambda_handler(None, None)
