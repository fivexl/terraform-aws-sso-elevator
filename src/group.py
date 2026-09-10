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
import schedule
import slack_helpers
import sso
from errors import handle_errors

logger = config.get_logger(service="main")
cfg = config.get_config()

session = boto3._get_default_session()
sso_client: SSOAdminClient = session.client("sso-admin")
identity_store_client: IdentityStoreClient = session.client("identitystore")
schedule_client: EventBridgeSchedulerClient = session.client("scheduler")
sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
identity_store_id = sso_instance.identity_store_id


def _group_access_decision_messages(  # noqa: PLR0911
    client: WebClient,
    decision: access_control.AccessRequestDecision,
) -> tuple[str, str, str]:
    match decision.reason:
        case access_control.DecisionReason.ApprovalNotRequired:
            return (
                "Approval for this Group is not required. Request will be approved automatically.",
                "Approval for this Group is not required. Your request will be approved automatically.",
                cfg.good_result_emoji,
            )
        case access_control.DecisionReason.SelfApproval:
            return (
                "Self approval is allowed and requester is an approver. Request will be approved automatically.",
                "Self approval is allowed and you are an approver. Your request will be approved automatically.",
                cfg.good_result_emoji,
            )
        case access_control.DecisionReason.RequiresApproval:
            approvers, approver_emails_not_found = slack_helpers.find_approvers_in_slack(
                client,
                decision.approvers,  # type: ignore # noqa: PGH003
            )
            if not approvers:
                return (
                    """
                None of the approvers from configuration could be found in Slack.
                Request cannot be processed. Please discard the request and check the module configuration.
                """,
                    """
                Your request cannot be processed because none of the approvers from configuration could be found in Slack.
                Please discard the request and check the module configuration.
                """,
                    cfg.bad_result_emoji,
                )
            mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
            text = f"{mention_approvers} there is a request waiting for the approval."
            if approver_emails_not_found:
                missing_emails = ", ".join(approver_emails_not_found)
                text += f"""
                    Note: Some approvers ({missing_emails}) could not be found in Slack.
                    Please discard the request and check the module configuration.
                    """
            return (
                text,
                f"Your request is waiting for the approval from {mention_approvers}.",
                cfg.waiting_result_emoji,
            )
        case access_control.DecisionReason.NoApprovers:
            return (
                "Nobody can approve this request.",
                "Nobody can approve this request.",
                cfg.bad_result_emoji,
            )
        case access_control.DecisionReason.NoStatements:
            return (
                "There are no statements for this Group.",
                "There are no statements for this Group.",
                cfg.bad_result_emoji,
            )
        case access_control.DecisionReason.RequesterNotAllowed:
            return (
                "Requester is not allowed to request access to this Group.",
                "You are not allowed to request access to this Group.",
                cfg.bad_result_emoji,
            )


@handle_errors
def handle_request_for_group_access_submittion(
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> SlackResponse | None:
    logger.info("Handling request for access submission")
    request = slack_helpers.RequestForGroupAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)

    group = sso.describe_group(identity_store_id, request.group_id, identity_store_client)

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=requester.email,
        group_id=request.group_id,
        requester_group_ids=access_control.get_requester_group_ids_if_needed(cfg.group_statements, requester.email),
    )

    show_buttons = bool(decision.approvers)
    slack_response = client.chat_postMessage(
        blocks=slack_helpers.build_approval_request_message_blocks(
            sso_client=sso_client,
            identity_store_client=identity_store_client,
            slack_client=client,
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
                schedule_client=schedule_client,  # type: ignore # noqa: PGH003
                time_stamp=ts,
                channel_id=cfg.slack_channel_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=schedule_client,  # type: ignore # noqa: PGH003
                message_ts=ts,
                channel_id=cfg.slack_channel_id,
                time_to_wait=timedelta(
                    minutes=cfg.approver_renotification_initial_wait_time,
                ),
            )

    text, dm_text, color_coding_emoji = _group_access_decision_messages(client, decision)

    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    # execute_decision_on_group_request runs before every notification below
    # (#194 A3, mirroring main.py's process_access_request) -- not after,
    # with the message already recolored to reflect an auto-grant decision.
    # For ApprovalNotRequired/SelfApproval, text/color_coding_emoji above are
    # already the "will be approved automatically" / good_result_emoji
    # wording purely from the *decision*, before the grant has actually been
    # attempted. Posting any notification before running the real AWS calls
    # here means a failure (a stale group id, IAM Identity Center throttling,
    # anything) left every message permanently reading "will be approved
    # automatically" with no correction. For RequiresApproval, decision.grant
    # is still False here, so execute_decision_on_group_request's own
    # `if not decision.grant: return False` makes this a no-op -- this only
    # changes behavior for the two auto-grant reasons.
    grant_error: Exception | None = None
    try:
        access_control.execute_decision_on_group_request(
            group=group,
            permission_duration=request.permission_duration,
            approver=requester,
            requester=requester,
            reason=request.reason,
            decision=decision,
            identity_store_id=identity_store_id,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(
            "execute_decision_on_group_request failed -- overriding the message to reflect the actual outcome",
            extra={"decision": decision.dict()},
        )
        color_coding_emoji = cfg.bad_result_emoji
        text = f"An error occurred while granting access: {e}"
        dm_text = text

    # Everything below is best-effort, not re-raised: once the grant has
    # either succeeded (access is live) or definitively failed (captured as
    # grant_error above), a Slack API hiccup while posting/updating these
    # notifications must never be reported as "this failed" on top of a
    # grant that actually succeeded.
    try:
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

        if decision.grant and grant_error is None:
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"Permissions granted to <@{requester.id}>",
                thread_ts=slack_response["ts"],
            )
            if not is_user_in_channel and cfg.send_dm_if_user_not_in_channel:
                client.chat_postMessage(
                    channel=requester.id,
                    text="Your request was processed, permissions granted.",
                )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fully post/update notifications about this request's outcome (best-effort, not re-raised)")

    if grant_error is not None:
        raise grant_error


cache_for_dublicate_requests = {}


@handle_errors
def handle_group_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse | None:  # type: ignore # noqa: PGH003 ARG001 PLR0915
    logger.info("Handling button click")
    payload = slack_helpers.ButtonGroupClickedPayload.model_validate(body)
    logger.info("Button click payload", extra={"payload": payload})
    approver = slack_helpers.get_user(client, id=payload.approver_slack_id)
    requester = slack_helpers.get_user(client, id=payload.request.requester_slack_id)
    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    if (
        cache_for_dublicate_requests.get("requester_slack_id") == payload.request.requester_slack_id
        and cache_for_dublicate_requests["group_id"] == payload.request.group_id
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

        cache_for_dublicate_requests.clear()
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )

    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.group_statements, requester.email)
    cache_for_dublicate_requests["requester_slack_id"] = payload.request.requester_slack_id
    cache_for_dublicate_requests["group_id"] = payload.request.group_id

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.group_statements,  # type: ignore # noqa: PGH003
        group_id=payload.request.group_id,
        approver_email=approver.email,
        requester_email=requester.email,
        requester_group_ids=requester_group_ids,
    )

    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    dm_text = f"Your request was approved by <@{approver.id}>. Permissions granted."

    # Buttons stripped *before* execute_decision_on_group_request runs, not
    # only afterward together with the rest of the outcome (#194 A2/A3):
    # cache_for_dublicate_requests is per-container in-memory state, so it
    # can't close this window by itself -- a second approver clicking
    # Approve while the grant is still in progress can land in a different,
    # fresh Lambda container that sees an empty cache and this message's
    # still-live buttons. Best-effort: a Slack hiccup here narrows the
    # closed window rather than eliminating it, but must not stop the
    # actual grant from being attempted.
    try:
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=slack_helpers.remove_blocks(payload.message["blocks"], block_ids=["buttons"]),
            text=f"<@{approver.id}> is processing this request...",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to strip buttons before granting (best-effort, not re-raised)")

    # execute_decision_on_group_request runs before the chat_update/
    # notifications below, not after: the old order recolored the message
    # green and said "Permissions granted" before the grant had actually
    # been attempted, so a failure here left that message incorrect with no
    # visible correction. Same shape as main.py's handle_button_click.
    grant_error: Exception | None = None
    try:
        access_control.execute_decision_on_group_request(
            decision=decision,
            group=sso.describe_group(identity_store_id, payload.request.group_id, identity_store_client),
            permission_duration=payload.request.permission_duration,
            approver=approver,
            requester=requester,
            reason=payload.request.reason,
            identity_store_id=identity_store_id,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(
            "execute_decision_on_group_request failed -- overriding the message to reflect the actual outcome",
            extra={"decision": decision.dict()},
        )
        text = f"An error occurred while granting access: {e}"
        dm_text = text

    # The dedup cache is cleared once the outcome is decided (success or a
    # caught failure), not only on the success path -- otherwise a failed
    # execute_decision_on_group_request left this exact request permanently
    # stuck reporting "already in progress" to any retry.
    cache_for_dublicate_requests.clear()

    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=payload.message["blocks"],
        color_coding_emoji=cfg.bad_result_emoji if grant_error is not None else cfg.good_result_emoji,
    )
    blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

    # Best-effort from here, not re-raised: once execute_decision_on_group_request
    # has either succeeded (access is live) or definitively failed (captured
    # as grant_error above), a Slack API hiccup while posting/updating these
    # notifications must never be reported as "this failed" on top of a
    # grant that actually succeeded.
    result: SlackResponse | None = None
    try:
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        result = client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fully post/update notifications about this approval's outcome (best-effort, not re-raised)")

    if grant_error is not None:
        raise grant_error

    return result
