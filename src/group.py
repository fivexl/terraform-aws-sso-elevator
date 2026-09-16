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
import s3
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


@handle_errors
def handle_request_for_group_access_submittion(
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> None:
    logger.info("Handling request for access submission")
    request = slack_helpers.RequestForGroupAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)
    group = sso.describe_group(identity_store_id, request.group_id, identity_store_client)
    request = request.model_copy(update={"group_name": group.name})
    if rejection := slack_helpers.request_rejection(request):
        slack_helpers.dm_rejection(client, requester.id, rejection)
        return

    decision = access_control.make_decision_on_access_request(
        cfg.group_statements,
        requester_email=requester.email,
        group_id=request.group_id,
        requester_group_ids=access_control.get_requester_group_ids_if_needed(cfg.group_statements, requester.email),
    )

    _, secondary_domain_used = sso.get_user_principal_id_by_email(
        identity_store_client=identity_store_client, identity_store_id=identity_store_id, email=requester.email, cfg=cfg
    )
    card = slack_helpers.RequestCard.for_request(request, secondary_domain_used)
    outcome = slack_helpers.intake_outcome(client, decision, requester.id)

    text, blocks = slack_helpers.build_request_message(card, outcome.state)
    ts = client.chat_postMessage(channel=cfg.slack_channel_id, blocks=blocks, text=text)["ts"]
    dm_requester = slack_helpers.should_dm(client, requester.id)
    if outcome.thread_reply:
        slack_helpers.post_thread_reply(client, cfg.slack_channel_id, ts, outcome.thread_reply)
    if outcome.dm and dm_requester:
        slack_helpers.send_dm(client, requester.id, outcome.dm)
    if outcome.state.is_pending:
        schedule.schedule_discard_buttons_event(schedule_client=schedule_client, time_stamp=ts, channel_id=cfg.slack_channel_id)  # type: ignore # noqa: PGH003
        schedule.schedule_approver_notification_event(
            schedule_client=schedule_client,  # type: ignore # noqa: PGH003
            message_ts=ts,
            channel_id=cfg.slack_channel_id,
            time_to_wait=timedelta(minutes=cfg.approver_renotification_initial_wait_time),
        )

    # Called for denials too: one that ends the request is audited as "declined".
    # Granted before the outcome is shown, so a failure is never reported as success.
    replaced, grant_error = [], None
    try:
        replaced = access_control.execute_decision_on_group_request(
            group=group,
            permission_duration=request.permission_duration,
            approver=requester,
            requester=requester,
            reason=request.reason,
            decision=decision,
            identity_store_id=identity_store_id,
            channel_id=cfg.slack_channel_id,
            message_ts=ts,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(f"execute_decision_on_group_request failed: {e}", extra={"decision": decision.dict()})
    if not decision.grant:
        return
    slack_helpers.report_grant_outcome(
        client,
        channel_id=cfg.slack_channel_id,
        ts=ts,
        card=card,
        decided_by=slack_helpers.AUTO_APPROVAL_LABELS[decision.reason],
        auto=True,
        duration=request.permission_duration,
        replaced=replaced or [],
        error=grant_error,
        dm_requester=dm_requester,
    )


cache_for_dublicate_requests = {}


@handle_errors
def handle_group_button_click(payload: slack_helpers.ButtonClickedPayload, client: WebClient, context: BoltContext) -> SlackResponse | None:  # noqa: ARG001
    """Approve/Discard on a group request; main.handle_button_click routes here by the request's kind."""
    request: slack_helpers.RequestForGroupAccess = payload.request  # type: ignore # noqa: PGH003
    logger.info("Button click payload", extra={"payload": payload})
    approver = slack_helpers.get_user(client, id=payload.approver_slack_id)
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)
    dm_requester = slack_helpers.should_dm(client, requester.id)
    card = slack_helpers.RequestCard.from_message(payload.message, slack_helpers.request_subject(request), requester.id)

    if (
        cache_for_dublicate_requests.get("requester_slack_id") == request.requester_slack_id
        and cache_for_dublicate_requests.get("group_id") == request.group_id
    ):
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> request is already in progress, please wait for the result.",
            thread_ts=payload.thread_ts,
        )
    if payload.action == entities.ApproverAction.Discard:
        # Audited once the buttons are gone; see main.handle_button_click.
        if slack_helpers.discard_request(client, payload.channel_id, payload.thread_ts, card, approver.id, requester.id, dm_requester):
            s3.log_operation_best_effort(
                s3.AuditEntry(
                    group_id=request.group_id,
                    group_name=request.group_name or "NA",
                    reason=request.reason,
                    requester_slack_id=requester.id,
                    requester_email=requester.email,
                    approver_slack_id=approver.id,
                    approver_email=approver.email,
                    operation_type="declined",
                    permission_duration=request.permission_duration,
                    sso_user_principal_id="NA",
                    audit_entry_type="group",
                    decision_reason="Discarded",
                ),
            )
        cache_for_dublicate_requests.clear()
        return None

    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.group_statements, requester.email)
    cache_for_dublicate_requests["requester_slack_id"] = request.requester_slack_id
    cache_for_dublicate_requests["group_id"] = request.group_id

    # Every exit below clears the dedup cache; see main.handle_button_click.
    try:
        decision = access_control.make_decision_on_approve_request(
            action=payload.action,
            statements=cfg.group_statements,  # type: ignore # noqa: PGH003
            group_id=request.group_id,
            approver_email=approver.email,
            requester_email=requester.email,
            requester_group_ids=requester_group_ids,
        )
    except Exception:
        cache_for_dublicate_requests.clear()
        raise
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    # Buttons go before the grant runs; see main.handle_button_click (#194 A2).
    decided_by = slack_helpers.approved_by(approver.id)
    slack_helpers.update_request_message(
        client, payload.channel_id, payload.thread_ts, card, slack_helpers.RequestState.processing(decided_by)
    )

    replaced, grant_error = [], None
    try:
        replaced = access_control.execute_decision_on_group_request(
            decision=decision,
            group=sso.describe_group(identity_store_id, request.group_id, identity_store_client),
            permission_duration=request.permission_duration,
            approver=approver,
            requester=requester,
            reason=request.reason,
            identity_store_id=identity_store_id,
            channel_id=payload.channel_id,
            message_ts=payload.thread_ts,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(f"execute_decision_on_group_request failed: {e}", extra={"decision": decision.dict()})
    cache_for_dublicate_requests.clear()

    slack_helpers.report_grant_outcome(
        client,
        channel_id=payload.channel_id,
        ts=payload.thread_ts,
        card=card,
        decided_by=decided_by,
        auto=False,
        duration=request.permission_duration,
        replaced=replaced or [],
        error=grant_error,
        dm_requester=dm_requester,
    )
    return None
