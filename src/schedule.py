import json
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import jmespath as jp
from mypy_boto3_scheduler import EventBridgeSchedulerClient, type_defs
from pydantic import ValidationError

import config
import entities
import sso
from events import RevokeEvent, ScheduledRevokeEvent, Event

logger = config.get_logger(service="schedule")
cfg = config.get_config()


def get_schedules(client: EventBridgeSchedulerClient) -> list[type_defs.GetScheduleOutputTypeDef]:
    paginator = client.get_paginator("list_schedules")
    scheduled_events = []
    for page in paginator.paginate(GroupName=cfg.schedule_group_name):
        schedules_names = jp.search("Schedules[*].Name", page)
        for schedule_name in schedules_names:
            if not schedule_name:
                continue
            full_schedule = client.get_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
            scheduled_events.append(full_schedule)
    return scheduled_events


def get_scheduled_events(client: EventBridgeSchedulerClient) -> list[ScheduledRevokeEvent]:
    scheduled_events = get_schedules(client)
    scheduled_revoke_events: list[ScheduledRevokeEvent] = []
    for full_schedule in scheduled_events:
        if full_schedule["Name"].startswith("discard-buttons"):
            continue

        event = json.loads(jp.search("Target.Input", full_schedule))

        try:
            event = Event.parse_obj(event)
        except ValidationError as e:
            logger.warning("Got unexpected event", extra={"event": event, "error": e})
            continue

        if isinstance(event.__root__, ScheduledRevokeEvent):
            scheduled_revoke_events.append(event.__root__)
            print("case: ScheduledRevokeEvent")

    return scheduled_revoke_events


def delete_schedule(client: EventBridgeSchedulerClient, schedule_name: str) -> None:
    try:
        client.delete_schedule(GroupName=cfg.schedule_group_name, Name=schedule_name)
        logger.info("Schedule deleted", extra={"schedule_name": schedule_name})
    except botocore.exceptions.ClientError as e:
        if jp.search("Error.Code", e.response) == "ResourceNotFoundException":
            logger.info("Schedule for deletion was not found", extra={"schedule_name": schedule_name})
        else:
            raise e


def get_and_delete_scheduled_revoke_event_if_already_exist(
    client: EventBridgeSchedulerClient,
    user_account_assignment: sso.UserAccountAssignment,
) -> None:
    for scheduled_event in get_scheduled_events(client):
        if scheduled_event.revoke_event.user_account_assignment == user_account_assignment:
            logger.info("Schedule already exist, deleting it", extra={"schedule_name": scheduled_event.revoke_event.schedule_name})
            delete_schedule(client, scheduled_event.revoke_event.schedule_name)


def event_bridge_schedule_after(td: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return f"at({(now + td).replace(microsecond=0).isoformat().replace('+00:00', '')})"


def schedule_revoke_event(
    schedule_client: EventBridgeSchedulerClient,
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    user_account_assignment: sso.UserAccountAssignment,
) -> type_defs.CreateScheduleOutputTypeDef:
    logger.info("Scheduling revoke event")
    schedule_name = f"{cfg.revoker_function_name}" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    get_and_delete_scheduled_revoke_event_if_already_exist(schedule_client, user_account_assignment)
    revoke_event = RevokeEvent(
        schedule_name=schedule_name,
        approver=approver,
        requester=requester,
        user_account_assignment=user_account_assignment,
        permission_duration=permission_duration,
    )
    logger.debug("Creating schedule", extra={"revoke_event": revoke_event})
    return schedule_client.create_schedule(
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=schedule_name,
        GroupName=cfg.schedule_group_name,
        ScheduleExpression=event_bridge_schedule_after(permission_duration),
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
