import base64
import json
import logging
import os
from urllib import parse as urlparse

import boto3
import sso
import config
from dynamodb import log_operation_to_dynamodb
import slack

logging.basicConfig(format="[%(asctime)s] p%(process)s {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger.setLevel(logging.getLevelName(log_level))


def lambda_handler(event, context):
    print(f"event: {json.dumps(event)}")
    cfg = config.Config()  # type: ignore

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
    slack_cfg = config.SlackConfig()  # type: ignore
    try:
        slack.verify_request(request_headers, slack_cfg.signing_secret, body_as_string)
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
        main(actual_payload, cfg, slack_cfg)
    except Exception as e:
        logger.error(e)
        return {"statusCode": 500}

    return {"statusCode": 200}


def main(payload, cfg: config.Config, slack_cfg: config.SlackConfig):
    # Initial call
    if payload["type"] == "shortcut":
        trigger_id = payload["trigger_id"]
        inital_form = slack.prepare_initial_form(trigger_id, cfg.lookup("permission_sets"), cfg.lookup("accounts"))
        slack.post_message("/api/views.open", inital_form, slack_cfg.bot_token)

    # Form submitted
    elif payload["type"] == "view_submission":
        values = payload["view"]["state"]["values"]
        selected_account = values["select_account"]["selected_account"]["selected_option"]["value"]
        approvers = cfg.lookup("accounts", "id", selected_account, "approvers")
        requires_approval = True if len(approvers) > 0 else False
        message = slack.prepare_approval_request(
            slack_cfg.channel_id,
            payload["user"]["id"],
            selected_account,
            requires_approval,
            values["select_role"]["selected_role"]["selected_option"]["value"],
            values["provide_reason"]["provided_reason"]["value"],
        )
        _, response_status = slack.post_message("/api/chat.postMessage", message, slack_cfg.bot_token)
        if requires_approval:
            approvers_slack_ids = [
                f'<@{cfg.lookup("users", "email", approver, "slack_id")}>' for approver in approvers
            ]
            approval_request_notification_message = {
                "channel": response_status["channel"],
                "thread_ts": response_status["ts"],
                "text": " ".join(approvers_slack_ids) + " there is a request waiting for the approval",
            }
            slack.post_message("/api/chat.postMessage", approval_request_notification_message, slack_cfg.bot_token)

    # someone pressed a button
    elif payload["type"] == "block_actions":
        if payload["actions"][0]["value"] != "approve" and payload["actions"][0]["value"] != "deny":
            logger.error(f"Unsupported type. payload: {payload}")
            return {"statusCode": 500}
        account_id: str = slack.find_value_in_content_block(payload["message"]["blocks"], "AccountId")  # type: ignore
        approvers = cfg.lookup("accounts", "id", account_id, "approvers")
        approvers_slack_ids = [cfg.lookup("users", "email", approver, "slack_id") for approver in approvers]
        requires_approval = True if len(approvers) > 0 else False
        approvers = [user["slack_id"] for user in cfg.lookup("users") if user["can_approve"]]
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
            slack.post_message("/api/chat.postMessage", status_message, slack_cfg.bot_token)
            return
        message = slack.prepare_approval_request_update(
            payload["channel"]["id"],
            payload["message"]["ts"],
            payload["user"]["id"],
            payload["actions"][0]["value"],
            payload["message"]["blocks"],
        )
        slack.post_message("/api/chat.update", message, slack_cfg.bot_token)
        if payload["actions"][0]["value"] == "approve":
            status_message = {
                "thread_ts": payload["message"]["ts"],
                "channel": payload["channel"]["id"],
                "text": "Updating permissions as requested...",
            }
            slack.post_message("/api/chat.postMessage", status_message, slack_cfg.bot_token)
            client = boto3.client("sso-admin")  # type: ignore
            sso_instance_arn = sso.get_sso_instance_arn(client, cfg)
            logger.debug(f"selected SSO instance: {sso_instance_arn}")
            logger.debug("Create assigment")
            account_id: str = slack.find_value_in_content_block(payload["message"]["blocks"], "AccountId")  # type: ignore
            role_name = slack.find_value_in_content_block(payload["message"]["blocks"], "Role name")
            # slack id will come with <@{requester_slack_id}> so we need to clean it
            requester_slack_id: str = slack.find_value_in_content_block(payload["message"]["blocks"], "Requester")  # type: ignore
            requester_slack_id_clean = requester_slack_id.replace("@", "").replace("<", "").replace(">", "")
            reason = slack.find_value_in_content_block(payload["message"]["blocks"], "Reason")
            account_assignment = sso.create_account_assignment_and_wait_for_result(
                client,
                sso.UserAccountAssignment(
                    instance_arn=sso_instance_arn,
                    account_id=account_id,
                    permission_set_arn=cfg.lookup("permission_sets", "name", role_name, "arn"),
                    user_principal_id=cfg.lookup("users", "slack_id", requester_slack_id_clean, "sso_id"),
                ),
            )
            request_id = account_assignment.request_id

            audit_entry = {
                "role_name": role_name,
                "account_id": account_id,
                "reason": reason,
                "requester_slack_id": requester_slack_id_clean,
                "requester_email": cfg.lookup("users", "slack_id", requester_slack_id_clean, "email"),
                "request_id": request_id,
                "approver_slack_id": payload["user"]["id"],
                "approver_email": cfg.lookup("users", "slack_id", payload["user"]["id"], "email"),
                "operation_type": "grant",
            }

            response = log_operation_to_dynamodb(logger, cfg.dynamodb_table_name, audit_entry)
            logger.debug(response)

            status_message = {
                "thread_ts": payload["message"]["ts"],
                "channel": payload["channel"]["id"],
                "text": "Done",
            }
            slack.post_message("/api/chat.postMessage", status_message, slack_cfg.bot_token)

    else:
        logger.error(f"Unsupported type. payload: {payload}")
        raise Exception(f"Unsupported type. payload: {payload}")
