from typing import Optional

from .model import BaseModel


class Account(BaseModel):
    id: str
    name: str


class PermissionSet(BaseModel):
    name: str
    arn: str
    description: Optional[str]

class SSOGroup(BaseModel):
    name: str
    id: str
    description: Optional[str]
    identity_store_id: str
