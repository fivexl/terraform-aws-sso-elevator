import json
from datetime import datetime, timedelta, timezone
import config 


def event_bridge_schedule_after(td: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return f"at({(now + td).replace(microsecond=0).isoformat().replace('+00:00', '')})"


# TODO typehint for schedule_client
def create_schedule_for_revoker(
    time_delta: timedelta,
    schedule_client,
    account_id: str,
    permission_set_arn: str,
    user_principal_id: str,
    requester_slack_id: str,
    requester_email: str,
    approver_slack_id: str,
    approver_email: str,
):
    cfg = config.Config() #type: ignore
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    schedule_expression = event_bridge_schedule_after(time_delta)
    # scheduler.amazonaws.com
    payload = {
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Name": schedule_name,
        "ScheduleExpression": schedule_expression,
        "State": "ENABLED",
        "Target": {
            "Arn": cfg.revoker_function_arn,
            "RoleArn": cfg.schedule_policy_arn,
            "Input": json.dumps(
                {
                    "Schedule_name": schedule_name,
                    "ScheduleExpression": schedule_expression,
                    "Scheduled_revoke": {
                        "instance_arn": cfg.sso_instance_arn,
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
