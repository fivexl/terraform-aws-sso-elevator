import json
import os
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import jmespath as jp
from aws_lambda_powertools import Logger
from mypy_boto3_scheduler import EventBridgeSchedulerClient, type_defs
from pydantic import BaseModel, ValidationError

import config
import entities
import sso

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)


class RevokeEvent(BaseModel):
    schedule_name: str
    approver: entities.slack.User
    requester: entities.slack.User
    user_account_assignment: sso.UserAccountAssignment


def event_bridge_schedule_after(td: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return f"at({(now + td).replace(microsecond=0).isoformat().replace('+00:00', '')})"


def delete_schedule(client: EventBridgeSchedulerClient, schedule_name: str):
    try:
        client.delete_schedule(GroupName="sso_elevator_revoke",Name=schedule_name)
    except botocore.exceptions.ClientError as e:
        if jp.search("Error.Code", e.response) == "ResourceNotFoundException":
            logger.info(f"schedule with name {schedule_name} was not found for deletion")
        else:
            raise e


def get_scheduled_revoke_events(client: EventBridgeSchedulerClient) -> list[RevokeEvent]:
    paginator = client.get_paginator("list_schedules")
    scheduled_revoke_events = []
    for page in paginator.paginate(GroupName= "sso_elevator_revoke"):
        schedules_names = jp.search("Schedules[*].Name", page)
        for schedule_name in schedules_names:
            if not schedule_name:
                continue
            full_schedule = client.get_schedule(GroupName="sso_elevator_revoke",Name=schedule_name)
            if event := json.loads(jp.search("Target.Input", full_schedule))["revoke_event"]:
                try:
                    revoke_event = RevokeEvent.parse_raw(event)
                except ValidationError:
                    logger.error(f"failed to parse schedule. Name: {schedule_name}, event:{event}")
                    continue
                scheduled_revoke_events.append(revoke_event)
    return scheduled_revoke_events


def get_and_delete_schedule_if_already_exist(
    client: EventBridgeSchedulerClient,
    user_account_assignment: sso.UserAccountAssignment,
):
    for revoke_event in get_scheduled_revoke_events(client):
        if revoke_event.user_account_assignment == user_account_assignment:
            delete_schedule(client, revoke_event.schedule_name)
            logger.info(f"previous schedule:{revoke_event.schedule_name} found and deleted")


def schedule_revoke_event(
    schedule_client: EventBridgeSchedulerClient,
    time_delta: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    user_account_assignment: sso.UserAccountAssignment,
):
    cfg = config.Config()  # type: ignore
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    get_and_delete_schedule_if_already_exist(schedule_client, user_account_assignment)
    try:
        schedule_client.create_schedule(
            FlexibleTimeWindow={"Mode": "OFF"},
            Name=schedule_name,
            GroupName="sso_elevator_revoke",
            ScheduleExpression=event_bridge_schedule_after(time_delta),
            State="ENABLED",
            Target=type_defs.TargetTypeDef(
                Arn=cfg.revoker_function_arn,
                RoleArn=cfg.schedule_policy_arn,
                Input=json.dumps(
                    {
                        "action": "event_bridge_revoke",
                        "revoke_event": RevokeEvent(
                            schedule_name=schedule_name,
                            approver=approver,
                            requester=requester,
                            user_account_assignment=user_account_assignment,
                        ).json(),
                    },
                ),
            ),
        )
    except botocore.exceptions.ClientError as e:
        logger.error(f"failed to schedule revoke event: {e}")
        raise e