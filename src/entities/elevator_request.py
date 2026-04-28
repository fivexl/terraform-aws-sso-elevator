"""Domain types for access requests (platform-agnostic)."""

from enum import Enum

from .model import BaseModel


class ElevatorRequestKind(str, Enum):
    account = "account"
    group = "group"


class ElevatorRequestStatus(str, Enum):
    """Lifecycle of a request row in the store (authoritative, not UI)."""

    draft = "draft"
    awaiting_approval = "awaiting_approval"
    completed = "completed"
    discarded = "discarded"
    expired = "expired"


class ElevatorRequestRecord(BaseModel):
    """Structured access request: source of truth outside chat message text."""

    elevator_request_id: str
    kind: ElevatorRequestKind
    status: ElevatorRequestStatus
    requester_slack_id: str
    requester_display_name: str | None = None
    reason: str
    permission_duration_seconds: int
    account_id: str | None = None
    permission_set_name: str | None = None
    group_id: str | None = None
    slack_channel_id: str | None = None
    slack_message_ts: str | None = None
    request_id_for_audit_sso: str = "NA"
