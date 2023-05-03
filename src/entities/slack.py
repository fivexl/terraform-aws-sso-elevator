from enum import Enum

from .model import BaseModel


class ApproverAction(Enum):
    Approve = "approve"
    Deny = "deny"


class User(BaseModel):
    id: str
    email: str
    real_name: str
