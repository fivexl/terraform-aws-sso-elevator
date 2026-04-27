"""Defer posting the Teams approval card to a second Lambda invoke so task/submit returns before Teams' ~15s client timeout.

Also allows the requester to complete when outbound Bot Framework HTTPS is slow or blocked (NAT); the user still
gets an immediate "submitted" message; the card may follow on a separate invocation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import timedelta
from typing import Any

import access_control
import config
import entities
import organizations
import request_store
import schedule
from entities.elevator_request import ElevatorRequestKind
from requester.common.context import get_requester_context
from requester.teams.teams_deps import TeamsDependencies
from requester.teams.teams_notifier import TeamsNotifier
from requester.teams.teams_runtime import configure_teams_dependencies, get_teams_app

from . import teams_cards

log = config.get_logger(service="teams_approval_deferred")

ACCOUNT_APPROVAL_INTERNAL_ACTION = "teams_post_account_approval"


def _hmac_key() -> bytes:
    s = (os.environ.get("TEAMS_MICROSOFT_APP_PASSWORD") or "").encode()
    if not s:
        msg = "TEAMS_MICROSOFT_APP_PASSWORD is required to sign internal approval events"
        raise ValueError(msg)
    return s


def sign_account_approval_post(elevator_id: str, requester_email: str) -> str:
    body = f"v1|account|{elevator_id}|{requester_email.lower()}"
    return hmac.new(_hmac_key(), body.encode(), hashlib.sha256).hexdigest()


def verify_account_approval_post(elevator_id: str, requester_email: str, signature: str) -> bool:
    try:
        expect = sign_account_approval_post(elevator_id, requester_email)
    except ValueError:
        return False
    return hmac.compare_digest(expect, signature)


def invoke_account_approval_post_async(
    elevator_id: str,
    requester_display_name: str,
    requester_email: str,
) -> None:
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not fn:
        return
    import boto3

    pay: dict[str, Any] = {
        "internal_action": ACCOUNT_APPROVAL_INTERNAL_ACTION,
        "elevator_id": elevator_id,
        "requester_display_name": requester_display_name,
        "requester_email": requester_email,
        "hmac": sign_account_approval_post(elevator_id, requester_email),
    }
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps(pay).encode("utf-8"),
    )


async def post_account_approval_to_teams_channel(
    deps: TeamsDependencies,
    elevator_id: str,
    requester_display_name: str,
    requester_email: str,
) -> None:
    """Build approval card, post to teams_approval_conversation_id, update store, schedule follow-ups."""
    c = deps.cfg
    org_client = deps.org_client
    schedule_client = deps.schedule_client

    rec = request_store.get_access_request(elevator_id)
    if rec is None or rec.kind != ElevatorRequestKind.account:
        log.warning("Deferred post: missing or non-account request %s", elevator_id)
        return

    decision = access_control.make_decision_on_access_request(
        c.statements,
        requester_email=requester_email,
        account_id=rec.account_id or "",
        permission_set_name=rec.permission_set_name or "",
    )
    try:
        account = organizations.describe_account(org_client, rec.account_id or "")
    except Exception:
        account = entities.aws.Account(id=rec.account_id or "", name=rec.account_id or "")

    show_buttons = bool(decision.approvers)
    color_style = teams_cards.get_color_style(c.waiting_result_emoji)
    duration_str = str(timedelta(seconds=rec.permission_duration_seconds))
    request_data = {
        "account_id": rec.account_id or "",
        "permission_set": rec.permission_set_name or "",
        "duration": duration_str,
        "reason": rec.reason,
        "requester_id": rec.requester_slack_id,
    }
    card = teams_cards.build_approval_card(
        requester_name=requester_display_name,
        account=account,
        group=None,
        role_name=rec.permission_set_name or "",
        reason=rec.reason,
        permission_duration=duration_str,
        show_buttons=show_buttons,
        color_style=color_style,
        request_data=request_data,
        elevator_request_id=elevator_id,
    )

    def _notifier() -> TeamsNotifier:
        return TeamsNotifier(c, get_teams_app)

    try:
        notifier = _notifier()
        act_id = await notifier.send_message(text="New access request", card=card)
        if act_id:
            request_store.update_teams_presentation(elevator_id, c.teams_approval_conversation_id, act_id)
        if show_buttons:
            schedule.schedule_discard_buttons_event(
                schedule_client=schedule_client,
                time_stamp="",
                channel_id="",
                elevator_request_id=elevator_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=schedule_client,
                message_ts="",
                channel_id="",
                time_to_wait=timedelta(minutes=c.approver_renotification_initial_wait_time),
                elevator_request_id=elevator_id,
            )
    except Exception as e:
        log.exception("Failed to post approval card to Teams channel (deferred): %s", e)


async def run_post_account_approval_worker(event: dict[str, Any]) -> dict[str, Any]:
    """Second Lambda entry: HMAC + post card + schedule (invoked with InvocationType=Event)."""
    eid = str(event.get("elevator_id", ""))
    rname = str(event.get("requester_display_name", ""))
    email = str(event.get("requester_email", ""))
    sig = str(event.get("hmac", ""))
    if not eid or not email or not verify_account_approval_post(eid, email, sig):
        log.warning("Invalid or unsigned teams_post_account_approval event")
        return {"statusCode": 403, "body": "forbidden"}

    ctx = get_requester_context()
    deps = TeamsDependencies(
        cfg=ctx.cfg,
        org_client=ctx.org_client,
        s3_client=ctx.s3_client,
        sso_client=ctx.sso_client,
        identity_store_client=ctx.identity_store_client,
        schedule_client=ctx.schedule_client,
    )
    configure_teams_dependencies(deps)
    await get_teams_app()
    await post_account_approval_to_teams_channel(deps, eid, rname, email)
    return {"statusCode": 200, "body": "ok"}
