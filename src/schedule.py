import json
from datetime import datetime, timedelta, timezone


def event_bridge_schedule_after(td: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return f"at({(now + td).replace(microsecond=0).isoformat().replace('+00:00', '')})"


# TODO typehint for schedule_client
def create_schedule_for_revoker(
    lambda_arn: str,
    lambda_name: str,
    time_delta: timedelta,
    schedule_client,
    sso_instance_arn: str,
    account_id: str,
    permission_set_arn: str,
    user_principal_id: str,
    requester_slack_id: str,
    requester_email: str,
    approver_slack_id: str,
    approver_email: str,
):
    schedule_name = f"{lambda_name}-{time_delta}_" + datetime.now().strftime(
        "%Y-%m-%d-%H-%M-%S"
    )
    schedule_expression = event_bridge_schedule_after(time_delta)

    payload = {
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Name": schedule_name,
        "ScheduleExpression": schedule_expression,
        "State": "ENABLED",
        "Target": {
            "Arn": lambda_arn,
            "RoleArn": "arn:aws:iam::754426185857:role/test_scheduling",
            "Input": json.dumps(
                {
                    "Schedule_name": schedule_name,
                    "ScheduleExpression": schedule_expression,
                    "Scheduled_revoke": {
                        "instance_arn": sso_instance_arn,
                        "account_id": account_id,
                        "permission_set_arn": permission_set_arn,
                        "user_principal_id": user_principal_id,
                        "requester_slack_id": requester_slack_id,
                        "requester_email": requester_email,
                        "approver_slack_id": approver_slack_id,
                        "approver_email": approver_email,
                    },
                },
                indent=4,
            ),
        },
    }
    schedule_client.create_schedule(**payload)


def delete_schedule(schedule_name: str, schedule_client):
    schedule_client.delete_schedule(Name=schedule_name)
