from datetime import timedelta
from typing import Literal, Union

from pydantic import model_validator, RootModel

import entities
import sso
from entities.model import BaseModel


class RevokeEvent(BaseModel):
    schedule_name: str
    approver: entities.slack.User
    requester: entities.slack.User
    user_account_assignment: sso.UserAccountAssignment
    permission_duration: timedelta


class GroupRevokeEvent(BaseModel):
    schedule_name: str
    approver: entities.slack.User
    requester: entities.slack.User
    group_assignment: sso.GroupAssignment
    permission_duration: timedelta


class ScheduledGroupRevokeEvent(BaseModel):
    action: Literal["event_bridge_group_revoke"]
    revoke_event: GroupRevokeEvent

    @model_validator(mode="before")
    @classmethod
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        values["revoke_event"] = GroupRevokeEvent.model_validate_json(values["revoke_event"])
        return values


class ScheduledRevokeEvent(BaseModel):
    action: Literal["event_bridge_revoke"]
    revoke_event: RevokeEvent

    @model_validator(mode="before")
    @classmethod
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        values["revoke_event"] = RevokeEvent.model_validate_json(values["revoke_event"])
        return values


class DiscardButtonsEvent(BaseModel):
    action: Literal["discard_buttons_event"]
    schedule_name: str
    time_stamp: str
    channel_id: str


class CheckOnInconsistency(BaseModel):
    action: Literal["check_on_inconsistency"]


class SSOElevatorScheduledRevocation(BaseModel):
    action: Literal["sso_elevator_scheduled_revocation"]


class ApproverNotificationEvent(BaseModel):
    action: Literal["approvers_renotification"]
    schedule_name: str
    time_stamp: str
    channel_id: str
    time_to_wait_in_seconds: float


Event = RootModel[
    Union[
        ScheduledRevokeEvent,
        DiscardButtonsEvent,
        CheckOnInconsistency,
        SSOElevatorScheduledRevocation,
        ApproverNotificationEvent,
        ScheduledGroupRevokeEvent,
    ]
]
