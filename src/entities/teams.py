from __future__ import annotations

from .model import BaseModel
from . import slack


class TeamsUser(BaseModel):
    """Teams user with fields compatible with entities.slack.User."""

    id: str
    aad_object_id: str
    email: str
    display_name: str

    @property
    def real_name(self) -> str:
        return self.display_name

    def to_slack_user(self) -> slack.User:
        """Convert to slack.User for passing to business logic that expects it."""
        return slack.User(id=self.id, email=self.email, real_name=self.display_name)
