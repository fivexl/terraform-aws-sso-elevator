from datetime import datetime
import logging
from mypy_boto3_dynamodb import DynamoDBServiceResource, type_defs
import boto3


def log_operation_to_dynamodb(
    logger: logging.Logger, table_name: str, audit_entry: dict
) -> type_defs.PutItemOutputTableTypeDef:
    dynamodb: DynamoDBServiceResource = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    now = datetime.now()
    audit_entry_with_time = audit_entry | {
        "time": str(now),
        "timestamp": int(now.timestamp() * 1000),
    }
    logger.info(f"Posting to {table_name}: {audit_entry_with_time}")
    return table.put_item(Item=audit_entry_with_time)
