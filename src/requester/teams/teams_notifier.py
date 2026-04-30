"""Proactive Teams channel/DM messages via Microsoft Teams App (no BotFrameworkAdapter)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any, cast

import config
import httpx
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


def base_approval_channel_conversation_id(teams_conversation_id: str | None, cfg: config.Config) -> str:
    """Base channel id (no ``;messageid=``) for Bot Framework ``/conversations/{id}/members`` (e.g. approver roster)."""
    s = (teams_conversation_id or "").strip()
    if s:
        base, _ = _teams_channel_id_and_thread_root_activity_id(s)
        return (base or _config_conversation_id_for_bot(s)).strip()
    return _config_conversation_id_for_bot(cast(str, cfg.teams_approval_conversation_id))


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

    def _effective_thread_conversation_id_for_url(self) -> str:
        """Conversation id to use for in-thread proactive sends.

        Observed 2026-04: some tenants only deliver in-thread proactive replies when the URL/reference uses
        the full ``conversation.id`` including ``;messageid=...``. When we have an override (from the incoming
        activity), keep it intact; otherwise fall back to the base channel id.
        """
        if self._conversation_id_override:
            return self._conversation_id_override
        return self._effective_approval_conversation_id()

    def _approval_thread_reply_to_id(self) -> str | None:
        # Proactive send must use a parent that keeps the message in the thread (see
        # ``thread_follow_up_reply_parent_candidates`` — we use its first id, typically the launcher/card).
        t = (self._reply_parent_activity_id or "").strip() or None
        if not self._conversation_id_override:
            if t:
                return t
            return None
        from .teams_threading import thread_follow_up_reply_parent_candidates

        cands = thread_follow_up_reply_parent_candidates(self._conversation_id_override, (t or "").strip())
        if cands:
            return cands[0]
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
        """Update an existing activity. Uses the same regional base URL as sends when set (activity ``serviceUrl``)."""
        app = await self._get_app()
        conv_id = self._effective_approval_conversation_id()
        tenant_id = cast(str, self._cfg.teams_azure_tenant_id)
        up = MessageActivityInput(
            id=str(activity_id),
        ).add_attachments(Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card))
        su = (
            (self._service_url_override or "").strip()
            or cast(
                str,
                getattr(getattr(app, "api", None), "service_url", None) or f"https://smba.trafficmanager.net/{tenant_id}/",
            )
        ).rstrip("/")
        as_http = getattr(getattr(app, "activity_sender", None), "_client", None)
        if as_http is not None:
            from microsoft_teams.api import ApiClient  # same package as App

            api = ApiClient(
                su,
                as_http,
                cloud=getattr(app, "cloud", None),
            )
            await api.conversations.activities(conv_id).update(str(activity_id), up)
        else:
            await app.api.conversations.activities(conv_id).update(str(activity_id), up)

    async def send_thread_text_as_activity_context_send(
        self,
        parent_activity_id: str,
        text: str,
        entities: list[dict] | None = None,
    ) -> None:
        """Post in-thread text like :func:`teams_activity_helpers.teams_send_text_with_user_mention` (``ActivityContext.send``).

        Uses ``POST .../conversations/{id}/activities`` with ``replyToId`` on the body, not the reply subresource
        (``.../activities/{parentId}``), matching interactive Approve/Discard follow-up lines.
        """
        app = await self._get_app()
        conv_id = self._effective_thread_conversation_id_for_url()
        app_id = cast(str, self._cfg.teams_microsoft_app_id)
        tenant_id = cast(str, self._cfg.teams_azure_tenant_id)
        msg = MessageActivityInput(text=text, entities=entities) if entities else MessageActivityInput(text=text)
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
            use_activity_context_send_path=True,
        )

    async def send_thread_reply(self, parent_activity_id: str, text: str) -> None:
        await self.send_thread_reply_with_entities(parent_activity_id, text, None)

    async def send_thread_reply_with_entities(
        self,
        parent_activity_id: str,
        text: str,
        entities: list[dict] | None,
    ) -> None:
        app = await self._get_app()
        conv_id = self._effective_thread_conversation_id_for_url()
        app_id = cast(str, self._cfg.teams_microsoft_app_id)
        tenant_id = cast(str, self._cfg.teams_azure_tenant_id)
        msg = MessageActivityInput(text=text, entities=entities) if entities else MessageActivityInput(text=text)
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

    async def send_thread_text_with_transport_fallback(
        self,
        parent_activity_id: str,
        text: str,
        entities: list[dict] | None = None,
    ) -> None:
        """Post plain text (optional entities) as a **thread reply** only.

        Tries ``ActivityContext``-style ``POST .../activities`` with ``replyToId``, then Bot Framework
        ``POST .../activities/{parentId}/reply`` when the first path returns 404/405. Both targets the
        same parent — never posts a top-level channel message without ``replyToId``.
        """
        parent = (parent_activity_id or "").strip()
        if not parent:
            msg = "parent_activity_id is required for threaded Teams messages"
            raise ValueError(msg)
        try:
            await self.send_thread_text_as_activity_context_send(parent, text, entities)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
                raise
            log.info(
                "Teams in-thread send (activity_sender + replyToId) returned %s; retrying reply subresource",
                e.response.status_code,
            )
            await self.send_thread_reply_with_entities(parent, text, entities)

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
    *,
    use_activity_context_send_path: bool = False,
) -> str:
    su = (
        service_url
        or cast(
            str,
            getattr(getattr(app, "api", None), "service_url", None) or f"https://smba.trafficmanager.net/{tenant_id}/",
        )
    ).rstrip("/")
    # Proactive in-thread: ``use_activity_context_send_path`` matches :meth:`ActivityContext.send`; the ``reply``
    # subresource is used for other callers with ``use_activity_context_send_path`` false.
    parent = getattr(message, "reply_to_id", None)
    as_http = getattr(getattr(app, "activity_sender", None), "_client", None) or getattr(app, "http_client", None)
    if parent and as_http is not None and not use_activity_context_send_path:
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
