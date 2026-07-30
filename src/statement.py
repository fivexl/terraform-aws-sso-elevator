from enum import Enum
from typing import Annotated, FrozenSet, Union

from pydantic import EmailStr, Field

from entities import BaseModel


class ResourceType(str, Enum):
    Account = "Account"
    OU = "OU"


# Pydantic v2: Use Annotated with Field for constrained strings
AWSAccountId = Annotated[str, Field(pattern=r"^\d{12}$")]
AWSOUName = Annotated[str, Field(pattern=r"^[\s\S]{1,128}$")]
PermissionSetName = Annotated[str, Field(pattern=r"^[\w+=,.@-]{1,32}$")]
WildCard = Annotated[str, Field(pattern=r"^\*$")]
AWSSSOGroupID = Annotated[
    str, Field(pattern=r"^([0-9a-f]{10}-)?[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}$")
]


def requester_allowed(
    statement: "BaseStatement | GroupStatement",
    requester_emails: FrozenSet[str],
    requester_group_ids: FrozenSet[str],
) -> bool:
    """Whether a requester may use a statement.

    Empty ``allowed_groups`` and ``allowed_users`` means no restriction (backward-compatible
    default). Otherwise the requester must be a member of at least one of the listed SSO groups
    or be listed by email in ``allowed_users``. ``requester_emails`` holds the requester's email
    variants (e.g. with secondary fallback domains applied), matched case-insensitively.
    """
    if not statement.allowed_groups and not statement.allowed_users:
        return True
    allowed_users = {user.lower() for user in statement.allowed_users}
    if any(email.lower() in allowed_users for email in requester_emails):
        return True
    return bool(statement.allowed_groups & requester_group_ids)


class BaseStatement(BaseModel):
    permission_set: FrozenSet[Union[PermissionSetName, WildCard]]

    allow_self_approval: bool | None = None
    approval_is_not_required: bool | None = None
    approvers: FrozenSet[EmailStr] = Field(default_factory=frozenset)
    #: Optional requester restriction: SSO group IDs whose members may request this statement.
    #: Empty (together with allowed_users) = unrestricted.
    allowed_groups: FrozenSet[AWSSSOGroupID] = Field(default_factory=frozenset)
    #: Optional requester restriction: emails of users who may request this statement.
    #: Empty (together with allowed_groups) = unrestricted.
    allowed_users: FrozenSet[EmailStr] = Field(default_factory=frozenset)


class Statement(BaseStatement):
    resource_type: ResourceType = Field(default=ResourceType.Account, frozen=True)
    resource: FrozenSet[Union[AWSAccountId, WildCard]]

    def affects(self, account_id: str, permission_set_name: str) -> bool:  # noqa: ANN101
        return (account_id in self.resource or "*" in self.resource) and (
            permission_set_name in self.permission_set or "*" in self.permission_set
        )


def get_affected_statements(statements: FrozenSet[Statement], account_id: str, permission_set_name: str) -> FrozenSet[Statement]:
    return frozenset(statement for statement in statements if statement.affects(account_id, permission_set_name))


class OUStatement(BaseStatement):
    resource_type: ResourceType = Field(default=ResourceType.OU, frozen=True)
    resource: FrozenSet[Union[AWSOUName, WildCard]]


class GroupStatement(BaseModel):
    resource: FrozenSet[AWSSSOGroupID]
    allow_self_approval: bool | None = None
    approval_is_not_required: bool | None = None
    approvers: FrozenSet[EmailStr] = Field(default_factory=frozenset)
    #: Optional requester restriction: SSO group IDs whose members may request this statement.
    #: Empty (together with allowed_users) = unrestricted.
    allowed_groups: FrozenSet[AWSSSOGroupID] = Field(default_factory=frozenset)
    #: Optional requester restriction: emails of users who may request this statement.
    #: Empty (together with allowed_groups) = unrestricted.
    allowed_users: FrozenSet[EmailStr] = Field(default_factory=frozenset)

    def affects(self, group_id: str) -> bool:  # noqa: ANN101
        return group_id in self.resource


def get_affected_group_statements(statements: FrozenSet[GroupStatement], group_id: str) -> FrozenSet[GroupStatement]:
    return frozenset(statement for statement in statements if statement.affects(group_id))
