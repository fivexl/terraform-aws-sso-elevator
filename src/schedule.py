import json
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import jmespath as jp
from mypy_boto3_scheduler import EventBridgeSchedulerClient, type_defs
from pydantic import ValidationError

import config
import entities
import sso
from entities import BaseModel

logger = config.get_logger(service="schedule")
cfg = config.Config()  # type: ignore


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
        client.delete_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
        logger.info("Schedule deleted", extra={"schedule_name": schedule_name})
    except botocore.exceptions.ClientError as e:
        if jp.search("Error.Code", e.response) == "ResourceNotFoundException":
            logger.info("Schedule for deletion was not found", extra={"schedule_name": schedule_name})
        else:
            raise e


def get_scheduled_revoke_events(client: EventBridgeSchedulerClient) -> list[RevokeEvent]:
    paginator = client.get_paginator("list_schedules")
    scheduled_revoke_events = []
    for page in paginator.paginate(GroupName=cfg.schedule_group_name):
        schedules_names = jp.search("Schedules[*].Name", page)
        for schedule_name in schedules_names:
            if not schedule_name:
                continue
            full_schedule = client.get_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
            if event := json.loads(jp.search("Target.Input", full_schedule))["revoke_event"]:
                try:
                    revoke_event = RevokeEvent.parse_raw(event)
                except ValidationError:
                    logger.error("Failed to parse schedule for revoke event", extra={"schedule_name": schedule_name, "event": event})
                    continue
                scheduled_revoke_events.append(revoke_event)
    return scheduled_revoke_events


def get_and_delete_schedule_if_already_exist(
    client: EventBridgeSchedulerClient,
    user_account_assignment: sso.UserAccountAssignment,
):
    for revoke_event in get_scheduled_revoke_events(client):
        if revoke_event.user_account_assignment == user_account_assignment:
            logger.info("Schedule already exist, deleting it", extra={"schedule_name": revoke_event.schedule_name})
            delete_schedule(client, revoke_event.schedule_name)


def schedule_revoke_event(
    schedule_client: EventBridgeSchedulerClient,
    time_delta: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    user_account_assignment: sso.UserAccountAssignment,
):
    logger.info("Scheduling revoke event")
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    get_and_delete_schedule_if_already_exist(schedule_client, user_account_assignment)
    revoke_event = RevokeEvent(
        schedule_name=schedule_name,
        approver=approver,
        requester=requester,
        user_account_assignment=user_account_assignment,
    )
    logger.debug("Creating schedule", extra={"revoke_event": revoke_event})
    schedule_client.create_schedule(
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=schedule_name,
        GroupName=cfg.schedule_group_name,
        ScheduleExpression=event_bridge_schedule_after(time_delta),
        State="ENABLED",
        Target=type_defs.TargetTypeDef(
            Arn=cfg.revoker_function_arn,
            RoleArn=cfg.schedule_policy_arn,
            Input=json.dumps(
                {
                    "action": "event_bridge_revoke",
                    "revoke_event": revoke_event.json(),
                },
            ),
        ),
    )
