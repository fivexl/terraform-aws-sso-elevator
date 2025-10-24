import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import boto3
from mypy_boto3_s3 import S3Client, type_defs

from config import get_config, get_logger

cfg = get_config()
logger = get_logger(service="s3")
s3: S3Client = boto3.client("s3")


@dataclass
class AuditEntry:
    reason: str
    operation_type: Literal["grant", "revoke"]
    permission_duration: Literal["NA"] | timedelta
    sso_user_principal_id: str
    audit_entry_type: Literal["group", "account"]
    version = 1
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


def log_operation(audit_entry: AuditEntry) -> type_defs.PutObjectOutputTypeDef:
    now = datetime.now(timezone.utc)
    logger.debug("Posting audit entry to s3", extra={"audit_entry": audit_entry})
    logger.info("Posting audit entry to s3")
    if isinstance(audit_entry.permission_duration, timedelta):
        permission_duration = str(int(audit_entry.permission_duration.total_seconds()))
    else:
        permission_duration = "NA"

    audit_entry_dict = asdict(audit_entry) | {
        "permission_duration": permission_duration,
        "time": str(now),
        "timestamp": int(now.timestamp() * 1000),
    }

    json_data = json.dumps(audit_entry_dict)
    bucket_name = cfg.s3_bucket_for_audit_entry_name
    bucket_prefix = cfg.s3_bucket_prefix_for_partitions
    return s3.put_object(
        Bucket=bucket_name,
        Key=f"{bucket_prefix}/{now.strftime('%Y/%m/%d')}/{uuid.uuid4()}.json",
        Body=json_data,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
