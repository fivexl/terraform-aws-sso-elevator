"""Defer posting the Teams approval card to a second Lambda invoke so task/submit returns before Teams' ~15s client timeout.

When a launcher activity id is known, the approval adaptive card **replaces** the launcher message in place
(``update``) instead of ``send`` as a new channel message. Otherwise the behavior falls back to a new
message and the requester may still get the legacy launcher ``submitted`` stub from the handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class AccountApprovalTeamsThread:
    """Channel conversation id, regional service URL, parent id for threaded ``reply`` sends, optional launcher id."""

    conversation_id: str
    service_url: str = ""
    parent_activity_id: str = ""
    launcher_activity_id: str = ""


def _hmac_key() -> bytes:
    s = (os.environ.get("TEAMS_MICROSOFT_APP_PASSWORD") or "").encode()
    if not s:
        msg = "TEAMS_MICROSOFT_APP_PASSWORD is required to sign internal approval events"
        raise ValueError(msg)
    return s


def sign_account_approval_post(
    elevator_id: str,
    requester_email: str,
    teams: AccountApprovalTeamsThread,
) -> str:
    cid = (teams.conversation_id or "").strip()
    su = (teams.service_url or "").strip()
    pa = (teams.parent_activity_id or "").strip()
    lid = (teams.launcher_activity_id or "").strip()
    body = f"v5|account|{elevator_id}|{requester_email.lower()}|{cid}|{su}|{pa}|{lid}"
    return hmac.new(_hmac_key(), body.encode(), hashlib.sha256).hexdigest()


def verify_account_approval_post(
    elevator_id: str,
    requester_email: str,
    teams: AccountApprovalTeamsThread,
    signature: str,
) -> bool:
    try:
        expect = sign_account_approval_post(elevator_id, requester_email, teams)
    except ValueError:
        return False
    return hmac.compare_digest(expect, signature)


def invoke_account_approval_post_async(
    elevator_id: str,
    requester_display_name: str,
    requester_email: str,
    teams: AccountApprovalTeamsThread,
) -> None:
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not fn:
        return
    import boto3

    tsu = (teams.service_url or "").strip()
    tpar = (teams.parent_activity_id or "").strip()
    tlaunch = (teams.launcher_activity_id or "").strip()
    cid = (teams.conversation_id or "").strip()
    pay: dict[str, Any] = {
        "internal_action": ACCOUNT_APPROVAL_INTERNAL_ACTION,
        "elevator_id": elevator_id,
        "requester_display_name": requester_display_name,
        "requester_email": requester_email,
        "teams_conversation_id": cid,
        "teams_service_url": tsu,
        "teams_parent_activity_id": tpar,
        "teams_launcher_activity_id": tlaunch,
        "hmac": sign_account_approval_post(elevator_id, requester_email, teams),
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
    teams: AccountApprovalTeamsThread,
) -> None:
    """Build approval card, post to the request's Teams conversation, update store, schedule follow-ups."""
    c = deps.cfg
    org_client = deps.org_client
    schedule_client = deps.schedule_client

    teams_conversation_id = (teams.conversation_id or "").strip()
    teams_service_url = (teams.service_url or "").strip()
    teams_parent_activity_id = (teams.parent_activity_id or "").strip()

    if not teams_conversation_id:
        log.warning("Deferred post: empty teams_conversation_id for %s", elevator_id)
        return

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
    rname = (rec.requester_display_name or requester_display_name or rec.requester_slack_id or "Requester").strip() or "Requester"
    card = teams_cards.build_approval_card(
        requester_name=rname,
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

    tpar = (teams_parent_activity_id or "").strip() or None
    launcher_id = (teams.launcher_activity_id or "").strip()

    def _notifier() -> TeamsNotifier:
        return TeamsNotifier(
            c,
            get_teams_app,
            conversation_id_override=teams_conversation_id,
            service_url_override=(teams_service_url or "").strip() or None,
            reply_parent_activity_id_override=tpar,
        )

    try:
        notifier = _notifier()
        act_id = ""
        if launcher_id:
            try:
                await notifier.update_message(launcher_id, card)
                act_id = launcher_id
            except Exception as e:
                log.exception("In-place approval card update failed, posting new message: %s", e)
                act_id = await notifier.send_message(text="New access request", card=card)
        else:
            act_id = await notifier.send_message(text="New access request", card=card)
        if act_id:
            request_store.update_teams_presentation(elevator_id, teams_conversation_id, act_id)
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
    teams_conversation_id = str(event.get("teams_conversation_id", ""))
    sig = str(event.get("hmac", ""))
    teams_service_url = str(event.get("teams_service_url", ""))
    teams_parent_activity_id = str(event.get("teams_parent_activity_id", ""))
    teams_launcher_activity_id = str(event.get("teams_launcher_activity_id", ""))
    thread = AccountApprovalTeamsThread(
        conversation_id=teams_conversation_id,
        service_url=teams_service_url,
        parent_activity_id=teams_parent_activity_id,
        launcher_activity_id=teams_launcher_activity_id,
    )
    if not eid or not email or not teams_conversation_id or not verify_account_approval_post(eid, email, thread, sig):
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
    await post_account_approval_to_teams_channel(deps, eid, rname, email, thread)
    return {"statusCode": 200, "body": "ok"}
