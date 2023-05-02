from dataclasses import asdict, dataclass
from datetime import datetime

import boto3

import config

logger = config.get_logger(service="dynamodb")


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


def log_operation(table_name: str, audit_entry: AuditEntry):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    now = datetime.now()
    audit_entry_with_time = asdict(audit_entry) | {"time": str(now), "timestamp": int(now.timestamp() * 1000)}
    result = table.put_item(Item=audit_entry_with_time)
    logger.debug("Audit entry posted to dynamodb", extra={"result": result, "table": table_name})
