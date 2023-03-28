import http.client
import json
import hashlib
import hmac
import logging
import time

# https://api.slack.com/surfaces/modals/using
# https://app.slack.com/block-kit-builder/
def prepare_initial_form(trigger_id, permission_sets, accounts):
    return {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": "modal-identifier",
            "submit": {"type": "plain_text", "text": "Request"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "title": {"type": "plain_text", "text": "Get AWS access"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": ":wave: Hey! Please fill form below to request AWS access.",
                    },
                },
                {"type": "divider"},
                {
                    "block_id": "select_role",
                    "type": "input",
                    "label": {"type": "plain_text", "text": "Select role to assume"},
                    "element": {
                        "action_id": "selected_role",
                        "type": "radio_buttons",
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": permission_set["name"],
                                },
                                "value": permission_set["name"],
                            }
                            for permission_set in permission_sets
                        ],
                    },
                },
                {
                    "block_id": "select_account",
                    "type": "input",
                    "label": {"type": "plain_text", "text": "Select AWS account"},
                    "element": {
                        "action_id": "selected_account",
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select AWS account",
                        },
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": account["name"]},
                                "value": account["id"],
                            }
                            for account in accounts
                        ],
                    },
                },
                {
                    "block_id": "provide_reason",
                    "type": "input",
                    "label": {
                        "type": "plain_text",
                        "text": "What is it you are going to do",
                    },
                    "element": {
                        "action_id": "provided_reason",
                        "type": "plain_text_input",
                        "multiline": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "Remember to use access responsibly. All actions (AWS API calls) are being recorded.",
                    },
                },
            ],
        },
    }


def prepare_approval_request(channel, requester_slack_id, account_id, requires_approval, role_name, reason):
    header_text = "AWS account access request."
    if requires_approval:
        header_text += "\n⚠️ This account does not allow self-approval ⚠️"
        header_text += "\nWe already contacted eligible approvers, wait for them to click the button."
    can_self_approve = "No" if requires_approval else "Yes"
    return {
        "channel": channel,
        "blocks": [
            {
                "type": "section",
                "block_id": "header",
                "text": {"type": "mrkdwn", "text": header_text},
            },
            {
                "type": "section",
                "block_id": "content",
                "fields": [
                    {"type": "mrkdwn", "text": f"Requester: <@{requester_slack_id}>"},
                    {"type": "mrkdwn", "text": f"AccountId: {account_id}"},
                    {"type": "mrkdwn", "text": f"Role name: {role_name}"},
                    {"type": "mrkdwn", "text": f"Can self-approve: {can_self_approve}"},
                    {"type": "mrkdwn", "text": f"Reason: {reason}"},
                ],
            },
            {
                "type": "actions",
                "block_id": "buttons",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": "approve",
                    },
                    {
                        "type": "button",
                        "action_id": "deny",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "value": "deny",
                    },
                ],
            },
        ],
    }


def find_value_in_content_block(blocks, key):
    for block in blocks:
        if block["block_id"] != "content":
            continue
        for field in block["fields"]:
            if field["text"].startswith(key):
                value = field["text"].split(": ")[1]
                return value.strip()
        raise KeyError(f"Can not find filed with key={key} in block {block}")


def prepare_approval_request_update(channel, ts, approver, action, blocks):
    message = {"channel": channel, "ts": ts, "blocks": []}
    # loop through original message and take header and content blocks to drop buttons
    for block in blocks:
        if block["block_id"] in ["header", "content"]:
            message["blocks"].append(block)
    # add information about approver
    message["blocks"].append(
        {
            "type": "section",
            "block_id": "footer",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{approver}> pressed {action} button",
            },
        }
    )
    return message


def post_message(api_path: str, message: dict, token: str):
    # POST https://slack.com/api/views.open
    # Content-type: application/json
    # Authorization: Bearer YOUR_ACCESS_TOKEN_HERE
    print(f"Sending message: {json.dumps(message)}")
    headers = {"Content-type": "application/json", "Authorization": f"Bearer {token}"}
    connection = http.client.HTTPSConnection("slack.com")
    connection.request("POST", api_path, json.dumps(message), headers)
    response = connection.getresponse()
    response_status = response.status
    response_body = json.loads(response.read().decode())
    print(f"Response: {response_status}, message: {response_body}")
    return response_status, response_body


def verify_request(headers, signing_secret, body, age=60):
    """Method to verifty slack requests
    Read more here https://api.slack.com/authentication/verifying-requests-from-slack
    """
    timestamp = None
    if "X-Slack-Request-Timestamp" in headers:
        timestamp = headers["X-Slack-Request-Timestamp"]
    elif "x-slack-request-timestamp" in headers:
        timestamp = headers["x-slack-request-timestamp"]
    else:
        raise ValueError("Request does not have X-Slack-Request-Timestamp or x-slack-request-timestamp")
    if not timestamp.isdigit():
        raise ValueError("Value of X-Slack-Request-Timestamp does not appear to be a digit")

    request_signature = None
    if "X-Slack-Signature" in headers:
        request_signature = headers["X-Slack-Signature"]
    elif "x-slack-signature" in headers:
        request_signature = headers["x-slack-signature"]
    else:
        raise ValueError("Request does not have X-Slack-Signature or x-slack-signature")
    if abs(time.time() - int(timestamp)) > age:
        # The request timestamp is more than five minutes from local time.
        # It could be a replay attack, so let's ignore it.
        raise ValueError("Request is older than one minute")

    sig_basestring = f"v0:{timestamp}:{body}"
    signature = hmac.new(
        bytes(signing_secret, "utf8"),
        msg=bytes(sig_basestring, "utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    logging.info(f"computed signature = {signature}")
    if f"v0={signature}" != request_signature:
        raise ValueError(
            f"Request computed signature v0={signature} " + f"is not equal to received one {request_signature}"
        )
    else:
        logging.info("Request looks legit. It is safe to process it")
