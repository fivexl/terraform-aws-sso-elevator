import base64
import json
import logging
import os
from urllib import parse as urlparse

import boto3
import layer as base  # pylint: disable=import-error

from config import config_lookup
from dynamodb import log_operation_to_dynamodb
from slack_helpers import (
    find_value_in_content_block,
    post_slack_message,
    prepare_slack_approval_request,
    prepare_slack_approval_request_update,
    prepare_slack_initial_form,
)
from sso import create_account_assigment, list_sso_instances

logging.basicConfig()
logger = logging.getLogger(__name__)
log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger.setLevel(logging.getLevelName(log_level))


def lambda_handler(event, context):
    # parameters
    TOKEN = base.read_env_variable_or_die("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = base.read_env_variable_or_die("SLACK_SIGNING_SECRET")
    DYNAMODB_TABLE_NAME = base.read_env_variable_or_die("DYNAMODB_TABLE_NAME")
    SLACK_CHANNEL_ID = base.read_env_variable_or_die("SLACK_CHANNEL_ID")

    print(f"event: {json.dumps(event)}")

    # Get body and headers
    if "headers" not in event:
        logger.error("Request does not contain headers. Return 400")
        return {"statusCode": 400}
    if "body" not in event:
        logger.error("Request does not contain body. Return 400")
        return {"statusCode": 400}
    request_headers = event["headers"]
    body_as_bytes = base64.b64decode(event["body"])
    body_as_string = body_as_bytes.decode("utf-8")

    # Verify request that it is coming from Slack https://api.slack.com/authentication/verifying-requests-from-slack
    # Check that required headers are present
    try:
        base.verify_slack_request(request_headers, SLACK_SIGNING_SECRET, body_as_string)
    except Exception as error:
        logger.error(error)
        return {"statusCode": 400}

    # Parse payload
    payload = dict(
        urlparse.parse_qsl(base64.b64decode(str(event["body"])).decode("ascii"))
    )  # data comes b64 and also urlencoded name=value& pairs
    actual_payload = json.loads(payload["payload"])

    logger.debug(f"payload:{json.dumps(actual_payload)}")
    try:
        main(actual_payload, SLACK_CHANNEL_ID, TOKEN, DYNAMODB_TABLE_NAME)
    except Exception as e:
        logger.error(e)
        return {"statusCode": 500}

    return {"statusCode": 200}


def main(payload, slack_channel_id, token, dynamodb_table_name):
    # Initial call
    if payload["type"] == "shortcut":
        trigger_id = payload["trigger_id"]
        inital_form = prepare_slack_initial_form(
            trigger_id, config_lookup("permission_sets"), config_lookup("accounts")
        )
        post_slack_message("/api/views.open", inital_form, token)

    # Form submitted
    elif payload["type"] == "view_submission":
        values = payload["view"]["state"]["values"]
        selected_account = values["select_account"]["selected_account"][
            "selected_option"
        ]["value"]
        approvers = config_lookup("accounts", "id", selected_account, "approvers")
        requires_approval = True if len(approvers) > 0 else False
        message = prepare_slack_approval_request(
            slack_channel_id,
            payload["user"]["id"],
            selected_account,
            requires_approval,
            values["select_role"]["selected_role"]["selected_option"]["value"],
            values["provide_reason"]["provided_reason"]["value"],
        )
        _, response_status = post_slack_message("/api/chat.postMessage", message, token)
        if requires_approval:
            approvers_slack_ids = [
                f'<@{config_lookup("users", "email", approver, "slack_id")}>'
                for approver in approvers
            ]
            approval_request_notification_message = {
                "channel": response_status["channel"],
                "thread_ts": response_status["ts"],
                "text": " ".join(approvers_slack_ids)
                + " there is a request waiting for the approval",
            }
            post_slack_message(
                "/api/chat.postMessage", approval_request_notification_message, token
            )

    # someone pressed a button
    elif payload["type"] == "block_actions":
        if (
            payload["actions"][0]["value"] != "approve"
            and payload["actions"][0]["value"] != "deny"
        ):
            logger.error(f"Unsupported type. payload: {payload}")
            return {"statusCode": 500}
        account_id = find_value_in_content_block(
            payload["message"]["blocks"], "AccountId"
        )
        approvers = config_lookup("accounts", "id", account_id, "approvers")
        approvers_slack_ids = [
            config_lookup("users", "email", approver, "slack_id")
            for approver in approvers
        ]
        requires_approval = True if len(approvers) > 0 else False
        approvers = [
            user["slack_id"] for user in config_lookup("users") if user["can_approve"]
        ]
        if (
            payload["actions"][0]["value"] == "approve"
            and requires_approval
            and payload["user"]["id"] not in approvers_slack_ids
        ):
            text = f'<@{payload["user"]["id"]}> you can not self-approve requests to this account\n'
            text += "Please wait for approval"
            status_message = {
                "thread_ts": payload["message"]["ts"],
                "channel": payload["channel"]["id"],
                "text": text,
            }
            post_slack_message("/api/chat.postMessage", status_message, token)
            return
        message = prepare_slack_approval_request_update(
            payload["channel"]["id"],
            payload["message"]["ts"],
            payload["user"]["id"],
            payload["actions"][0]["value"],
            payload["message"]["blocks"],
        )
        post_slack_message("/api/chat.update", message, token)
        if payload["actions"][0]["value"] == "approve":
            status_message = {
                "thread_ts": payload["message"]["ts"],
                "channel": payload["channel"]["id"],
                "text": "Updating permissions as requested...",
            }
            post_slack_message("/api/chat.postMessage", status_message, token)
            client = boto3.client("sso-admin")
            sso_instances = list_sso_instances(client, logger)
            sso_instance_arn = sso_instances["Instances"][0]["InstanceArn"]
            logger.debug(f"selected SSO instance: {sso_instance_arn}")
            logger.debug("Create assigment")
            account_id = find_value_in_content_block(
                payload["message"]["blocks"], "AccountId"
            )
            role_name = find_value_in_content_block(
                payload["message"]["blocks"], "Role name"
            )
            # slack id will come with <@{requester_slack_id}> so we need to clean it
            requester_slack_id = find_value_in_content_block(
                payload["message"]["blocks"], "Requester"
            )
            requester_slack_id_clean = (
                requester_slack_id.replace("@", "").replace("<", "").replace(">", "")
            )
            reason = find_value_in_content_block(payload["message"]["blocks"], "Reason")
            request_id = create_account_assigment(
                logger,
                client,
                sso_instance_arn,
                account_id,
                config_lookup("permission_sets", "name", role_name, "arn"),
                config_lookup("users", "slack_id", requester_slack_id_clean, "sso_id"),
            )

            audit_entry = {
                "role_name": role_name,
                "account_id": account_id,
                "reason": reason,
                "requester_slack_id": requester_slack_id_clean,
                "requester_email": config_lookup(
                    "users", "slack_id", requester_slack_id_clean, "email"
                ),
                "request_id": request_id,
                "approver_slack_id": payload["user"]["id"],
                "approver_email": config_lookup(
                    "users", "slack_id", payload["user"]["id"], "email"
                ),
                "operation_type": "grant",
            }

            response = log_operation_to_dynamodb(
                logger, dynamodb_table_name, audit_entry
            )
            logger.debug(response)

            status_message = {
                "thread_ts": payload["message"]["ts"],
                "channel": payload["channel"]["id"],
                "text": "Done",
            }
            post_slack_message("/api/chat.postMessage", status_message, token)

    else:
        logger.error(f"Unsupported type. payload: {payload}")
        raise Exception(f"Unsupported type. payload: {payload}")
