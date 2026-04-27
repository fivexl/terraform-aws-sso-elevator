"""API Gateway / Function URL event helpers shared by any HTTP-wrapped handler on Lambda."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


def normalize_api_gateway_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if v is not None and isinstance(v, str):
            out[k] = v
        elif v is not None and isinstance(v, (list, tuple)) and v:
            out[k] = v[0] if isinstance(v[0], str) else str(v[0])
    return out


def parse_api_gateway_event_json_body(
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse JSON object body from a Lambda proxy event.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` with a full API Gateway–style
    response dict to return from the handler.
    """
    raw_body = event.get("body", "")
    if isinstance(raw_body, str) and event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
    if isinstance(raw_body, str):
        try:
            body: Any = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return None, {
                "statusCode": 400,
                "headers": {"Content-Type": "text/plain"},
                "body": "Invalid JSON body",
            }
    else:
        body = raw_body
    if not isinstance(body, dict):
        return None, {
            "statusCode": 400,
            "headers": {"Content-Type": "text/plain"},
            "body": "Expected object JSON body",
        }
    return body, None
