import datetime
import time
from datetime import timedelta
from typing import Optional, TypeVar, Union

import jmespath as jp
import slack_sdk.errors
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import root_validator
from slack_sdk import WebClient
from slack_sdk.models.blocks import (
    ActionsBlock,
    Block,
    ButtonElement,
    DividerBlock,
    InputBlock,
    MarkdownTextObject,
    Option,
    PlainTextInputElement,
    PlainTextObject,
    SectionBlock,
    StaticSelectElement,
)
from slack_sdk.models.views import View
from slack_sdk.web.slack_response import SlackResponse

import config
import entities
import sso
from entities import BaseModel

# ruff: noqa: ANN102, PGH003

logger = config.get_logger(service="slack")
cfg = config.get_config()


class RequestForAccess(BaseModel):
    permission_set_name: str
    account_id: str
    reason: str
    requester_slack_id: str
    permission_duration: timedelta


class RequestForAccessView:
    CALLBACK_ID = "request_for_access_submitted"

    REASON_BLOCK_ID = "provide_reason"
    REASON_ACTION_ID = "provided_reason"

    ACCOUNT_BLOCK_ID = "select_account"
    ACCOUNT_ACTION_ID = "selected_account"

    PERMISSION_SET_BLOCK_ID = "select_permission_set"
    PERMISSION_SET_ACTION_ID = "selected_permission_set"

    DURATION_BLOCK_ID = "duration_picker"
    DURATION_ACTION_ID = "duration_picker_action"

    LOADING_BLOCK_ID = "loading"

    @classmethod
    def build(cls) -> View:
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            submit=PlainTextObject(text="Request"),
            close=PlainTextObject(text="Cancel"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[
                SectionBlock(text=MarkdownTextObject(text=":wave: Hey! Please fill form below to request AWS access.")),
                DividerBlock(),
                SectionBlock(
                    block_id=cls.DURATION_BLOCK_ID,
                    text=MarkdownTextObject(text="Select the duration for which the authorization will be provided"),
                    accessory=StaticSelectElement(
                        action_id=cls.DURATION_ACTION_ID,
                        initial_option=get_max_duration_block(cfg)[0],
                        options=get_max_duration_block(cfg),
                        placeholder=PlainTextObject(text="Select duration"),
                    ),
                ),
                InputBlock(
                    block_id=cls.REASON_BLOCK_ID,
                    label=PlainTextObject(text="What is it you are going to do"),
                    element=PlainTextInputElement(
                        action_id=cls.REASON_ACTION_ID,
                        multiline=True,
                    ),
                ),
                DividerBlock(),
                SectionBlock(
                    text=MarkdownTextObject(
                        text="Remember to use access responsibly. All actions (AWS API calls) are being recorded.",
                    ),
                ),
                SectionBlock(
                    block_id=cls.LOADING_BLOCK_ID,
                    text=MarkdownTextObject(
                        text=":hourglass: Loading available accounts and permission sets...",
                    ),
                ),
            ],
        )

    @classmethod
    def build_select_account_input_block(cls, accounts: list[entities.aws.Account]) -> InputBlock:
        # TODO: handle case when there are more than 100 accounts
        # 99 is the limit for StaticSelectElement
        # https://slack.dev/python-slack-sdk/api-docs/slack_sdk/models/blocks/block_elements.html#:~:text=StaticSelectElement(InputInteractiveElement)%3A%0A%20%20%20%20type%20%3D%20%22static_select%22-,options_max_length%20%3D%20100,-option_groups_max_length%20%3D%20100%0A%0A%20%20%20%20%40property%0A%20%20%20%20def%20attributes(
        if len(accounts) >99: # noqa: PLR2004
            accounts = accounts[:99]
        sorted_accounts = sorted(accounts, key=lambda account: account.name)
        return InputBlock(
            block_id=cls.ACCOUNT_BLOCK_ID,
            label=PlainTextObject(text="Select account"),
            element=StaticSelectElement(
                action_id=cls.ACCOUNT_ACTION_ID,
                placeholder=PlainTextObject(text="Select account"),
                options=[
                    Option(text=PlainTextObject(text=f"{account.id} - {account.name}"), value=account.id) for account in sorted_accounts
                ],
            ),
        )

    @classmethod
    def build_select_permission_set_input_block(cls, permission_sets: list[entities.aws.PermissionSet]) -> InputBlock:
        sorted_permission_sets = sorted(permission_sets, key=lambda permission_set: permission_set.name)
        return InputBlock(
            block_id=cls.PERMISSION_SET_BLOCK_ID,
            label=PlainTextObject(text="Select permission set"),
            element=StaticSelectElement(
                action_id=cls.PERMISSION_SET_ACTION_ID,
                placeholder=PlainTextObject(text="Select permission set"),
                options=[
                    Option(text=PlainTextObject(text=permission_set.name), value=permission_set.name)
                    for permission_set in sorted_permission_sets
                ],
            ),
        )

    @classmethod
    def update_with_accounts_and_permission_sets(
        cls, accounts: list[entities.aws.Account], permission_sets: list[entities.aws.PermissionSet]
    ) -> View:
        view = cls.build()
        view.blocks = remove_blocks(view.blocks, block_ids=[cls.LOADING_BLOCK_ID])
        view.blocks = insert_blocks(
            blocks=view.blocks,
            blocks_to_insert=[
                cls.build_select_account_input_block(accounts),
                cls.build_select_permission_set_input_block(permission_sets),
            ],
            after_block_id=cls.REASON_BLOCK_ID,
        )
        return view

    @classmethod
    def parse(cls, obj: dict) -> RequestForAccess:
        values = jp.search("view.state.values", obj)
        hhmm = jp.search(f"{cls.DURATION_BLOCK_ID}.{cls.DURATION_ACTION_ID}.selected_option.value", values)
        hours, minutes = map(int, hhmm.split(":"))
        duration = timedelta(hours=hours, minutes=minutes)
        return RequestForAccess.parse_obj(
            {
                "permission_duration": duration,
                "permission_set_name": jp.search(
                    f"{cls.PERMISSION_SET_BLOCK_ID}.{cls.PERMISSION_SET_ACTION_ID}.selected_option.value", values
                ),
                "account_id": jp.search(f"{cls.ACCOUNT_BLOCK_ID}.{cls.ACCOUNT_ACTION_ID}.selected_option.value", values),
                "reason": jp.search(f"{cls.REASON_BLOCK_ID}.{cls.REASON_ACTION_ID}.value", values),
                "requester_slack_id": jp.search("user.id", obj),
            }
        )


T = TypeVar("T", Block, dict)


def get_block_id(block: Union[Block, dict]) -> Optional[str]:
    return block["block_id"] if isinstance(block, dict) else block.block_id


def remove_blocks(blocks: list[T], block_ids: list[str]) -> list[T]:
    return [block for block in blocks if get_block_id(block) not in block_ids]


def insert_blocks(blocks: list[T], blocks_to_insert: list[Block], after_block_id: str) -> list[T]:
    index = next(i for i, block in enumerate(blocks) if get_block_id(block) == after_block_id)
    return blocks[: index + 1] + blocks_to_insert + blocks[index + 1 :]  # type: ignore


def humanize_timedelta(td: timedelta) -> str:
    # example 12h 30m
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"


def unhumanize_timedelta(td_str: str) -> timedelta:
    hours, minutes = td_str.split(" ")
    hours = hours.removesuffix("h")
    minutes = minutes.removesuffix("m")
    return timedelta(hours=int(hours), minutes=int(minutes))


def build_approval_request_message_blocks(  # noqa: PLR0913
    requester_slack_id: str,
    account: entities.aws.Account,
    role_name: str,
    reason: str,
    permission_duration: timedelta,
    show_buttons: bool = True,
) -> list[Block]:
    blocks: list[Block] = [
        SectionBlock(block_id="header", text=MarkdownTextObject(text="AWS account access request.")),
        SectionBlock(
            block_id="content",
            fields=[
                MarkdownTextObject(text=f"Requester: <@{requester_slack_id}>"),
                MarkdownTextObject(text=f"Account: {account.name} #{account.id}"),
                MarkdownTextObject(text=f"Role name: {role_name}"),
                MarkdownTextObject(text=f"Reason: {reason}"),
                MarkdownTextObject(text=f"Permission duration: {humanize_timedelta(permission_duration)}"),
            ],
        ),
    ]
    if show_buttons:
        blocks.append(
            ActionsBlock(
                block_id="buttons",
                elements=[
                    ButtonElement(
                        action_id=entities.ApproverAction.Approve.value,
                        text=PlainTextObject(text="Approve"),
                        style="primary",
                        value=entities.ApproverAction.Approve.value,
                    ),
                    ButtonElement(
                        action_id=entities.ApproverAction.Discard.value,
                        text=PlainTextObject(text="Discard"),
                        style="danger",
                        value=entities.ApproverAction.Discard.value,
                    ),
                ],
            )
        )
    return blocks


def button_click_info_block(action: entities.ApproverAction, approver_slack_id: str) -> SectionBlock:
    return SectionBlock(
        block_id="footer",
        text=MarkdownTextObject(
            text=f"<@{approver_slack_id}> pressed {action.value} button",
        ),
    )


class ButtonClickedPayload(BaseModel):
    action: entities.ApproverAction
    approver_slack_id: str
    thread_ts: str
    channel_id: str
    message: dict
    request: RequestForAccess

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        fields = jp.search("message.blocks[?block_id == 'content'].fields[]", values)
        requester_mention = cls.find_in_fields(fields, "Requester")
        requester_slack_id = requester_mention.removeprefix("<@").removesuffix(">")
        humanized_permission_duration = cls.find_in_fields(fields, "Permission duration")
        permission_duration = unhumanize_timedelta(humanized_permission_duration)
        account = cls.find_in_fields(fields, "Account")
        account_id = account.split("#")[-1]
        return {
            "action": jp.search("actions[0].value", values),
            "approver_slack_id": jp.search("user.id", values),
            "thread_ts": jp.search("message.ts", values),
            "channel_id": jp.search("channel.id", values),
            "message": values.get("message"),
            "request": RequestForAccess(
                requester_slack_id=requester_slack_id,
                account_id=account_id,
                permission_set_name=cls.find_in_fields(fields, "Role name"),
                reason=cls.find_in_fields(fields, "Reason"),
                permission_duration=permission_duration,
            ),
        }

    @staticmethod
    def find_in_fields(fields: list[dict[str, str]], key: str) -> str:
        for field in fields:
            if field["text"].startswith(key):
                return field["text"].split(": ")[1].strip()
        raise ValueError(f"Failed to parse message. Could not find {key} in fields: {fields}")


def parse_user(user: dict) -> entities.slack.User:
    return entities.slack.User.parse_obj(
        {"id": jp.search("user.id", user), "email": jp.search("user.profile.email", user), "real_name": jp.search("user.real_name", user)}
    )


def get_user(client: WebClient, id: str) -> entities.slack.User:
    response = client.users_info(user=id)
    return parse_user(response.data)  # type: ignore


def get_user_by_email(client: WebClient, email: str) -> entities.slack.User:
    start = datetime.datetime.now()
    timeout_seconds = 30
    try:
        r = client.users_lookupByEmail(email=email)
        return parse_user(r.data)  # type: ignore
    except slack_sdk.errors.SlackApiError as e:
        if e.response["error"] == "ratelimited":
            if datetime.datetime.now() - start >= datetime.timedelta(seconds=timeout_seconds):
                raise e
            logger.info(f"Rate limited when getting slack user by email. Sleeping for 3 seconds. {e}")
            time.sleep(3)
            return get_user_by_email(client, email)
        else:
            raise e
    except Exception as e:
        raise e


def remove_buttons(payload: ButtonClickedPayload, client: WebClient, approver: entities.slack.User) -> SlackResponse:
    blocks = remove_blocks(payload.message["blocks"], block_ids=["buttons"])
    blocks.append(button_click_info_block(payload.action, approver.id))
    return client.chat_update(
        channel=payload.channel_id,
        ts=payload.thread_ts,
        blocks=blocks,
        text="Buttons were removed.",
    )


def create_slack_mention_by_principal_id(
    account_assignment: sso.AccountAssignment | sso.UserAccountAssignment,
    sso_client: SSOAdminClient,
    cfg: config.Config,
    identitystore_client: IdentityStoreClient,
    slack_client: WebClient,
) -> str:
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    aws_user_emails = sso.get_user_emails(
        identitystore_client,
        sso_instance.identity_store_id,
        account_assignment.principal_id if isinstance(account_assignment, sso.AccountAssignment) else account_assignment.user_principal_id,
    )
    user_name = None

    for email in aws_user_emails:
        try:
            slack_user = get_user_by_email(slack_client, email)
            user_name = slack_user.real_name
        except Exception:
            continue

    return f"{user_name}" if user_name is not None else aws_user_emails[0]


def get_message_from_timestamp(channel_id: str, message_ts: str, slack_client: slack_sdk.WebClient) -> dict | None:
    response = slack_client.conversations_history(channel=channel_id)

    if response["ok"]:
        messages = response.get("messages")
        if messages is not None:
            for message in messages:
                if "ts" in message and message["ts"] == message_ts:
                    return message

    return None


def get_max_duration_block(cfg: config.Config) -> list[Option]:
    return [
        Option(text=PlainTextObject(text=f"{i:02d}:00"), value=f"{i:02d}:00")
        for i in range(1, cfg.max_permissions_duration_time + 1)
    ]
