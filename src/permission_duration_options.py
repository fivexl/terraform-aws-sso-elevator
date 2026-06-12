"""Duration choice strings shared by Slack and Teams request forms."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import config

# Slack StaticSelect allows at most 100 options; keep parity with slack_helpers.get_max_duration_block.
_MAX_INCREMENTS = 99


def permission_duration_choice_strings(cfg: config.Config) -> list[str]:
    """Return selectable duration labels/values (HH:MM in 30-minute steps, or config override)."""
    if cfg.permission_duration_list_override:
        elements = list(cfg.permission_duration_list_override)
        if len(elements) > 100:  # noqa: PLR2004
            return elements[:99] + elements[-1:]
        return elements
    max_increments = min(cfg.max_permissions_duration_time * 2, _MAX_INCREMENTS)
    return [f"{i // 2:02d}:{(i % 2) * 30:02d}" for i in range(1, max_increments + 1)]
