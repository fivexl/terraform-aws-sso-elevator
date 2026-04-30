"""Channel thread context for Microsoft Teams (Bot Framework).

`ActivityContext.send` (microsoft-teams-apps) always uses ``POST .../conversations/{id}/activities`` (``create``).
In Teams, posting into a *channel thread* must use ``POST .../conversations/{id}/activities/{parentId}`` (``reply``);
otherwise the message becomes a new top-level channel post. See
https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-send-and-receive-messages

Callers that need in-thread text (approval outcomes, "form received", etc.) should use
``teams_activity_helpers.send_channel_thread_message`` (or
:class:`requester.teams.teams_notifier.TeamsNotifier` for proactive posts), all of which use the Bot Framework
``reply`` API when a parent message id is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .teams_notifier import _teams_channel_id_and_thread_root_activity_id


@dataclass(frozen=True, slots=True)
class ChannelThreadContext:
    """Raw channel conversation id from the activity, regional service URL, and a parent id for in-thread children."""

    conversation_id: str
    service_url: str
    parent_activity_id: str

    @classmethod
    def from_activity(cls, activity: Any) -> ChannelThreadContext:
        conv = getattr(activity, "conversation", None)
        raw = str(getattr(conv, "id", "") or "").strip()
        su = str(getattr(activity, "service_url", "") or "").strip()
        parent = _thread_reply_target_activity_id(activity, raw)
        return cls(conversation_id=raw, service_url=su, parent_activity_id=parent)

    def account_approval_thread_conversation_id(self) -> str:
        """String passed to :class:`teams_approval_deferred.AccountApprovalTeamsThread` and signing (HMAC)."""
        cid = self.conversation_id
        p = (self.parent_activity_id or "").strip()
        if p and ";messageid=" not in cid:
            return f"{cid};messageid={p}"
        return cid

    def account_approval_fields(self) -> tuple[str, str, str]:
        """``(conversation_id, service_url, parent_activity_id)`` for :class:`teams_approval_deferred.AccountApprovalTeamsThread`."""
        return (self.account_approval_thread_conversation_id(), (self.service_url or "").strip(), (self.parent_activity_id or "").strip())


def _channel_data_dict(activity: Any) -> dict:
    chd = getattr(activity, "channel_data", None)
    if chd is not None and hasattr(chd, "model_dump"):
        chd = chd.model_dump()
    if isinstance(chd, dict):
        return chd
    return {}


def _thread_reply_target_activity_id(activity: Any, raw_conversation_id: str) -> str:
    # Invoke: often replyToId; task/submit: may only have channelData or ;messageid= on conversation
    r = str(getattr(activity, "reply_to_id", None) or "").strip()
    if r:
        return r
    chd = _channel_data_dict(activity)
    for key in ("replyToId", "threadId"):
        v = chd.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    m = chd.get("message")
    if isinstance(m, dict) and m.get("id") is not None and str(m.get("id", "")).strip():
        return str(m["id"]).strip()
    _base, root = _teams_channel_id_and_thread_root_activity_id(raw_conversation_id)
    return (root or "").strip()


def thread_root_activity_id_for_reply(raw_conversation_id: str) -> str:
    """Return the ``;messageid=`` part of ``conversation.id`` if present."""
    _b, root = _teams_channel_id_and_thread_root_activity_id(raw_conversation_id)
    return (root or "").strip()


def parent_activity_id_for_bot_thread_reply(
    raw_conversation_id: str,
    task_submit_or_invoke_parent: str = "",
) -> str:
    """When ``replyToId`` and ``;messageid=`` root **both** exist and differ, prefer the **thread root**.

    Observed 2026-04: with ``parentId`` = task submit's ``replyToId`` and ``;messageid=`` = a different
    thread root, Bot Framework sometimes placed messages on the **channel** instead of the thread. Using
    the **root** id in ``/conversations/.../activities/{parentId}`` matched the in-client thread.

    For **follow-up** lines (approver ping, scheduled reminders), prefer
    :func:`thread_follow_up_reply_parent_candidates` which tries the **approval card** id first, then root
    — some tenants only keep replies in-thread when ``replyToId`` targets the card message.
    """
    root = thread_root_activity_id_for_reply(raw_conversation_id)
    t = (task_submit_or_invoke_parent or "").strip()
    if root and t and t != root:
        return root
    return t or root


def thread_follow_up_reply_parent_candidates(raw_conversation_id: str, card_activity_id: str) -> list[str]:
    """Ordered Bot Framework activity ids to use as ``replyToId`` / reply parent under the approval card.

    When ``;messageid=ROOT`` and the stored **card** activity id differ, try **card first**, then **root**.
    Some Teams channel configurations deliver follow-ups to the main channel when replying only to ROOT;
    replying to the launcher / approval card id matches the in-client thread.
    """
    root = thread_root_activity_id_for_reply(raw_conversation_id)
    ta = (card_activity_id or "").strip()
    ordered: tuple[str, ...]
    if root and ta and root != ta:
        ordered = (ta, root)
    elif ta:
        ordered = (ta,)
    elif root:
        ordered = (root,)
    else:
        ordered = ()
    out: list[str] = []
    for x in ordered:
        z = (x or "").strip()
        if z and z not in out:
            out.append(z)
    return out


def base_channel_conversation_id_for_path(raw_conversation_id: str) -> str:
    base, _ = _teams_channel_id_and_thread_root_activity_id(raw_conversation_id)
    return (base or "").strip() or (raw_conversation_id or "").strip()


def message_activity_id_for_channel_card_invoke(activity: Any) -> str:
    """Bot Framework id of the **message** that contains the card (value for ``PATCH`` / update).

    Adaptive card invocations in Teams do not always set ``replyToId``; the parent is sometimes only in
    ``channelData.message`` or ``channelData.replyToId``.
    """
    r = str(getattr(activity, "reply_to_id", None) or "").strip()
    if r:
        return r
    chd = _channel_data_dict(activity)
    for key in ("replyToId", "threadId"):
        v = chd.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    m = chd.get("message")
    if isinstance(m, dict) and m.get("id") is not None and str(m.get("id", "")).strip():
        return str(m["id"]).strip()
    return (str(getattr(activity, "id", None) or "")).strip()
