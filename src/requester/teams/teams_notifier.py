"""Proactive Teams channel/DM messages via Microsoft Teams App (no BotFrameworkAdapter)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import config
from microsoft_teams.api import (
    Account,
    Attachment,
    MessageActivityInput,
)
from microsoft_teams.api.models import (
    ChannelID,
    ConversationAccount,
    ConversationReference,
)

log = logging.getLogger(__name__)

TeamsGetApp = Callable[[], Awaitable[Any]]


def _config_conversation_id_for_bot(conversation_id: str) -> str:
    """Strip ``;messageid=...`` from pasted Terraform values (channel root id only)."""
    s = (conversation_id or "").strip()
    if ";" in s:
        return s.split(";", 1)[0].strip()
    return s


def _teams_channel_id_and_thread_root_activity_id(conversation_id: str) -> tuple[str, str | None]:
    """Split ``19:...@thread.tacv2;messageid=ROOT`` into channel conversation id and thread root message id.

    Teams *Threads* channels send task/submit with a thread-scoped ``conversation.id``. The Bot Framework
    ``POST .../v3/conversations/{id}/activities`` path must use the **channel** id; the thread is selected via
    ``replyToId`` on the activity, not by putting ``;messageid=...`` in the URL (that often returns 404).
    """
    s = (conversation_id or "").strip()
    key = ";messageid="
    i = s.lower().find(key)
    if i < 0:
        return s, None
    base = s[:i].strip()
    tail = s[i + len(key) :].strip()
    if ";" in tail:
        tail = tail.split(";", 1)[0].strip()
    return (base, tail) if tail else (base, None)


def _ref_for_conversation(app_id: str, tenant_id: str, conversation_id: str, service_url: str | None = None) -> ConversationReference:
    su = (service_url or f"https://smba.trafficmanager.net/{tenant_id}/").rstrip("/")
    return ConversationReference(
        service_url=su + "/",
        channel_id=cast(ChannelID, "msteams"),
        bot=Account(id=app_id, name="bot"),
        conversation=ConversationAccount(
            id=conversation_id,
            tenant_id=tenant_id,
            conversation_type="channel",
            is_group=True,
        ),
    )


class TeamsNotifier:
    """Send or update activities in a Teams channel / DM using :attr:`App.activity_sender`."""

    def __init__(
        self,
        cfg: config.Config,
        get_app: TeamsGetApp,
        conversation_id_override: str | None = None,
        service_url_override: str | None = None,
        reply_parent_activity_id_override: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._get_app = get_app
        self._conversation_id_override = (conversation_id_override or "").strip() or None
        # Bot Framework / Teams: use the activity's serviceUrl (regional) for sends; the default
        # smba URL from config alone can 404 for some conversations.
        self._service_url_override = (service_url_override or "").strip() or None
        # Task submit often carries ``replyToId`` (parent card message). Prefer it over ``;messageid=`` in
        # ``conversation.id`` for ``POST .../activities/{parentId}`` (reply) — they can differ in Teams.
        self._reply_parent_activity_id = (reply_parent_activity_id_override or "").strip() or None

    def _effective_approval_conversation_id(self) -> str:
        if self._conversation_id_override:
            base, _ = _teams_channel_id_and_thread_root_activity_id(self._conversation_id_override)
            return base
        return _config_conversation_id_for_bot(cast(str, self._cfg.teams_approval_conversation_id))

    def _approval_thread_reply_to_id(self) -> str | None:
        # Prefer the thread root extracted from ``;messageid=`` in the conversation id.
        # The activity's ``reply_to_id`` points to the launcher card (a reply *inside* the thread),
        # not the thread root. Bot Framework ``reply`` requires the root message id to land in the
        # correct thread; using a non-root id causes the message to appear in the main channel feed.
        if self._conversation_id_override:
            _, root = _teams_channel_id_and_thread_root_activity_id(self._conversation_id_override)
            if root:
                return root
        if self._reply_parent_activity_id:
            return self._reply_parent_activity_id
        return None

    async def send_message(self, text: str, card: dict | None = None) -> str:
        app = await self._get_app()
        msg: MessageActivityInput
        if card:
            t = text if (text and text.strip()) else "."
            msg = MessageActivityInput(text=t).add_attachments(
                Attachment(
                    content_type="application/vnd.microsoft.card.adaptive",
                    content=card,
                )
            )
        else:
            msg = MessageActivityInput(text=text)
        r2 = self._approval_thread_reply_to_id()
        if r2:
            msg.reply_to_id = r2
        return await _send_in_conversation(
            app=app,
            app_id=cast(str, self._cfg.teams_microsoft_app_id),
            tenant_id=cast(str, self._cfg.teams_azure_tenant_id),
            conversation_id=self._effective_approval_conversation_id(),
            message=msg,
            service_url=self._service_url_override,
        )

    async def update_message(self, activity_id: str, card: dict) -> None:
        app = await self._get_app()
        conv_id = self._effective_approval_conversation_id()
        up = MessageActivityInput(
            id=str(activity_id),
        ).add_attachments(Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card))
        await app.api.conversations.activities(conv_id).update(str(activity_id), up)

    async def send_thread_reply(self, parent_activity_id: str, text: str) -> None:
        app = await self._get_app()
        conv_id = self._effective_approval_conversation_id()
        app_id = cast(str, self._cfg.teams_microsoft_app_id)
        tenant_id = cast(str, self._cfg.teams_azure_tenant_id)
        msg = MessageActivityInput(text=text)
        msg.reply_to_id = parent_activity_id
        su = self._service_url_override or cast(
            str,
            getattr(getattr(app, "api", None), "service_url", None) or f"https://smba.trafficmanager.net/{tenant_id}/",
        )
        await _send_in_conversation(
            app=app,
            app_id=app_id,
            tenant_id=tenant_id,
            conversation_id=conv_id,
            message=msg,
            service_url=su,
        )

    async def send_proactive_dm(self, conversation_reference: dict, text: str) -> None:
        app = await self._get_app()
        ref = ConversationReference.model_validate(conversation_reference)
        if not text:
            return
        msg = MessageActivityInput(text=text)
        try:
            await app.activity_sender.send(msg, ref)
        except Exception as e:
            if "403" in str(e) or "Forbidden" in str(e):
                log.exception(f"Proactive DM blocked (403 Forbidden): {e}")
            else:
                raise


async def _send_in_conversation(  # noqa: PLR0913
    app: Any,
    app_id: str,
    tenant_id: str,
    conversation_id: str,
    message: MessageActivityInput,
    service_url: str | None = None,
) -> str:
    su = (
        service_url
        or cast(
            str,
            getattr(getattr(app, "api", None), "service_url", None) or f"https://smba.trafficmanager.net/{tenant_id}/",
        )
    ).rstrip("/")
    # Teams: replies in a channel thread must use Bot Framework ``reply`` (POST .../activities/{parentId}),
    # not ``create`` with only replyToId in the body — otherwise the message lands in the main feed.
    parent = getattr(message, "reply_to_id", None)
    as_http = getattr(getattr(app, "activity_sender", None), "_client", None)
    if parent and as_http is not None:
        from microsoft_teams.api import ApiClient  # same package as App

        ref = _ref_for_conversation(app_id, tenant_id, conversation_id, su)
        out = message.model_copy(update={"reply_to_id": None})
        out.from_ = ref.bot
        out.conversation = ref.conversation
        api = ApiClient(
            su,
            as_http,
            cloud=getattr(app, "cloud", None),
        )
        res = await api.conversations.activities(conversation_id).reply(str(parent), out)
        return _sent_activity_id_or_empty(res)

    ref = _ref_for_conversation(app_id, tenant_id, conversation_id, su)
    res = await app.activity_sender.send(message, ref)
    return _sent_activity_id_or_empty(res)


def _sent_activity_id_or_empty(res: Any) -> str:
    from .teams_bot_framework_path_encoding import MISSING_ACTIVITY_ID_PLACEHOLDER

    if not res or not getattr(res, "id", None):
        return ""
    rid = str(res.id)
    if rid == MISSING_ACTIVITY_ID_PLACEHOLDER:
        return ""
    return rid
