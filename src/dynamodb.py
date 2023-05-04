from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

import boto3

import config

logger = config.get_logger(service="dynamodb")

from datetime import timedelta


@dataclass
class AuditEntry:
    role_name: str
    account_id: str
    reason: str
    requester_slack_id: str
    requester_email: str
    request_id: str
    approver_slack_id: str
    approver_email: str
    operation_type: str
    permission_duration: timedelta | Literal["NA"]


def log_operation(table_name: str, audit_entry: AuditEntry):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    now = datetime.now()
    if isinstance(audit_entry.permission_duration, timedelta):
        permission_duration = int(audit_entry.permission_duration.total_seconds())
    else:
        permission_duration = "NA"

    audit_entry_dict = asdict(audit_entry) | {
        "permission_duration": permission_duration,
        "time": str(now),
        "timestamp": int(now.timestamp() * 1000),
    }

    result = table.put_item(Item=audit_entry_dict)
    logger.debug("Audit entry posted to dynamodb", extra={"result": result, "table": table_name})
