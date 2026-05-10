import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
import boto3
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_sso_admin import SSOAdminClient
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from slack_sdk.web.slack_response import SlackResponse

import access_control
import config
import entities
import request_store
import schedule
import sso
from entities.elevator_request import ElevatorRequestKind, ElevatorRequestRecord, ElevatorRequestStatus
from entities.teams import TeamsUser
from errors import handle_errors
from requester.slack import slack_helpers
from requester.teams import teams_activity_helpers, teams_cards
from requester.teams.teams_threading import ChannelThreadContext

logger = config.get_logger(service="main")
cfg = config.get_config()

# Lazy AWS clients — initialized on first use, not at import time.
_sso_client: SSOAdminClient | None = None
_identity_store_client: IdentityStoreClient | None = None
_schedule_client: EventBridgeSchedulerClient | None = None
_identity_store_id: str | None = None


def _get_sso_client() -> SSOAdminClient:
    global _sso_client  # noqa: PLW0603
    if _sso_client is None:
        _sso_client = boto3.client("sso-admin")  # type: ignore[assignment]
    return _sso_client


def _get_identity_store_client() -> IdentityStoreClient:
    global _identity_store_client  # noqa: PLW0603
    if _identity_store_client is None:
        _identity_store_client = boto3.client("identitystore")  # type: ignore[assignment]
    return _identity_store_client


def _get_schedule_client() -> EventBridgeSchedulerClient:
    global _schedule_client  # noqa: PLW0603
    if _schedule_client is None:
        _schedule_client = boto3.client("scheduler")  # type: ignore[assignment]
    return _schedule_client


def _get_identity_store_id() -> str:
    global _identity_store_id  # noqa: PLW0603
    if _identity_store_id is None:
        sso_instance = sso.describe_sso_instance(_get_sso_client(), cfg.sso_instance_arn)
        _identity_store_id = sso_instance.identity_store_id
    return _identity_store_id


@handle_errors
def handle_request_for_group_access_submittion(  # noqa: PLR0912, PLR0915
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> SlackResponse | None:
    logger.info("Handling request for access submission")
    request = slack_helpers.RequestForGroupAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)

    group = sso.describe_group(_get_identity_store_id(), request.group_id, _get_identity_store_client())

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=requester.email,
        group_id=request.group_id,
    )

    elevator_id = str(uuid.uuid4())
    request_store.put_access_request(
        ElevatorRequestRecord(
            elevator_request_id=elevator_id,
            kind=ElevatorRequestKind.group,
            status=ElevatorRequestStatus.awaiting_approval,
            requester_slack_id=request.requester_slack_id,
            requester_display_name=(requester.real_name or "").strip() or None,
            requester_email=(requester.email or "").strip() or None,
            reason=request.reason,
            permission_duration_seconds=int(request.permission_duration.total_seconds()),
            group_id=request.group_id,
        )
    )

    show_buttons = bool(decision.approvers)
    slack_response = client.chat_postMessage(
        blocks=slack_helpers.build_approval_request_message_blocks(
            sso_client=_get_sso_client(),
            identity_store_client=_get_identity_store_client(),
            slack_client=client,
            requester_slack_id=request.requester_slack_id,
            group=group,
            reason=request.reason,
            permission_duration=request.permission_duration,
            show_buttons=show_buttons,
            color_coding_emoji=cfg.waiting_result_emoji,
            elevator_request_id=elevator_id,
        ),
        channel=cfg.slack_channel_id,
        text=f"Request for access to {group.name} group from {requester.real_name}",
    )
    if slack_response.get("ts") is not None:
        request_store.update_slack_presentation(elevator_id, cfg.slack_channel_id, str(slack_response["ts"]))

    if show_buttons:
        ts = slack_response["ts"]
        if ts is not None:
            schedule.schedule_discard_buttons_event(
                schedule_client=_get_schedule_client(),  # type: ignore # noqa: PGH003
                time_stamp=ts,
                channel_id=cfg.slack_channel_id,
                elevator_request_id=elevator_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=_get_schedule_client(),  # type: ignore # noqa: PGH003
                message_ts=ts,
                channel_id=cfg.slack_channel_id,
                time_to_wait=timedelta(
                    minutes=cfg.approver_renotification_initial_wait_time,
                ),
                elevator_request_id=elevator_id,
            )

    match decision.reason:
        case access_control.DecisionReason.ApprovalNotRequired:
            text = "Approval for this Group is not required. Your request was approved automatically."
            dm_text = "Approval for this Group is not required. Your request was approved automatically."
        case access_control.DecisionReason.SelfApproval:
            text = "Self approval is allowed and you are listed as an approver. Your request was approved automatically."
            dm_text = "Self approval is allowed and you are listed as an approver. Your request was approved automatically."
        case access_control.DecisionReason.RequiresApproval:
            approvers, approver_emails_not_found = slack_helpers.find_approvers_in_slack(
                client,
                decision.approvers,  # type: ignore # noqa: PGH003
            )
            if not approvers:
                text = """
                None of the approvers from configuration could be found in Slack.
                Request cannot be processed. Please discard the request and check the module configuration.
                """
                dm_text = """
                Your request cannot be processed because none of the approvers from configuration could be found in Slack.
                Please discard the request and check the module configuration.
                """
                color_coding_emoji = cfg.bad_result_emoji
            else:
                mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
                text = f"{mention_approvers} there is a request waiting for the approval."
                if approver_emails_not_found:
                    missing_emails = ", ".join(approver_emails_not_found)
                    text += f"""
                    Note: Some approvers ({missing_emails}) could not be found in Slack.
                    Please discard the request and check the module configuration.
                    """
                dm_text = f"Your request is waiting for the approval from {mention_approvers}."
                color_coding_emoji = cfg.waiting_result_emoji
        case access_control.DecisionReason.NoApprovers:
            text = "Nobody can approve this request."
            dm_text = "Nobody can approve this request."
            color_coding_emoji = cfg.bad_result_emoji
        case access_control.DecisionReason.NoStatements:
            text = "There are no statements for this Group."
            dm_text = "There are no statements for this Group."
            color_coding_emoji = cfg.bad_result_emoji

    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    logger.info(f"Sending message to the channel {cfg.slack_channel_id}, message: {text}")
    client.chat_postMessage(text=text, thread_ts=slack_response["ts"], channel=cfg.slack_channel_id)
    if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
        logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
        client.chat_postMessage(
            channel=requester.id,
            text=f"""
            {dm_text} You are receiving this message in a DM because you are not a member of the channel <#{cfg.slack_channel_id}>.
            """,
        )

    is_auto_grant = decision.reason in (
        access_control.DecisionReason.ApprovalNotRequired,
        access_control.DecisionReason.SelfApproval,
    )
    if not is_auto_grant:
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

    try:
        access_control.execute_decision_on_group_request(
            group=group,
            permission_duration=request.permission_duration,
            approver=requester,
            requester=requester,
            reason=request.reason,
            decision=decision,
            identity_store_id=_get_identity_store_id(),
            elevator_request_id=elevator_id,
        )
    except Exception as e:
        if is_auto_grant:
            try:
                blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
                    blocks=slack_response["message"]["blocks"],
                    color_coding_emoji=cfg.bad_result_emoji,
                )
                client.chat_update(
                    channel=cfg.slack_channel_id,
                    ts=slack_response["ts"],
                    blocks=blocks,
                    text="Request processing failed. Please check logs for details.",
                )
            except Exception as update_err:
                logger.exception(f"Failed to update Slack message after auto-grant failure: {update_err}")
        raise e

    if is_auto_grant and decision.grant:
        try:
            blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
                blocks=slack_response["message"]["blocks"],
                color_coding_emoji=cfg.good_result_emoji,
            )
            client.chat_update(
                channel=cfg.slack_channel_id,
                ts=slack_response["ts"],
                blocks=blocks,
                text=text,
            )
        except Exception as update_err:
            logger.exception(f"Failed to update Slack message after successful auto-grant: {update_err}")
    if decision.grant and elevator_id:
        request_store.update_request_status(elevator_id, ElevatorRequestStatus.completed)

    if decision.grant:
        grant_notice = f"Permissions have been granted by <@{requester.id}>."
        client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=grant_notice,
            thread_ts=slack_response["ts"],
        )
        if not is_user_in_channel and cfg.send_dm_if_user_not_in_channel:
            client.chat_postMessage(channel=requester.id, text=grant_notice)


@handle_errors
def handle_group_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse:  # type: ignore # noqa: PGH003 ARG001
    logger.info("Handling button click")
    payload = slack_helpers.ButtonGroupClickedPayload.model_validate(body)
    logger.info("Button click payload", extra={"payload": payload})
    approver = slack_helpers.get_user(client, id=payload.approver_slack_id)
    requester = slack_helpers.get_user(client, id=payload.request.requester_slack_id)
    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    if not request_store.try_begin_in_flight_approval(
        requester_slack_id=payload.request.requester_slack_id,
        account_id=None,
        permission_set_name=None,
        group_id=payload.request.group_id,
    ):
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> request is already in progress, please wait for the result.",
            thread_ts=payload.thread_ts,
        )

    if payload.action == entities.ApproverAction.Discard:
        blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
            blocks=payload.message["blocks"],
            color_coding_emoji=cfg.bad_result_emoji,
        )

        blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
        blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

        text = f"Request was discarded by<@{approver.id}> "
        dm_text = f"Your request was discarded by <@{approver.id}>."
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )
        if payload.elevator_request_id:
            request_store.update_request_status(payload.elevator_request_id, ElevatorRequestStatus.discarded)

        request_store.end_in_flight_approval(
            requester_slack_id=payload.request.requester_slack_id,
            account_id=None,
            permission_set_name=None,
            group_id=payload.request.group_id,
        )
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.group_statements,  # type: ignore # noqa: PGH003
        group_id=payload.request.group_id,
        approver_email=approver.email,
        requester_email=requester.email,
    )

    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        request_store.end_in_flight_approval(
            requester_slack_id=payload.request.requester_slack_id,
            account_id=None,
            permission_set_name=None,
            group_id=payload.request.group_id,
        )
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    dm_text = f"Your request was approved by <@{approver.id}>. Permissions granted."
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

    access_control.execute_decision_on_group_request(
        decision=decision,
        group=sso.describe_group(_get_identity_store_id(), payload.request.group_id, _get_identity_store_client()),
        permission_duration=payload.request.permission_duration,
        approver=approver,
        requester=requester,
        reason=payload.request.reason,
        identity_store_id=_get_identity_store_id(),
        elevator_request_id=payload.elevator_request_id,
    )
    if payload.elevator_request_id:
        request_store.update_request_status(payload.elevator_request_id, ElevatorRequestStatus.completed)
    request_store.end_in_flight_approval(
        requester_slack_id=payload.request.requester_slack_id,
        account_id=None,
        permission_set_name=None,
        group_id=payload.request.group_id,
    )
    if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
        logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
        client.chat_postMessage(channel=requester.id, text=dm_text)
    return client.chat_postMessage(
        channel=payload.channel_id,
        text=text,
        thread_ts=payload.thread_ts,
    )


async def handle_teams_group_task_submit(  # noqa: PLR0912, PLR0915
    turn_context,  # noqa: ANN001
    data: dict,
    user: TeamsUser,
) -> dict:
    """Parse group task/submit, run access control, post approval card, auto-execute if grant."""
    _thr = ChannelThreadContext.from_activity(turn_context.activity)
    t_conv, t_su, t_par = _thr.account_approval_fields()
    su_from_act = str(getattr(turn_context.activity, "service_url", None) or "").strip() or None
    su_effective = (t_su or "").strip() or su_from_act
    store_conv = (t_conv or "").strip() or cfg.teams_approval_conversation_id
    launcher_activity_id = teams_activity_helpers.launcher_activity_id_for_task_submit(turn_context)

    permission_duration = teams_cards.parse_duration_choice(str(data.get("duration", "1:00:00")))
    reason = str(data.get("reason", ""))
    group_id = str(data.get("group_id", ""))
    elevator_id = str(uuid.uuid4())
    slack_user = user.to_slack_user()

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=user.email,
        group_id=group_id,
    )

    request_store.put_access_request(
        ElevatorRequestRecord(
            elevator_request_id=elevator_id,
            kind=ElevatorRequestKind.group,
            status=ElevatorRequestStatus.awaiting_approval,
            requester_slack_id=user.id,
            requester_display_name=(user.display_name or "").strip() or None,
            requester_email=(user.email or "").strip() or None,
            reason=reason,
            permission_duration_seconds=int(permission_duration.total_seconds()),
            group_id=group_id,
        )
    )

    if decision.grant:
        try:
            access_control.execute_decision_on_group_request(
                decision=decision,
                group=sso.describe_group(_get_identity_store_id(), group_id, _get_identity_store_client()),
                permission_duration=permission_duration,
                approver=slack_user,
                requester=slack_user,
                reason=reason,
                identity_store_id=_get_identity_store_id(),
                elevator_request_id=elevator_id,
            )
            request_store.update_request_status(elevator_id, ElevatorRequestStatus.completed)
        except Exception as e:
            logger.exception(f"Failed to execute auto-approved group decision: {e}")

    # Posting to the approval channel is deferred (async self-invoke) so task/submit returns under Teams'
    # client timeout (~15s). Local/tests without AWS_LAMBDA_FUNCTION_NAME run the post in-process.
    if not (t_conv or "").strip() and not (store_conv or "").strip():
        logger.warning("Group task submit: no conversation id on activity; skip posting approval card to Teams")
    else:
        from requester.teams.teams_approval_deferred import AccountApprovalTeamsThread
        from requester.teams.teams_approval_deferred import invoke_group_approval_post_async, post_group_approval_to_teams_channel
        from requester.teams.teams_deps import TeamsDependencies
        from requester.common.context import get_requester_context
        import os

        # Best-effort: use deps from runtime context for local in-process posts.
        tthr = AccountApprovalTeamsThread(
            conversation_id=(t_conv or "").strip() or (store_conv or "").strip(),
            service_url=(su_effective or "").strip() if su_effective else "",
            parent_activity_id=(t_par or "").strip(),
            launcher_activity_id=(launcher_activity_id or "").strip(),
        )
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            try:
                invoke_group_approval_post_async(
                    elevator_id,
                    user.display_name,
                    user.email,
                    tthr,
                )
            except Exception as e:
                logger.exception(f"Failed to schedule Teams group approval post: {e}")
        else:
            try:
                ctx = get_requester_context()
                deps = TeamsDependencies(
                    cfg=ctx.cfg,
                    org_client=ctx.org_client,
                    s3_client=ctx.s3_client,
                    sso_client=ctx.sso_client,
                    identity_store_client=ctx.identity_store_client,
                    schedule_client=ctx.schedule_client,
                )
                await post_group_approval_to_teams_channel(deps, elevator_id, user.display_name, user.email, tthr)
            except Exception as e:
                logger.exception(f"Failed to post approval card to Teams channel (group deferred inline): {e}")

    await teams_activity_helpers.update_teams_launcher_message_after_task_submit(turn_context, "group")

    return {"task": {"type": "message", "value": "Your request has been submitted."}}


async def handle_teams_group_card_action(  # noqa: PLR0915, PLR0913
    turn_context,  # noqa: ANN001
    rec: ElevatorRequestRecord,
    approver: TeamsUser,
    elevator_request_id: str,
    action: str,
    update_approval_card: Callable[..., Awaitable[None]],
) -> None:
    """Handle Approve/Discard on a Teams group approval card (Slack: handle_group_button_click)."""
    re_email = (rec.requester_email or "").strip()
    requester_slack = entities.slack.User(
        id=rec.requester_slack_id,
        email=re_email,
        real_name=(rec.requester_display_name or rec.requester_slack_id or "").strip() or rec.requester_slack_id,
    )
    permission_duration = timedelta(seconds=rec.permission_duration_seconds)
    approver_action = entities.ApproverAction.Approve if action == "approve" else entities.ApproverAction.Discard

    if approver_action == entities.ApproverAction.Discard:
        request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.discarded)
        request_store.end_in_flight_approval(
            requester_slack_id=rec.requester_slack_id,
            account_id=None,
            permission_set_name=None,
            group_id=rec.group_id,
        )
        await update_approval_card(
            turn_context=turn_context,
            elevator_request_id=elevator_request_id,
            decision_action="discarded",
            color_style=teams_cards.get_color_style(cfg.bad_result_emoji),
            decision_by=approver.display_name,
        )
        await teams_activity_helpers.teams_send_text_with_user_mention(
            turn_context,
            text_before_mention="Request was discarded by ",
            text_after_mention=".",
            user_id=approver.id,
            display_name=approver.display_name,
        )
        return

    decision = access_control.make_decision_on_approve_request(
        action=approver_action,
        statements=cfg.group_statements,  # type: ignore[arg-type]
        group_id=rec.group_id,
        approver_email=approver.email,
        requester_email=re_email,
    )

    if not decision.permit:
        request_store.end_in_flight_approval(
            requester_slack_id=rec.requester_slack_id,
            account_id=None,
            permission_set_name=None,
            group_id=rec.group_id,
        )
        await teams_activity_helpers.teams_send_text_with_user_mention(
            turn_context,
            text_before_mention="",
            text_after_mention=", you cannot approve this request.",
            user_id=approver.id,
            display_name=approver.display_name,
        )
        return

    await update_approval_card(
        turn_context=turn_context,
        elevator_request_id=elevator_request_id,
        decision_action="approved",
        color_style=teams_cards.get_color_style(cfg.good_result_emoji),
        decision_by=approver.display_name,
    )

    try:
        sso_group = sso.describe_group(_get_identity_store_id(), rec.group_id, _get_identity_store_client())
        access_control.execute_decision_on_group_request(
            decision=decision,
            group=sso_group,
            permission_duration=permission_duration,
            approver=approver.to_slack_user(),
            requester=requester_slack,
            reason=rec.reason,
            identity_store_id=_get_identity_store_id(),
            elevator_request_id=elevator_request_id,
        )
        request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.completed)
        await teams_activity_helpers.teams_send_text_with_user_mention(
            turn_context,
            text_before_mention="Permissions have been granted by ",
            text_after_mention=".",
            user_id=approver.id,
            display_name=approver.display_name,
        )
    except Exception as e:
        logger.exception(f"Failed to execute group decision in handle_teams_group_card_action: {e}")
    finally:
        request_store.end_in_flight_approval(
            requester_slack_id=rec.requester_slack_id,
            account_id=None,
            permission_set_name=None,
            group_id=rec.group_id,
        )
