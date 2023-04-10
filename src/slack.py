import functools
import hashlib
import hmac
import http.client
import json
import logging
import time
from pydantic import BaseModel, root_validator
import jmespath as jp
from typing import Literal, NamedTuple, Optional
import organizations
import sso


# https://api.slack.com/surfaces/modals/using
# https://app.slack.com/block-kit-builder/
def prepare_initial_form(trigger_id, permission_sets: list[sso.PermissionSet], accounts: list[organizations.AWSAccount]):
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
                                    "text": permission_set.name,
                                },
                                "value": permission_set.name,
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
                                "text": {"type": "plain_text", "text": account.name},
                                "value": account.id,
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


def prepare_approval_request(
    channel: str, requester_slack_id: str, account_id: str, role_name: str, reason: str, show_buttons: bool = True
):
    header_text = "AWS account access request."
    blocks = [
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
                {"type": "mrkdwn", "text": f"Reason: {reason}"},
            ],
        },
    ]
    if show_buttons:
        blocks.append(
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
            }
        )
    return {"channel": channel, "blocks": blocks}


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
    # print(f"Sending message: {json.dumps(message)}")
    headers = {"Content-type": "application/json", "Authorization": f"Bearer {token}"}
    connection = http.client.HTTPSConnection("slack.com")
    connection.request("POST", api_path, json.dumps(message), headers)
    response = connection.getresponse()
    response_status = response.status
    response_body = json.loads(response.read().decode())
    # print(f"Response: {response_status}, message: {response_body}")
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
        raise ValueError(f"Request computed signature v0={signature} " + f"is not equal to received one {request_signature}")
    else:
        logging.info("Request looks legit. It is safe to process it")


def tag_users(*id: str) -> str:
    return " ".join(f"<@{user_id}>" for user_id in id)


class Slack:
    def __init__(self, bot_token: str, default_channel: str) -> None:
        self.headers = {"Content-type": "application/json", "Authorization": f"Bearer {bot_token}"}
        self.default_channel = default_channel

    class Response(NamedTuple):
        status: int
        body: dict

    def api_call(
        self,
        api_path: str,
        body: Optional[dict] = None,
        method: str = "POST",
    ) -> Response:
        connection = http.client.HTTPSConnection("slack.com")
        if body is None:
            body = {}

        connection.request(method, api_path, json.dumps(body), self.headers)
        response = connection.getresponse()
        return Slack.Response(response.status, json.loads(response.read().decode()))

    def post_message(
        self, channel: Optional[str] = None, thread_ts: Optional[str] = None, text: Optional[str] = None, **kwargs
    ) -> Response:
        body = {}
        if thread_ts is not None:
            body["thread_ts"] = thread_ts
        if text is not None:
            body["text"] = text
        if channel is None:
            body["channel"] = self.default_channel

        body.update(kwargs)
        return self.api_call(method="POST", api_path="/api/chat.postMessage", body=body)

    class User(NamedTuple):
        id: str
        name: str
        email: Optional[str]

    @functools.cache
    def list_users(self) -> list[User]:
        response = self.api_call(method="GET", api_path="/api/users.list")
        body = response.body

        return [Slack.User(user["id"], user["name"], user.get("profile", {}).get("email")) for user in body["members"]]

    def get_user_by_id(self, id: str) -> Optional[User]:
        return next((user for user in self.list_users() if user.id == id), None)

    def get_user_by_email(self, email: str) -> Optional[User]:
        return next((user for user in self.list_users() if user.email == email), None)


class ButtonClickedPayload(BaseModel):
    action: Literal["approve", "deny"]
    account_id: str
    permission_set_name: str
    approver_slack_id: str
    thread_ts: str
    reason: str
    requester_slack_id: str
    channel_id: str
    message: dict

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        fields = jp.search("message.blocks[?block_id == 'content'].fields[]", values)
        requester_mention: Optional[str] = cls.find_in_fields(fields, "Requester")
        if requester_mention is None:
            raise ValueError("Can not find requester mention")

        return {
            "action": jp.search("actions[0].value", values),
            # slack id will come with <@{requester_slack_id}> so we need to clean it
            "requester_slack_id": requester_mention.removeprefix("<@").removesuffix(">"),
            "account_id": cls.find_in_fields(fields, "AccountId"),
            "permission_set_name": cls.find_in_fields(fields, "Role name"),
            "approver_slack_id": jp.search("user.id", values),
            "thread_ts": jp.search("message.ts", values),
            "reason": cls.find_in_fields(fields, "Reason"),
            "channel_id": jp.search("channel.id", values),
            "message": values.get("message"),
        }

    @staticmethod
    def find_in_fields(fields: list[dict[str, str]], key: str) -> Optional[str]:
        for field in fields:
            if field["text"].startswith(key):
                return field["text"].split(": ")[1].strip()
