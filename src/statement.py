from enum import Enum
from typing import FrozenSet, Union

from pydantic import ConstrainedStr, EmailStr, Field

from entities import BaseModel


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
    permission_set: FrozenSet[Union[PermissionSetName, WildCard]]

    allow_self_approval: bool = False
    approval_is_not_required: bool = False
    approvers: FrozenSet[EmailStr] = Field(default_factory=frozenset)


class Statement(BaseStatement):
    resource_type: ResourceType = Field(ResourceType.Account, const=True)
    resource: FrozenSet[Union[AWSAccountId, WildCard]]

    def affects(self, account_id: str, permission_set_name: str) -> bool:  # noqa: ANN101
        return (account_id in self.resource or "*" in self.resource) and (
            permission_set_name in self.permission_set or "*" in self.permission_set
        )


def get_affected_statements(statements: FrozenSet[Statement], account_id: str, permission_set_name: str) -> FrozenSet[Statement]:
    return frozenset(statement for statement in statements if statement.affects(account_id, permission_set_name))


class OUStatement(BaseStatement):
    resource_type: ResourceType = Field(ResourceType.OU, const=True)
    resource: FrozenSet[Union[AWSOUName, WildCard]]
