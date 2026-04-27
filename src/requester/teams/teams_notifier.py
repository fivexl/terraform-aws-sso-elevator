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


def _ref_for_conversation(app_id: str, tenant_id: str, conversation_id: str, service_url: str | None = None) -> ConversationReference:
    su = (service_url or f"https://smba.trafficmanager.net/{tenant_id}/").rstrip("/")
    return ConversationReference(
        service_url=su + "/",
        channel_id=cast(ChannelID, "msteams"),
        bot=Account(id=app_id, name="bot"),
        conversation=ConversationAccount(id=conversation_id),
    )


class TeamsNotifier:
    """Send or update activities in a Teams channel / DM using :attr:`App.activity_sender`."""

    def __init__(self, cfg: config.Config, get_app: TeamsGetApp) -> None:
        self._cfg = cfg
        self._get_app = get_app

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
        return await _send_in_conversation(
            app=app,
            app_id=cast(str, self._cfg.teams_microsoft_app_id),
            tenant_id=cast(str, self._cfg.teams_azure_tenant_id),
            conversation_id=cast(str, self._cfg.teams_approval_conversation_id),
            message=msg,
        )

    async def update_message(self, activity_id: str, card: dict) -> None:
        app = await self._get_app()
        conv_id = cast(str, self._cfg.teams_approval_conversation_id)
        up = MessageActivityInput(
            id=str(activity_id),
        ).add_attachments(Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card))
        await app.api.conversations.activities(conv_id).update(str(activity_id), up)

    async def send_thread_reply(self, parent_activity_id: str, text: str) -> None:
        app = await self._get_app()
        conv_id = cast(str, self._cfg.teams_approval_conversation_id)
        app_id = cast(str, self._cfg.teams_microsoft_app_id)
        tenant_id = cast(str, self._cfg.teams_azure_tenant_id)
        msg = MessageActivityInput(text=text)
        msg.reply_to_id = parent_activity_id
        ref = _ref_for_conversation(app_id, tenant_id, conv_id, app.api.service_url)  # type: ignore[union-attr]
        await app.activity_sender.send(msg, ref)

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


async def _send_in_conversation(
    app: Any,
    app_id: str,
    tenant_id: str,
    conversation_id: str,
    message: MessageActivityInput,
) -> str:
    su = cast(str, getattr(getattr(app, "api", None), "service_url", None) or f"https://smba.trafficmanager.net/{tenant_id}/")
    ref = _ref_for_conversation(app_id, tenant_id, conversation_id, su)
    res = await app.activity_sender.send(message, ref)
    if res and getattr(res, "id", None):
        return str(res.id)
    return ""
