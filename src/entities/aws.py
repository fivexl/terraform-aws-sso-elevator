from typing import Optional

from pydantic import BaseModel


class Account(BaseModel):
    id: str
    name: str


class PermissionSet(BaseModel):
    name: str
    arn: str
    description: Optional[str]
