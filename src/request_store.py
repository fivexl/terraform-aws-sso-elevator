"""DynamoDB (or in-memory) persistence for elevator requests and ephemeral UI state."""

from __future__ import annotations

import time
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError
from mypy_boto3_dynamodb import DynamoDBClient  # noqa: TC002
from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef  # noqa: TC002

from config import get_config, get_logger
from entities.elevator_request import ElevatorRequestRecord, ElevatorRequestStatus

logger = get_logger(service="request_store")

_client: DynamoDBClient | None = None
_memory: dict[str, dict[str, Any]] = {}


def _use_memory_store() -> bool:
    return get_config().elevator_requests_table_name == "memory"


def _table_name() -> str:
    return get_config().elevator_requests_table_name


def _ddb() -> DynamoDBClient:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _put_plain(item: dict[str, Any]) -> None:
    eid = str(item["id"])
    if _use_memory_store():
        _memory[eid] = item.copy()
        return
    av = {k: _to_av(v) for k, v in item.items()}
    _ddb().put_item(TableName=_table_name(), Item=cast("dict[str, AttributeValueTypeDef]", av))


def _to_av(v: Any) -> AttributeValueTypeDef:  # noqa: ANN401
    if v is None:
        return {"NULL": True}
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, int | float):
        return {"N": str(int(v))}
    return {"S": str(v)}


def _get_plain(item_id: str) -> dict[str, Any] | None:
    if _use_memory_store():
        r = _memory.get(item_id)
        if r and r.get("entity") == "VIEW_SESSION" and int(r.get("ttl", 0)) < int(time.time()):
            return None
        return r
    gr = _ddb().get_item(TableName=_table_name(), Key={"id": {"S": item_id}})
    if "Item" not in gr:
        return None
    return _from_item(gr["Item"])


def _from_item(item: dict[str, AttributeValueTypeDef]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = int(v["N"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
        elif "NULL" in v and v.get("NULL"):
            out[k] = None
    return out


def _item_to_record(item: dict[str, Any] | None) -> ElevatorRequestRecord | None:
    if not item or item.get("entity") != "ACCESS_REQUEST":
        return None
    return ElevatorRequestRecord.model_validate(
        {
            "elevator_request_id": item["id"],
            "kind": item["kind"],
            "status": item["status"],
            "requester_slack_id": item["requester_slack_id"],
            "requester_display_name": item.get("requester_display_name") or None,
            "reason": item["reason"],
            "permission_duration_seconds": int(item["permission_duration_seconds"]),
            "account_id": item.get("account_id") or None,
            "permission_set_name": item.get("permission_set_name") or None,
            "group_id": item.get("group_id") or None,
            "slack_channel_id": item.get("slack_channel_id") or None,
            "slack_message_ts": item.get("slack_message_ts") or None,
        }
    )


def put_access_request(rec: ElevatorRequestRecord) -> None:
    d: dict[str, Any] = {
        "id": rec.elevator_request_id,
        "entity": "ACCESS_REQUEST",
        "kind": rec.kind.value,
        "status": rec.status.value,
        "requester_slack_id": rec.requester_slack_id,
        "reason": rec.reason,
        "permission_duration_seconds": rec.permission_duration_seconds,
    }
    if rec.requester_display_name:
        d["requester_display_name"] = rec.requester_display_name
    if rec.account_id:
        d["account_id"] = rec.account_id
    if rec.permission_set_name:
        d["permission_set_name"] = rec.permission_set_name
    if rec.group_id:
        d["group_id"] = rec.group_id
    if rec.slack_channel_id:
        d["slack_channel_id"] = rec.slack_channel_id
    if rec.slack_message_ts:
        d["slack_message_ts"] = rec.slack_message_ts
    _put_plain(d)


def get_access_request(elevator_request_id: str) -> ElevatorRequestRecord | None:
    raw = _get_plain(elevator_request_id)
    return _item_to_record(raw)


def update_slack_presentation(elevator_request_id: str, channel_id: str, message_ts: str) -> None:
    if _use_memory_store():
        if elevator_request_id in _memory:
            _memory[elevator_request_id]["slack_channel_id"] = channel_id
            _memory[elevator_request_id]["slack_message_ts"] = message_ts
        return
    _ddb().update_item(
        TableName=_table_name(),
        Key={"id": {"S": elevator_request_id}},
        UpdateExpression="SET slack_channel_id = :c, slack_message_ts = :t",
        ExpressionAttributeValues={":c": {"S": channel_id}, ":t": {"S": message_ts}},
    )


def update_request_status(elevator_request_id: str, status: ElevatorRequestStatus) -> None:
    if _use_memory_store():
        if elevator_request_id in _memory:
            _memory[elevator_request_id]["status"] = status.value
        return
    _ddb().update_item(
        TableName=_table_name(),
        Key={"id": {"S": elevator_request_id}},
        UpdateExpression="SET #s = :st",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":st": {"S": status.value}},
    )


def _view_session_id(user_id: str, callback_id: str) -> str:
    return f"viewsess:{user_id}:{callback_id}"


def put_view_id(user_id: str, callback_id: str, view_id: str, ttl_seconds: int = 3600) -> None:
    now = int(time.time())
    item: dict[str, Any] = {
        "id": _view_session_id(user_id, callback_id),
        "entity": "VIEW_SESSION",
        "view_id": view_id,
        "ttl": now + ttl_seconds,
    }
    _put_plain(item)


def get_view_id(user_id: str, callback_id: str) -> str | None:
    vid = _view_session_id(user_id, callback_id)
    if _use_memory_store():
        row = _memory.get(vid)
        if not row or row.get("entity") != "VIEW_SESSION":
            return None
        if int(row.get("ttl", 0)) < int(time.time()):
            return None
        return str(row.get("view_id", "")) or None
    raw = _get_plain(vid)
    if not raw or raw.get("entity") != "VIEW_SESSION":
        return None
    v = raw.get("view_id")
    return str(v) if v else None


def _in_flight_id_account(requester_slack_id: str, account_id: str, permission_set_name: str) -> str:
    return f"inflight:acc:{requester_slack_id}:{account_id}:{permission_set_name}"


def _in_flight_id_group(requester_slack_id: str, group_id: str) -> str:
    return f"inflight:grp:{requester_slack_id}:{group_id}"


def try_begin_in_flight_approval(
    *,
    requester_slack_id: str,
    account_id: str | None,
    permission_set_name: str | None,
    group_id: str | None,
    ttl_seconds: int = 900,
) -> bool:
    if group_id is not None:
        iid = _in_flight_id_group(requester_slack_id, group_id)
    else:
        if account_id is None or permission_set_name is None:
            return True
        iid = _in_flight_id_account(requester_slack_id, account_id, permission_set_name)
    now = int(time.time())
    item: dict[str, Any] = {
        "id": iid,
        "entity": "IN_FLIGHT",
        "requester_slack_id": requester_slack_id,
        "ttl": now + ttl_seconds,
    }
    if _use_memory_store():
        old = _memory.get(iid)
        if old and int(old.get("ttl", 0)) > now:
            return False
        _memory[iid] = item
        return True
    try:
        _ddb().put_item(
            TableName=_table_name(),
            Item=cast(
                "dict[str, AttributeValueTypeDef]",
                {k: _to_av(v) for k, v in item.items() if v is not None},
            ),
            ConditionExpression="attribute_not_exists(id)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def end_in_flight_approval(
    *,
    requester_slack_id: str,
    account_id: str | None,
    permission_set_name: str | None,
    group_id: str | None,
) -> None:
    if group_id is not None:
        iid = _in_flight_id_group(requester_slack_id, group_id)
    else:
        if account_id is None or permission_set_name is None:
            return
        iid = _in_flight_id_account(requester_slack_id, account_id, permission_set_name)
    if _use_memory_store():
        _memory.pop(iid, None)
        return
    _ddb().delete_item(TableName=_table_name(), Key={"id": {"S": iid}})


def get_teams_presentation_ids(elevator_request_id: str) -> tuple[str, str] | None:
    """Return ``(teams_conversation_id, teams_activity_id)`` if both were stored (card PATCH target)."""
    raw = _get_plain(elevator_request_id)
    if not raw:
        return None
    c = (raw.get("teams_conversation_id") or "").strip()
    a = (raw.get("teams_activity_id") or "").strip()
    if c and a:
        return (c, a)
    return None


def update_teams_presentation(elevator_request_id: str, conversation_id: str, activity_id: str) -> None:
    """Store Teams activity_id and conversation_id for card updates."""
    if _use_memory_store():
        if elevator_request_id in _memory:
            _memory[elevator_request_id]["teams_conversation_id"] = conversation_id
            _memory[elevator_request_id]["teams_activity_id"] = activity_id
        return
    _ddb().update_item(
        TableName=_table_name(),
        Key={"id": {"S": elevator_request_id}},
        UpdateExpression="SET teams_conversation_id = :c, teams_activity_id = :a",
        ExpressionAttributeValues={":c": {"S": conversation_id}, ":a": {"S": activity_id}},
    )


def _conv_ref_id(user_aad_id: str) -> str:
    return f"convref:{user_aad_id}"


def save_conversation_reference(user_aad_id: str, reference: dict) -> None:
    """Store ConversationReference for proactive messaging."""
    import json as _json

    item: dict[str, Any] = {
        "id": _conv_ref_id(user_aad_id),
        "entity": "CONV_REF",
        "reference": _json.dumps(reference),
    }
    _put_plain(item)


def get_conversation_reference(user_aad_id: str) -> dict | None:
    """Retrieve stored ConversationReference."""
    import json as _json

    raw = _get_plain(_conv_ref_id(user_aad_id))
    if not raw or raw.get("entity") != "CONV_REF":
        return None
    ref_str = raw.get("reference")
    if not ref_str:
        return None
    try:
        return _json.loads(str(ref_str))
    except Exception:
        return None
