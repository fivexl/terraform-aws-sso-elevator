"""PostHog analytics integration for SSO Elevator.

This module provides optional analytics tracking when a PostHog API key is configured.
All events include a global "application" property for filtering.

Usage:
    import analytics

    analytics.capture(
        event="aws_access_requested",
        distinct_id=requester.email,
        properties={"account_id": "123456789012", ...}
    )

    # Call shutdown before Lambda freeze to flush events
    analytics.shutdown()
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import posthog as posthog_module

APPLICATION = "aws-sso-elevator"


@lru_cache(maxsize=1)
def get_posthog_client() -> posthog_module | None:
    """Get configured PostHog client, or None if not configured.

    Returns cached client instance. The client is configured on first call
    if POSTHOG_API_KEY environment variable is set.
    """
    api_key = os.environ.get("POSTHOG_API_KEY")
    if not api_key:
        return None

    import posthog

    posthog.api_key = api_key
    posthog.host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
    return posthog


def capture(event: str, distinct_id: str, properties: dict | None = None) -> None:
    """Capture an analytics event if PostHog is configured.

    Adds the global "application" property to all events for filtering.

    Args:
        event: The event name (e.g., "aws_access_requested").
        distinct_id: Unique identifier for the user (typically email).
        properties: Optional dict of event properties.
    """
    client = get_posthog_client()
    if client:
        all_properties = {"application": APPLICATION, **(properties or {})}
        client.capture(distinct_id, event, properties=all_properties)


def shutdown() -> None:
    """Flush pending events before Lambda freeze.

    Should be called at the end of Lambda handler to ensure
    all events are sent before the container is frozen.
    """
    client = get_posthog_client()
    if client:
        client.flush()
