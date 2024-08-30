from datetime import timedelta

import boto3
import jmespath as jp
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import root_validator
from slack_bolt import Ack, App, BoltContext
from slack_sdk import WebClient
from slack_sdk.models.blocks import (
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

import access_control
import config
import entities
import s3
import schedule
import slack_helpers
import socket_mode
import sso
from access_control import AccessRequestDecision, ApproveRequestDecision
from entities import BaseModel
from errors import handle_errors
from slack_helpers import unhumanize_timedelta

logger = config.get_logger(service="main")
cfg = config.get_config()
app = App(token=socket_mode.bot_token)




# SSO
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----

identity_store_client: IdentityStoreClient = boto3.client("identitystore", region_name="us-east-1")
sso_client: SSOAdminClient = boto3.client("sso-admin", region_name="us-east-1")
schedule_client = boto3.client("scheduler", region_name="us-east-1")

sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)

identity_store_id = sso_instance.identity_store_id




@handle_errors
def handle_request_for_group_access_submittion(
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> SlackResponse | None:
    logger.info("Handling request for access submittion")
    request = RequestForGroupAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)

    group = sso.describe_group(identity_store_id, request.group_id, identity_store_client)

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=requester.email,
        group_id=request.group_id,
    )

    show_buttons = bool(decision.approvers)
    slack_response = client.chat_postMessage(
        blocks=slack_helpers.build_approval_request_message_blocks(
            requester_slack_id=request.requester_slack_id,
            group=group,
            reason=request.reason,
            permission_duration=request.permission_duration,
            show_buttons=show_buttons,
            color_coding_emoji=cfg.waiting_result_emoji,
        ),
        channel=cfg.slack_channel_id,
        text=f"Request for access to {group.name} group from {requester.real_name}",
    )

    if show_buttons:
        ts = slack_response["ts"]
        if ts is not None:
            schedule.schedule_discard_buttons_event(
                schedule_client=schedule_client, #type: ignore # noqa: PGH003
                time_stamp=ts,
                channel_id=cfg.slack_channel_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=schedule_client, #type: ignore # noqa: PGH003
                message_ts=ts,
                channel_id=cfg.slack_channel_id,
                time_to_wait=timedelta(
                    minutes=cfg.approver_renotification_initial_wait_time,
                ),
            )

    match decision.reason:
        case access_control.DecisionReason.ApprovalNotRequired:
            text = "Approval for this Group is not required. Request will be approved automatically."
            color_coding_emoji = cfg.good_result_emoji
        case access_control.DecisionReason.SelfApproval:
            text = "Self approval is allowed and requester is an approver. Request will be approved automatically."
            color_coding_emoji = cfg.good_result_emoji
        case access_control.DecisionReason.RequiresApproval:
            approvers = [slack_helpers.get_user_by_email(client, email) for email in decision.approvers]
            mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
            text = f"{mention_approvers} there is a request waiting for the approval."
            color_coding_emoji = cfg.waiting_result_emoji
        case access_control.DecisionReason.NoApprovers:
            text = "Nobody can approve this request."
            color_coding_emoji = cfg.bad_result_emoji
        case access_control.DecisionReason.NoStatements:
            text = "There are no statements for this Group."
            color_coding_emoji = cfg.bad_result_emoji

    client.chat_postMessage(text=text, thread_ts=slack_response["ts"], channel=cfg.slack_channel_id)

    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=slack_response["message"]["blocks"],
        color_coding_emoji=color_coding_emoji,
    )
    client.chat_update(
        channel=cfg.slack_channel_id,
        ts=slack_response["ts"],
        blocks=blocks,
        text=text,
    )

    user_principal_id = sso.get_user_principal_id_by_email(identity_store_client, sso_instance.identity_store_id, requester.email)

    execute_decision_on_group_request(
        group = group,
        user_principal_id = user_principal_id,
        permission_duration = request.permission_duration,
        approver = requester,
        requester = requester,
        reason = request.reason,
        decision = decision
    )

    if decision.grant:

        client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=f"Permissions granted to <@{requester.id}>",
            thread_ts=slack_response["ts"],
        )

cache_for_dublicate_requests = {}


@handle_errors
def handle_group_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse:  #type: ignore # noqa: PGH003 ARG001
    logger.info("Handling button click")
    payload = ButtonGroupClickedPayload.parse_obj(body)
    logger.info("Button click payload", extra={"payload": payload})
    approver = slack_helpers.get_user(client, id=payload.approver_slack_id)
    requester = slack_helpers.get_user(client, id=payload.request.requester_slack_id)

    if (
        cache_for_dublicate_requests.get("requester_slack_id") == payload.request.requester_slack_id
        and cache_for_dublicate_requests["group_id"] == payload.request.group_id
    ):
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> request is already in progress, please wait for the result.",
            thread_ts=payload.thread_ts,
        )
    cache_for_dublicate_requests["requester_slack_id"] = payload.request.requester_slack_id
    cache_for_dublicate_requests["group_id"] = payload.request.group_id


    if payload.action == entities.ApproverAction.Discard:
        blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
            blocks=payload.message["blocks"],
            color_coding_emoji=cfg.bad_result_emoji,
        )

        blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
        blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

        text = f"Request was discarded by<@{approver.id}> "
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )

        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.group_statements, #type: ignore # noqa: PGH003
        group_id=payload.request.group_id,
        approver_email=approver.email,
        requester_email=requester.email,
    )

    logger.info("Decision on request was made", extra={"decision": decision})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=payload.message["blocks"],
        color_coding_emoji=cfg.good_result_emoji,
    )

    blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())
    client.chat_update(
        channel=payload.channel_id,
        ts=payload.thread_ts,
        blocks=blocks,
        text=text,
    )

    execute_decision_on_group_request(
        decision=decision,
        group = sso.describe_group(identity_store_id, payload.request.group_id, identity_store_client),
        user_principal_id = sso.get_user_principal_id_by_email(identity_store_client, sso_instance.identity_store_id, requester.email),
        permission_duration=payload.request.permission_duration,
        approver=approver,
        requester=requester,
        reason=payload.request.reason,
    )
    cache_for_dublicate_requests.clear()
    return client.chat_postMessage(
        channel=payload.channel_id,
        text=text,
        thread_ts=payload.thread_ts,
    )





# Access control
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----

def execute_decision_on_group_request(  # noqa: PLR0913
    decision: AccessRequestDecision | ApproveRequestDecision,
    group: entities.aws.SSOGroup,
    user_principal_id: str,
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
) -> bool:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    if not sso.is_user_in_group(
        identity_store_id = identity_store_id,
        group_id = group.id,
        sso_user_id = user_principal_id,
        identity_store_client = identity_store_client,
        ):

        responce = sso.add_user_to_a_group(group.id, user_principal_id, identity_store_id, identity_store_client)

    logger.info("User added to the group", extra={"group_id": group.id, "user_id": user_principal_id, })

    s3.log_operation(
        audit_entry=s3.GroupAccessAuditEntry(
            group_name = group.name,
            group_id = group.id,
            membership_id = responce["MembershipId"],
            reason = reason,
            requester_slack_id = requester.id,
            requester_email = requester.email,
            approver_slack_id = "NA",
            approver_email = "NA",
            operation_type = "grant",
            permission_duration = permission_duration,
            audit_entry_type = "group",
            user_principal_id = ""
            ),
        )

    schedule.schedule_group_revoke_event(
            permission_duration=permission_duration,
            schedule_client=schedule_client,
            approver=approver,
            requester=requester,
            group_assignment=sso.GroupAssignment(
                identity_store_id=identity_store_id,
                group_name=group.name,
                group_id=group.id,
                user_principal_id=user_principal_id,
                membership_id=responce["MembershipId"],
            ),
        )
    return# type: ignore # noqa: PGH003


#Slack
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----



class RequestForGroupAccess(entities.BaseModel):
    group_id: str
    reason: str
    requester_slack_id: str
    permission_duration: timedelta

class RequestForGroupAccessView:
    __name__ = "RequestForGroupAccessView"
    CALLBACK_ID = "request_for_group_access_submitted"

    REASON_BLOCK_ID = "provide_reason"
    REASON_ACTION_ID = "provided_reason"

    GROUP_BLOCK_ID = "select_group"
    GROUP_ACTION_ID = "selected_group"

    DURATION_BLOCK_ID = "duration_picker"
    DURATION_ACTION_ID = "duration_picker_action"

    LOADING_BLOCK_ID = "loading"

    @classmethod
    def build(cls) -> View: # noqa: ANN102
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            submit=PlainTextObject(text="Request"),
            close=PlainTextObject(text="Cancel"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[
                SectionBlock(text=MarkdownTextObject(text=":wave: Hey! Please fill form below to request access to AWS SSO group.")),
                DividerBlock(),
                SectionBlock(
                    block_id=cls.DURATION_BLOCK_ID,
                    text=MarkdownTextObject(text="Select the duration for which the access will be provided"),
                    accessory=StaticSelectElement(
                        action_id=cls.DURATION_ACTION_ID,
                        initial_option=slack_helpers.get_max_duration_block(cfg)[0],
                        options=slack_helpers.get_max_duration_block(cfg),
                        placeholder=PlainTextObject(text="Select duration"),
                    ),
                ),
                InputBlock(
                    block_id=cls.REASON_BLOCK_ID,
                    label=PlainTextObject(text="Why do you need access?"),
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
    def update_with_groups(
        cls, groups: list[entities.aws.SSOGroup] # noqa: ANN102
    ) -> View:
        view = cls.build()
        view.blocks = slack_helpers.remove_blocks(view.blocks, block_ids=[cls.LOADING_BLOCK_ID])
        view.blocks = slack_helpers.insert_blocks(
            blocks=view.blocks,
            blocks_to_insert=[
                cls.build_select_group_input_block(groups),
            ],
            after_block_id=cls.REASON_BLOCK_ID,
        )
        return view

    @classmethod
    def build_select_group_input_block(cls, groups: list[entities.aws.SSOGroup]) -> InputBlock: # noqa: ANN102
        # TODO: handle case when there are more than 100 groups
        # 99 is the limit for StaticSelectElement
        # https://slack.dev/python-slack-sdk/api-docs/slack_sdk/models/blocks/block_elements.html#:~:text=StaticSelectElement(InputInteractiveElement)%3A%0A%20%20%20%20type%20%3D%20%22static_select%22-,options_max_length%20%3D%20100,-option_groups_max_length%20%3D%20100%0A%0A%20%20%20%20%40property%0A%20%20%20%20def%20attributes(
        if len(groups) > 99:  # noqa: PLR2004
            groups = groups[:99]
        sorted_groups = sorted(groups, key=lambda groups: groups.name)
        return InputBlock(
            block_id=cls.GROUP_BLOCK_ID,
            label=PlainTextObject(text="Select group"),
            element=StaticSelectElement(
                action_id=cls.GROUP_ACTION_ID,
                placeholder=PlainTextObject(text="Select group"),
                options=[
                    Option(text=PlainTextObject(text=f"{group.name}"), value=group.id) for group in sorted_groups
                ],
            ),
        )

    @classmethod
    def parse(cls, obj: dict) -> RequestForGroupAccess:# noqa: ANN102
        values = jp.search("view.state.values", obj)
        hhmm = jp.search(f"{cls.DURATION_BLOCK_ID}.{cls.DURATION_ACTION_ID}.selected_option.value", values)
        hours, minutes = map(int, hhmm.split(":"))
        duration = timedelta(hours=hours, minutes=minutes)
        return RequestForGroupAccess.parse_obj(
            {
                "permission_duration": duration,
                "group_id": jp.search(f"{cls.GROUP_BLOCK_ID}.{cls.GROUP_ACTION_ID}.selected_option.value", values),
                "reason": jp.search(f"{cls.REASON_BLOCK_ID}.{cls.REASON_ACTION_ID}.value", values),
                "requester_slack_id": jp.search("user.id", obj),
            }
        )


class ButtonGroupClickedPayload(BaseModel):
    action: entities.ApproverAction
    approver_slack_id: str
    thread_ts: str
    channel_id: str
    message: dict
    request: RequestForGroupAccess

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        fields = jp.search("message.blocks[?block_id == 'content'].fields[]", values)
        requester_mention = cls.find_in_fields(fields, "Requester")
        requester_slack_id = requester_mention.removeprefix("<@").removesuffix(">")
        humanized_permission_duration = cls.find_in_fields(fields, "Permission duration")
        permission_duration = unhumanize_timedelta(humanized_permission_duration)
        group = cls.find_in_fields(fields, "Group")
        group_id = group.split("#")[-1]
        return {
            "action": jp.search("actions[0].value", values),
            "approver_slack_id": jp.search("user.id", values),
            "thread_ts": jp.search("message.ts", values),
            "channel_id": jp.search("channel.id", values),
            "message": values.get("message"),
            "request": RequestForGroupAccess(
                requester_slack_id=requester_slack_id,
                group_id=group_id,
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
