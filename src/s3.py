import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import boto3
from botocore.config import Config
from mypy_boto3_s3 import S3Client, type_defs

from config import get_config, get_logger

logger = get_logger(service="s3")
# Audit writes only. Bounded so a slow S3 fails the write (the entry then goes to CloudWatch)
# well inside the Lambda timeout, instead of killing the grant or revocation around it.
s3: S3Client = boto3.client("s3", config=Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 2}))


@dataclass
class AuditEntry:
    reason: str
    operation_type: Literal["grant", "revoke", "sync_add", "sync_remove", "manual_detected", "declined", "incomplete"]
    permission_duration: Literal["NA"] | timedelta
    sso_user_principal_id: str
    audit_entry_type: Literal["group", "account", "sync_add", "sync_remove", "manual_detected"]
    version: int = 2
    role_name: str = "NA"
    account_id: str = "NA"
    requester_slack_id: str = "NA"
    requester_email: str = "NA"
    request_id: str = "NA"
    approver_slack_id: str = "NA"
    approver_email: str = "NA"
    group_name: str = "NA"
    group_id: str = "NA"
    group_membership_id: str = "NA"
    secondary_domain_was_used: bool = False
    # New fields for attribute sync operations
    sync_operation: str = "NA"  # "attribute_sync" for sync operations
    matched_attributes: dict | None = None  # Attributes that triggered the match
    # Intake path of the request behind the entry: "slack" or "cli" (plus the
    # SigV4-verified ARN for "cli"); "revoker" when the revoker's sweep removed
    # access no request accounts for; "NA" when unknown, e.g. a revocation scheduled before it was recorded.
    request_source: str = "NA"
    verified_arn: str = "NA"
    sso_user_email: str = "NA"  # Human-readable email for the SSO user
    # "declined" entries only: a DecisionReason value, "Discarded" or "Expired".
    decision_reason: str = "NA"
    # "incomplete" entries only: the error that stopped an approved request, prefixed with the failed
    # step when access was already granted. A failed grant audit write still schedules the revocation,
    # so an "incomplete" entry with no "grant" entry can stand for live, scheduled access.
    error_message: str = "NA"


def log_operation(
    audit_entry: AuditEntry,
    bucket_name: str | None = None,
    bucket_prefix: str | None = None,
) -> type_defs.PutObjectOutputTypeDef:
    """Log an audit entry to S3.

    Args:
        audit_entry: The audit entry to log.
        bucket_name: S3 bucket name for audit entries. If None, uses config.
        bucket_prefix: S3 key prefix for partitions. If None, uses config.

    Returns:
        S3 PutObject response.
    """
    # Get bucket config from parameters or fall back to global config
    if bucket_name is None or bucket_prefix is None:
        cfg = get_config()
        bucket_name = bucket_name or cfg.s3_bucket_for_audit_entry_name
        bucket_prefix = bucket_prefix or cfg.s3_bucket_prefix_for_partitions

    now = datetime.now(timezone.utc)
    logger.debug("Posting audit entry to s3", extra={"audit_entry": audit_entry})
    logger.info("Posting audit entry to s3")
    return s3.put_object(
        Bucket=bucket_name,
        Key=f"{bucket_prefix}/{now.strftime('%Y/%m/%d')}/{uuid.uuid4()}.json",
        Body=json.dumps(audit_record(audit_entry, now)),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )


def audit_record(audit_entry: AuditEntry, now: datetime | None = None) -> dict:
    """The JSON object stored in S3 for audit_entry."""
    now = now or datetime.now(timezone.utc)
    if isinstance(audit_entry.permission_duration, timedelta):
        permission_duration = str(int(audit_entry.permission_duration.total_seconds()))
    else:
        permission_duration = "NA"

    audit_entry_dict = asdict(audit_entry) | {
        "permission_duration": permission_duration,
        "time": str(now),
        "timestamp": int(now.timestamp() * 1000),
    }

    # Handle matched_attributes - convert None to "NA" for JSON serialization consistency
    if audit_entry_dict.get("matched_attributes") is None:
        audit_entry_dict["matched_attributes"] = "NA"
    return audit_entry_dict


def log_operation_best_effort(
    audit_entry: AuditEntry,
    bucket_name: str | None = None,
    bucket_prefix: str | None = None,
) -> Exception | None:
    """For entries whose loss must not fail the caller's flow. Returns the write error, if any.
    The log record then carries the full entry, so CloudWatch alone can reconstruct it."""
    try:
        log_operation(audit_entry=audit_entry, bucket_name=bucket_name, bucket_prefix=bucket_prefix)
    except Exception as e:
        logger.exception(f"Failed to write {audit_entry.operation_type} audit entry: {e}", extra={"audit_entry": audit_record(audit_entry)})
        return e
    return None


@dataclass
class SyncAuditParams:
    """Parameters for creating a sync audit entry."""

    operation_type: Literal["sync_add", "sync_remove", "manual_detected"]
    sso_user_principal_id: str
    sso_user_email: str
    group_id: str
    group_name: str
    reason: str
    matched_attributes: dict | None = None


def create_sync_audit_entry(params: SyncAuditParams) -> AuditEntry:
    """Create an audit entry for attribute sync operations.

    Args:
        params: SyncAuditParams containing all required fields

    Returns:
        AuditEntry configured for sync operations
    """
    return AuditEntry(
        reason=params.reason,
        operation_type=params.operation_type,
        permission_duration="NA",
        sso_user_principal_id=params.sso_user_principal_id,
        audit_entry_type=params.operation_type,
        group_id=params.group_id,
        group_name=params.group_name,
        sync_operation="attribute_sync",
        matched_attributes=params.matched_attributes,
        sso_user_email=params.sso_user_email,
        request_source="attribute_sync",
    )
