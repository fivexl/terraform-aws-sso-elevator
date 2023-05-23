from datetime import timedelta
from typing import Literal

from pydantic import Field, root_validator

import entities
import sso
from entities.model import BaseModel


class RevokeEvent(BaseModel):
    schedule_name: str
    approver: entities.slack.User
    requester: entities.slack.User
    user_account_assignment: sso.UserAccountAssignment
    permission_duration: timedelta


class ScheduledRevokeEvent(BaseModel):
    action: Literal["event_bridge_revoke"]
    revoke_event: RevokeEvent

    @root_validator(pre=True)
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        values["revoke_event"] = RevokeEvent.parse_raw(values["revoke_event"])
        return values


class DiscardButtonsEvent(BaseModel):
    action: Literal["discard_buttons_event"]
    schedule_name: str
    time_stamp: str
    chanel_id: str


class CheckOnInconsistency(BaseModel):
    action: Literal["check_on_inconsistency"]


class SSOElevatorScheduledRevocation(BaseModel):
    action: Literal["sso_elevator_scheduled_revocation"]


class Event(BaseModel):
    __root__: (
        ScheduledRevokeEvent
        | DiscardButtonsEvent
        | CheckOnInconsistency
        | SSOElevatorScheduledRevocation
        ) = Field(..., discriminator="action")
