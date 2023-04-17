from datetime import timedelta
from typing import Literal, Optional, TypeVar, Union

import jmespath as jp
from pydantic import BaseModel, root_validator
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
    TimePickerElement,
)
from slack_sdk.models.views import View

import entities


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

    TIMEPICKER_BLOCK_ID = "timepicker"
    TIMEPICKER_ACTION_ID = "timepickeraction"

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
                    block_id=cls.TIMEPICKER_BLOCK_ID,
                    text=MarkdownTextObject(text="Choose the time for which the permissions will be granted"),
                    accessory=TimePickerElement(
                        action_id=cls.TIMEPICKER_ACTION_ID,
                        initial_time="00:30",
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
        return InputBlock(
            block_id=cls.ACCOUNT_BLOCK_ID,
            label=PlainTextObject(text="Select account"),
            element=StaticSelectElement(
                action_id=cls.ACCOUNT_ACTION_ID,
                placeholder=PlainTextObject(text="Select account"),
                options=[Option(text=PlainTextObject(text=f"{account.name} #{account.id}"), value=account.id) for account in accounts],
            ),
        )

    @classmethod
    def build_select_permission_set_input_block(cls, permission_sets: list[entities.aws.PermissionSet]) -> InputBlock:
        return InputBlock(
            block_id=cls.PERMISSION_SET_BLOCK_ID,
            label=PlainTextObject(text="Select permission set"),
            element=StaticSelectElement(
                action_id=cls.PERMISSION_SET_ACTION_ID,
                placeholder=PlainTextObject(text="Select permission set"),
                options=[
                    Option(text=PlainTextObject(text=permission_set.name), value=permission_set.name) for permission_set in permission_sets
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
        hhmm = jp.search(f"{cls.TIMEPICKER_BLOCK_ID}.{cls.TIMEPICKER_ACTION_ID}.selected_time", values)
        return RequestForAccess.parse_obj(
            {
                "permission_duration": timepicker_str_to_timedelta(hhmm),
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


def timepicker_str_to_timedelta(time_str: str) -> timedelta:
    hours, minutes = time_str.split(":")
    return timedelta(hours=int(hours), minutes=int(minutes))


def humanize_timedelta(td: timedelta):
    # example 12h 30m
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"


def unhumanize_timedelta(td_str: str) -> timedelta:
    hours, minutes = td_str.split(" ")
    hours = hours.removesuffix("h")
    minutes = minutes.removesuffix("m")
    return timedelta(hours=int(hours), minutes=int(minutes))


def build_approval_request_message_blocks(
    requester_slack_id: str,
    account: entities.aws.Account,
    role_name: str,
    reason: str,
    permission_duration: timedelta,
    show_buttons: bool = True,
):
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
                        action_id="approve",
                        text=PlainTextObject(text="Approve"),
                        style="primary",
                        value="approve",
                    ),
                    ButtonElement(
                        action_id="deny",
                        text=PlainTextObject(text="Deny"),
                        style="danger",
                        value="deny",
                    ),
                ],
            )
        )
    return blocks


def button_click_info_block(action: Literal["approve", "deny"], approver_slack_id: str) -> SectionBlock:
    return SectionBlock(
        block_id="footer",
        text=MarkdownTextObject(
            text=f"<@{approver_slack_id}> pressed {action} button",
        ),
    )


class ButtonClickedPayload(BaseModel):
    action: Literal["approve", "deny"]
    approver_slack_id: str
    thread_ts: str
    channel_id: str
    message: dict
    request: RequestForAccess

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
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
    return entities.slack.User.parse_obj({"id": jp.search("user.id", user), "email": jp.search("user.profile.email", user)})

def get_user(client: WebClient, id: str) -> entities.slack.User:
    response = client.users_info(user=id)
    return parse_user(response.data) # type: ignore

def get_user_by_email(client: WebClient, email: str) -> entities.slack.User:
    response = client.users_lookupByEmail(email=email)
    return parse_user(response.data) # type: ignore
