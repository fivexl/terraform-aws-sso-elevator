from enum import Enum
from typing import Union

from pydantic import BaseModel, ConstrainedStr, EmailStr, Field


class ResourceType(str, Enum):
    Account = "Account"
    OU = "OU"


class AWSAccountId(ConstrainedStr):
    regex = r"^\d{12}$"


class AWSOUName(ConstrainedStr):
    regex = r"^[\s\S]{1,128}$"


class PermissionSetName(ConstrainedStr):
    regex = r"^[\w+=,.@-]{1,32}$"


class WildCard(ConstrainedStr):
    regex = r"^\*$"


class BaseStatement(BaseModel):
    permission_set: frozenset[Union[PermissionSetName, WildCard]]

    allow_self_approval: bool = False
    approval_is_not_required: bool = False
    approvers: frozenset[EmailStr] = Field(default_factory=frozenset)

    class Config:
        frozen = True


class Statement(BaseStatement):
    resource_type: ResourceType = Field(ResourceType.Account, const=True)
    resource: frozenset[Union[AWSAccountId, WildCard]]

    def affects(self, account_id: str, permission_set_name: str) -> bool:
        return (account_id in self.resource or "*" in self.resource) and (
            permission_set_name in self.permission_set or "*" in self.permission_set
        )


def get_affected_statements(statements: frozenset[Statement], account_id: str, permission_set_name: str) -> frozenset[Statement]:
    return frozenset(statement for statement in statements if statement.affects(account_id, permission_set_name))


class OUStatement(BaseStatement):
    resource_type: ResourceType = Field(ResourceType.OU, const=True)
    resource: frozenset[Union[AWSOUName, WildCard]]
