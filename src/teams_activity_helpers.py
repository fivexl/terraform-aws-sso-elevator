"""Helpers for microsoft-teams-apps ActivityContext (replaces raw BotFramework TurnContext)."""

from __future__ import annotations

from typing import Any

from microsoft_teams.api import Attachment, MessageActivityInput
from microsoft_teams.apps.routing.activity_context import ActivityContext


async def teams_send_text_message(ctx: ActivityContext[Any], text: str) -> None:
    """Send a plain text message in the current conversation (invoke response body often empty)."""
    await ctx.send(MessageActivityInput(text=text))


def teams_message_with_adaptive_card(text: str, card: dict) -> MessageActivityInput:
    return MessageActivityInput(text=text).add_attachments(Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card))
