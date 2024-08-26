import datetime
import functools
import json
from datetime import datetime, timedelta

import boto3
import jmespath as jp
from aws_lambda_powertools import Logger
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from mypy_boto3_scheduler import type_defs as scheduler_type_defs
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import BaseModel, root_validator
from slack_bolt import Ack, App, BoltContext
from slack_bolt.adapter.socket_mode import SocketModeHandler
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

import access_control
import config
import entities
import errors
import events
import s3
import schedule
import slack_helpers
import sso
from entities import BaseModel
from slack_helpers import unhumanize_timedelta
from access_control import AccessRequestDecision, ApproveRequestDecision
import creds

# temporary mock
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----

logger = config.get_logger(service="main")

def error_handler(client: WebClient, e: Exception, logger: Logger, context: BoltContext) -> None:
    logger.exception(e)
    if isinstance(e, errors.ConfigurationError):
        text = f"<@{context['user_id']}> Your request for AWS permissions failed with error: {e}. Check logs for more details."
    else:
        text = f"<@{context['user_id']}> Your request for AWS permissions failed with error. Check access-requester logs for more details."

    client.chat_postMessage(text=text, channel=cfg.slack_channel_id)


def handle_errors(fn):  # noqa: ANN001, ANN201
    # Default slack error handler (app.error) does not handle all exceptions. Or at least I did not find how to do it.
    # So I created this error handler.
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            client: WebClient = kwargs["client"]
            context: BoltContext = kwargs["context"]
            error_handler(client=client, e=e, logger=logger, context=context)

    return wrapper



cfg = config.get_config()
app = App(token=creds.bot_token)




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
def get_all_groups(identity_store_id, identity_store_client: IdentityStoreClient) -> list[entities.aws.SSOGroup]: # noqa: ANN102 ANN001
    groups = []
    for page in identity_store_client.get_paginator("list_groups").paginate(IdentityStoreId=identity_store_id):
        groups.extend(
            entities.aws.SSOGroup(
                id=group.get("GroupId"),
                identity_store_id=group.get("IdentityStoreId"),
                name=group.get("DisplayName"),  # type: ignore # noqa: PGH003
                description=group.get("Description"),
            )
            for group in page["Groups"]
            if group.get("DisplayName") and group.get("GroupId")
        )
    # TODO: handle case when there are no groups
    logger.info("Got information about all groups.")
    logger.debug("Groups", extra={"groups": groups})
    return groups

@handle_errors
def add_user_to_a_group(sso_group_id, sso_user_id, identity_store_id, identity_store_client:IdentityStoreClient):  # noqa: ANN201 ANN001
    responce = identity_store_client.create_group_membership(
        GroupId=sso_group_id,
        MemberId= {"UserId": sso_user_id},
        IdentityStoreId=identity_store_id
    )
    logger.info("User added to the group", extra={"group_id": sso_group_id, "user_id": sso_user_id, })
    return responce

@handle_errors
def remove_user_from_group(identity_store_id, membership_id, identity_store_client: IdentityStoreClient): # noqa: ANN201 ANN001
    responce = identity_store_client.delete_group_membership(IdentityStoreId=identity_store_id, MembershipId=membership_id)
    logger.info("User removed from the group", extra={"membership_id": membership_id})
    return responce

@handle_errors
def is_user_in_group(identity_store_id: str, group_id: str, sso_user_id: str, identity_store_client: IdentityStoreClient) -> str | None:
    paginator = identity_store_client.get_paginator("list_group_memberships")
    for page in paginator.paginate(IdentityStoreId=identity_store_id, GroupId=group_id):
        for group in page["GroupMemberships"]:
            try:
                if group["MemberId"]["UserId"] == sso_user_id: # type: ignore # noqa: PGH003
                    logger.info("User is in the group", extra={"group": group})
                    return group["MembershipId"] # type: ignore # noqa: PGH003 (ignoring this because we checked if user is in the group)
            except Exception as e:
                logger.error("Error while checking if user is in the group", extra={"error": e})
    return None

def describe_group(identity_store_id, group_id, identity_store_client: IdentityStoreClient) -> entities.aws.SSOGroup: # noqa: ANN201 ANN001
    group = identity_store_client.describe_group(IdentityStoreId=identity_store_id, GroupId=group_id)
    logger.info("Group described", extra={"group": group})
    return entities.aws.SSOGroup(
        id = group.get("GroupId"),
        identity_store_id = group.get("IdentityStoreId"),
        name = group.get("DisplayName"), # type: ignore # noqa: PGH003
        description = group.get("Description"),
    )

#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#Main

trigger_view_map = {}
@handle_errors
def show_initial_form(client: WebClient, body: dict, ack: Ack) -> SlackResponse | None:
    ack()
    logger.info("Showing initial form for group access")
    logger.debug("Request body", extra={"body": body})
    trigger_id = body["trigger_id"]
    response = client.views_open(trigger_id=trigger_id, view=RequestForGroupAccessView.build())
    trigger_view_map[trigger_id] = response.data["view"]["id"] # type: ignore # noqa: PGH003
    return response


@handle_errors
def load_select_options(client: WebClient, body: dict) -> SlackResponse:
    groups = get_all_groups(identity_store_id, identity_store_client)

    trigger_id = body["trigger_id"]

    view = RequestForGroupAccessView.update_with_groups(groups=groups)
    return client.views_update(view_id=trigger_view_map[trigger_id], view=view)

app.shortcut("request_for_group_membership")(
    show_initial_form,
    load_select_options,
)





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

    group = describe_group(identity_store_id, request.group_id, identity_store_client)

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=requester.email,
        group_id=request.group_id,
    )

    show_buttons = False # TODO: implement this
    slack_response = client.chat_postMessage(
        blocks=build_approval_request_message_blocks(
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

    execute_decision(
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
    slack_helpers.get_user(client, id=payload.request.requester_slack_id)

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

    # decision = access_control.make_decision_on_approve_request(
    #     action=payload.action,
    #     statements=cfg.statements,
    #     account_id=payload.request.account_id,
    #     permission_set_name=payload.request.permission_set_name,
    #     approver_email=approver.email,
    #     requester_email=requester.email,
    # )
    # logger.info("Decision on request was made", extra={"decision": decision})

    # if not decision.permit:
    #     cache_for_dublicate_requests.clear()
    #     return client.chat_postMessage(
    #         channel=payload.channel_id,
    #         text=f"<@{approver.id}> you can not approve this request",
    #         thread_ts=payload.thread_ts,
    #     )

    # text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    # blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
    #     blocks=payload.message["blocks"],
    #     color_coding_emoji=cfg.good_result_emoji,
    # )

    # blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
    # blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())
    # client.chat_update(
    #     channel=payload.channel_id,
    #     ts=payload.thread_ts,
    #     blocks=blocks,
    #     text=text,
    # )

    # access_control.execute_decision(
    #     decision=decision,
    #     permission_set_name=payload.request.permission_set_name,
    #     account_id=payload.request.account_id,
    #     permission_duration=payload.request.permission_duration,
    #     approver=approver,
    #     requester=requester,
    #     reason=payload.request.reason,
    # )
    # cache_for_dublicate_requests.clear()
    # return client.chat_postMessage(
    #     channel=payload.channel_id,
    #     text=text,
    #     thread_ts=payload.thread_ts,
    # )





# Access control
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----




def execute_decision(  # noqa: PLR0913
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


    responce = add_user_to_a_group(group.id, user_principal_id, identity_store_id, identity_store_client)
    logger.info("User added to the group", extra={"group_id": group.id, "user_id": user_principal_id, })

    s3.log_operation(
        audit_entry=s3.GroupAccessAuditEntry(
            group_name = group.name,
            group_id = group.id,
            membership_id = responce["MembershipId"],
            reason = reason,
            requester_slack_id = requester.id,
            requester_email = requester.email,
            approver_slack_id = "N/A",
            approver_email = "N/A",
            operation_type = "grant",
            permission_duration = permission_duration,
            audit_entry_type = "group",
            user_principal_id = ""
            ),
        )

    schedule_group_revoke_event(
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




#Schedule
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----



def schedule_group_revoke_event(
    schedule_client: EventBridgeSchedulerClient,
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    group_assignment: sso.GroupAssignment,
) -> scheduler_type_defs.CreateScheduleOutputTypeDef:
    logger.info("Scheduling revoke event")
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    revoke_event = events.GroupRevokeEvent(
        action="event_bridge_group_revoke",
        schedule_name=schedule_name,
        approver=approver,
        requester=requester,
        group_assignment= group_assignment,
        permission_duration=permission_duration,
    )
    schedule.get_and_delete_scheduled_revoke_event_if_already_exist(schedule_client, revoke_event)
    logger.debug("Creating schedule", extra={"revoke_event": revoke_event})
    return schedule_client.create_schedule(
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=schedule_name,
        GroupName=cfg.schedule_group_name,
        ScheduleExpression=schedule.event_bridge_schedule_after(permission_duration),
        State="ENABLED",
        Target=scheduler_type_defs.TargetTypeDef(
            Arn=cfg.revoker_function_arn,
            RoleArn=cfg.schedule_policy_arn,
            Input=json.dumps(
                {
                    "action": "event_bridge_group_revoke",
                    "revoke_event": revoke_event.json(),
                },
            ),
        ),
    )


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





def build_approval_request_message_blocks(  # noqa: PLR0913
    requester_slack_id: str,
    group: entities.aws.SSOGroup,
    reason: str,
    color_coding_emoji: str,
    permission_duration: timedelta,
    show_buttons: bool = True,
) -> list[Block]:
    blocks: list[Block] = [
        slack_helpers.HeaderSectionBlock.new(color_coding_emoji),
        SectionBlock(
            block_id="content",
            fields=[
                MarkdownTextObject(text=f"Requester: <@{requester_slack_id}>"),
                MarkdownTextObject(text=f"Group: {group.name} #{group.id}"),
                MarkdownTextObject(text=f"Reason: {reason}"),
                MarkdownTextObject(text=f"Permission duration: {slack_helpers.humanize_timedelta(permission_duration)}"),
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

#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----
#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----#-----


def acknowledge_request(ack: Ack):  # noqa: ANN201
    ack()

app.view(RequestForGroupAccessView.CALLBACK_ID)(
    ack=acknowledge_request,
    lazy=[handle_request_for_group_access_submittion],
)


if __name__ == "__main__":
    SocketModeHandler(app, creds.app_level_token).start()

