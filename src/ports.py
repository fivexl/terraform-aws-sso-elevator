"""Platform-agnostic contracts (ports) for identity and chat surfaces.

Slack and future Teams drivers implement these; core uses them after wiring in handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from slack_sdk import WebClient


@runtime_checkable
class UserDirectory(Protocol):
    """Resolve a platform user id to a stable work email (for SSO / policy)."""

    def email_for_platform_user(self, platform_user_id: str) -> str: ...


@runtime_checkable
class ChatSurface(Protocol):
    """Post or update approval messages; Block Kit and Adaptive Card implementations go behind this."""

    def post_thread_reply(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        text: str,
    ) -> None: ...


class SlackUserDirectory:
    def __init__(self, client: "WebClient") -> None:
        self._client = client

    def email_for_platform_user(self, platform_user_id: str) -> str:
        import slack_helpers  # import here to keep ports load order simple

        u = slack_helpers.get_user(self._client, id=platform_user_id)
        return u.email
