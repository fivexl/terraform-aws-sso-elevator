import logging
from dataclasses import asdict, dataclass
from datetime import datetime

import boto3
from mypy_boto3_dynamodb import type_defs


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


def log_operation(logger: logging.Logger, table_name: str, audit_entry: AuditEntry) -> type_defs.PutItemOutputTableTypeDef:
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    now = datetime.now()
    audit_entry_with_time = asdict(audit_entry) | {"time": str(now), "timestamp": int(now.timestamp() * 1000)}
    logger.info(f"Posting to {table_name}: {audit_entry_with_time}")
    return table.put_item(Item=audit_entry_with_time)
