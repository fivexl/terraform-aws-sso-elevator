"""Extract elevator_request_id and approve/discard from Teams adaptive card invoke payloads (shape varies by client)."""

from __future__ import annotations

import json
from typing import Any


def _find_first_str_by_keys(obj: Any, *keys: str) -> str | None:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            r = _find_first_str_by_keys(v, *keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first_str_by_keys(v, *keys)
            if r:
                return r
    return None


def _find_approve_or_discard(obj: Any) -> str | None:
    if isinstance(obj, dict):
        v = obj.get("action")
        if isinstance(v, str) and v.strip().lower() in ("approve", "discard"):
            return v.strip().lower()
        for val in obj.values():
            r = _find_approve_or_discard(val)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_approve_or_discard(v)
            if r:
                return r
    return None


def parse_adaptive_card_invoke_value(val: Any) -> tuple[str | None, str | None]:
    """Return ``(elevator_request_id, 'approve'|'discard'|None)`` for :class:`AdaptiveCardInvokeActivity` ``value``."""
    if val is None:
        return (None, None)
    vdict: dict[str, Any]
    if hasattr(val, "model_dump"):
        vdict = val.model_dump(mode="json", by_alias=True)  # type: ignore[no-untyped-call]
    elif isinstance(val, dict):
        vdict = val
    else:
        return (None, None)
    eid = _find_first_str_by_keys(vdict, "elevator_request_id", "elevatorRequestId")
    act = _find_approve_or_discard(vdict)
    return (eid, act)


def value_from_message_activity_for_adaptive_submit(activity: Any) -> Any:
    """Payload for some channel clients: ``Action.Submit`` arrives as ``type=message``, not ``invoke``."""
    v = getattr(activity, "value", None)
    if v is not None:
        return v
    t = (getattr(activity, "text", None) or "").strip()
    if t.startswith("{"):
        try:
            return json.loads(t)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    chd = getattr(activity, "channel_data", None)
    if chd is not None and hasattr(chd, "model_dump"):
        chd = chd.model_dump()  # type: ignore[assignment]
    if isinstance(chd, dict) and chd and (
        "elevator_request_id" in chd
        or "action" in chd
        or "elevatorRequestId" in str(chd)
    ):
        return chd
    return None
