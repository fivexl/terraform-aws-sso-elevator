"""Helpers for microsoft-teams-apps ActivityContext (replaces raw BotFramework TurnContext)."""

from __future__ import annotations

from typing import Any

import config
from microsoft_teams.api import Attachment, MessageActivityInput
from microsoft_teams.apps.routing.activity_context import ActivityContext

from .teams_notifier import _teams_channel_id_and_thread_root_activity_id

log = config.get_logger(service="teams_activity_helpers")


def launcher_activity_id_for_task_submit(ctx: ActivityContext[Any]) -> str:
    """Bot Framework id of the launcher message (``replyToId`` or thread root) for PATCH/replace in place.

    Same target as :func:`update_teams_launcher_message_after_task_submit` uses to PATCH the card.
    """
    conv = getattr(ctx.activity, "conversation", None)
    if conv is None:
        return ""
    raw_cid = str(getattr(conv, "id", "") or "").strip()
    base_cid, root_id = _teams_channel_id_and_thread_root_activity_id(raw_cid)
    reply_parent = str(getattr(ctx.activity, "reply_to_id", None) or "").strip()
    act_id = reply_parent or (str(root_id) if root_id else "")
    if not act_id or not base_cid:
        return ""
    return str(act_id)


def _reply_to_id_for_threaded_channel_message(activity: Any) -> str | None:
    """Post in the same Teams channel thread as the parent message (Bot Framework id) or ``;messageid=``."""
    r2 = getattr(activity, "reply_to_id", None)
    if r2:
        return str(r2)
    conv = getattr(activity, "conversation", None)
    raw = str(getattr(conv, "id", "") or "").strip()
    _base, root = _teams_channel_id_and_thread_root_activity_id(raw)
    return root


async def teams_send_text_message(ctx: ActivityContext[Any], text: str) -> None:
    """Send plain text in the current channel thread when possible (parity with Slack ``thread_ts``)."""
    msg = MessageActivityInput(text=text)
    r2 = _reply_to_id_for_threaded_channel_message(ctx.activity)
    if r2:
        msg.reply_to_id = r2
    await ctx.send(msg)


async def update_teams_launcher_message_after_task_submit(ctx: ActivityContext[Any], kind: str) -> None:
    """Replace the launcher card (open form) with a submitted state so the button disappears."""
    from . import teams_cards  # local import: teams_cards imports types

    conv = getattr(ctx.activity, "conversation", None)
    if conv is None:
        return
    raw_cid = str(getattr(conv, "id", "") or "").strip()
    base_cid, root_id = _teams_channel_id_and_thread_root_activity_id(raw_cid)
    reply_parent = str(getattr(ctx.activity, "reply_to_id", None) or "").strip()
    act_id = reply_parent or (str(root_id) if root_id else "")
    if not act_id or not base_cid:
        return
    card = teams_cards.build_request_access_launcher_submitted_card(kind if kind in ("account", "group") else "account")
    up = MessageActivityInput().add_attachments(
        Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card),
    )
    up.id = str(act_id)
    try:
        await ctx.api.conversations.activities(base_cid).update(str(act_id), up)
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        # Some tenants return 403 on card update (RSC / policy); deleting the bot's own message often still works.
        if code in (401, 403):
            try:
                await ctx.api.conversations.activities(base_cid).delete(str(act_id))
                log.info("Launcher removed via delete after update was forbidden (HTTP %s)", code)
                return
            except Exception as de:
                log.exception("Launcher update failed with %s and delete also failed: %s", code, de)
                return
        log.exception("Failed to update launcher message after task submit: %s", e)


def teams_message_with_adaptive_card(text: str, card: dict) -> MessageActivityInput:
    return MessageActivityInput(text=text).add_attachments(Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card))
