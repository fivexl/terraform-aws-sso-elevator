"""Placeholder for Microsoft Teams (Bot Framework / Graph) behind the same ports as Slack.

Core and `request_store` stay platform-agnostic; wire a real implementation when adding Teams.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ports import ChatSurface, UserDirectory


class TeamsUserDirectory:
    def email_for_platform_user(self, platform_user_id: str) -> str:  # noqa: ARG002
        raise NotImplementedError("Graph / Entra user resolution is not implemented in this build.")


class TeamsChatSurface:
    def post_thread_reply(
        self,
        *,
        channel_id: str,  # noqa: ARG002
        thread_ts: str,  # noqa: ARG002
        text: str,  # noqa: ARG002
    ) -> None:
        raise NotImplementedError("Bot Framework message activity is not implemented in this build.")


def as_user_directory() -> UserDirectory:
    return TeamsUserDirectory()  # type: ignore[return-value]


def as_chat_surface() -> ChatSurface:
    return TeamsChatSurface()  # type: ignore[return-value]
