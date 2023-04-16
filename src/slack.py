from datetime import timedelta
from pydantic import BaseModel, root_validator
import jmespath as jp
from typing import Literal, Optional, TypeVar, Union
import organizations
import sso
from slack_sdk import WebClient
from slack_sdk.models.views import View
from slack_sdk.models.blocks import (
    PlainTextObject,
    InputBlock,
    PlainTextInputElement,
    SectionBlock,
    MarkdownTextObject,
    DividerBlock,
    Option,
    StaticSelectElement,
    Block,
    ActionsBlock,
    ButtonElement,
    TimePickerElement,
)

import entities

T = TypeVar("T", Block, dict)


def get_block_id(block: Union[Block, dict]) -> Optional[str]:
    return block["block_id"] if isinstance(block, dict) else block.block_id


def remove_blocks(blocks: list[T], block_ids: list[str]) -> list[T]:
    return [block for block in blocks if get_block_id(block) not in block_ids]


def insert_blocks(blocks: list[T], blocks_to_insert: list[Block], after_block_id: str) -> list[T]:
    index = next(i for i, block in enumerate(blocks) if get_block_id(block) == after_block_id)
    return blocks[: index + 1] + blocks_to_insert + blocks[index + 1 :]  # type: ignore


def select_account_input_block(accounts: list[organizations.AWSAccount]) -> InputBlock:
    return InputBlock(
        block_id="select_account",
        label=PlainTextObject(text="Select account"),
        element=StaticSelectElement(
            action_id="selected_account",
            placeholder=PlainTextObject(text="Select account"),
            options=[Option(text=PlainTextObject(text=account.name), value=account.id) for account in accounts],
        ),
    )


def select_permission_set_input_block(permission_sets: list[sso.PermissionSet]) -> InputBlock:
    return InputBlock(
        block_id="select_permission_set",
        label=PlainTextObject(text="Select permission set"),
        element=StaticSelectElement(
            action_id="selected_permission_set",
            placeholder=PlainTextObject(text="Select permission set"),
            options=[
                Option(text=PlainTextObject(text=permission_set.name), value=permission_set.name) for permission_set in permission_sets
            ],
        ),
    )


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


def prepare_approval_request_blocks(
    requester_slack_id: str,
    account: organizations.AWSAccount,
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
    account_id: str
    permission_set_name: str
    approver_slack_id: str
    thread_ts: str
    reason: str
    requester_slack_id: str
    channel_id: str
    message: dict
    permission_duration: timedelta

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        fields = jp.search("message.blocks[?block_id == 'content'].fields[]", values)
        requester_mention: Optional[str] = cls.find_in_fields(fields, "Requester")
        if requester_mention is None:
            raise ValueError("Can not find requester mention")
        account = cls.find_in_fields(fields, "Account")

        humanized_permission_duration = cls.find_in_fields(fields, "Permission duration")
        if humanized_permission_duration is None:
            raise ValueError("Can not find permission duration")
        permission_duration = unhumanize_timedelta(humanized_permission_duration)

        if account is None:
            raise ValueError("Can not find account in message")
        account_id = account.split("#")[-1]
        return {
            "permission_duration": permission_duration,
            "action": jp.search("actions[0].value", values),
            # slack id will come with <@{requester_slack_id}> so we need to clean it
            "requester_slack_id": requester_mention.removeprefix("<@").removesuffix(">"),
            "account_id": account_id,
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


class SlackUser(BaseModel):
    id: str
    email: str

    @root_validator(pre=True)
    def validate(cls, values):
        return {"id": jp.search("user.id", values), "email": jp.search("user.profile.email", values)}


def get_user(client: WebClient, id: str) -> SlackUser:
    response = client.users_info(user=id)
    return SlackUser.parse_obj(response.data)


def get_user_by_email(client: WebClient, email: str) -> SlackUser:
    response = client.users_lookupByEmail(email=email)
    return SlackUser.parse_obj(response.data)
