from datetime import datetime
import boto3

def log_operation_to_dynamodb(logger, table_name, audit_entry):
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    now = datetime.now()
    audit_entry_with_time = audit_entry | {
            'time': str(now),
            'timestamp': int(now.timestamp()*1000)
    }
    logger.info(f'Posting to {table_name}: {audit_entry_with_time}')
    response = table.put_item(Item=audit_entry_with_time)
    return response