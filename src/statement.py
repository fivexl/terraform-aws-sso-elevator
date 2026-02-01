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
PermissionSetArn = Annotated[str, Field(pattern=r"^arn:aws:sso:::permissionSet/ssoins-[a-f0-9]+/ps-[a-f0-9]+$")]
WildCard = Annotated[str, Field(pattern=r"^\*$")]


class BaseStatement(BaseModel):
    permission_set: FrozenSet[Union[PermissionSetName, PermissionSetArn, WildCard]]

    allow_self_approval: bool | None = None
    approval_is_not_required: bool | None = None
    approvers: FrozenSet[EmailStr] = Field(default_factory=frozenset)
    required_group_membership: FrozenSet[str] = Field(default_factory=frozenset)


class Statement(BaseStatement):
    resource_type: ResourceType = Field(default=ResourceType.Account, frozen=True)
    resource: FrozenSet[Union[AWSAccountId, WildCard]]

    def affects(self, account_id: str, permission_set_name: str, permission_set_arn: str | None = None) -> bool:  # noqa: ANN101
        account_match = account_id in self.resource or "*" in self.resource
        ps_match = (
            permission_set_name in self.permission_set
            or "*" in self.permission_set
            or (permission_set_arn is not None and permission_set_arn in self.permission_set)
        )
        return account_match and ps_match


def get_affected_statements(
    statements: FrozenSet[Statement],
    account_id: str,
    permission_set_name: str,
    permission_set_arn: str | None = None,
) -> FrozenSet[Statement]:
    return frozenset(statement for statement in statements if statement.affects(account_id, permission_set_name, permission_set_arn))


def get_permission_sets_for_account(statements: FrozenSet[Statement], account_id: str) -> set[str]:
    """Return permission set names valid for given account based on statements."""
    permission_sets: set[str] = set()
    for statement in statements:
        if account_id in statement.resource or "*" in statement.resource:
            if "*" in statement.permission_set:
                return {"*"}
            permission_sets.update(statement.permission_set)
    return permission_sets


def is_statement_eligible_for_user(statement: BaseStatement, user_group_ids: set[str]) -> bool:
    """Check if user is eligible for a statement based on group membership.

    If required_group_membership is empty, statement is available to all (backwards compatible).
    Otherwise, user must be in at least one of the required groups.
    """
    if not statement.required_group_membership:
        return True
    return bool(statement.required_group_membership & user_group_ids)


def get_eligible_statements_for_user(statements: FrozenSet[Statement], user_group_ids: set[str]) -> FrozenSet[Statement]:
    """Filter statements to only those the user is eligible for based on group membership."""
    return frozenset(s for s in statements if is_statement_eligible_for_user(s, user_group_ids))


def get_accounts_for_user(statements: FrozenSet[Statement], user_group_ids: set[str]) -> set[str]:
    """Return account IDs the user can request access to based on eligible statements."""
    eligible_statements = get_eligible_statements_for_user(statements, user_group_ids)
    accounts: set[str] = set()
    for statement in eligible_statements:
        if "*" in statement.resource:
            return {"*"}
        accounts.update(statement.resource)
    return accounts


def get_permission_sets_for_account_and_user(statements: FrozenSet[Statement], account_id: str, user_group_ids: set[str]) -> set[str]:
    """Return permission set names valid for given account and user based on eligible statements.

    Args:
        statements: The statements to evaluate.
        account_id: The AWS account ID to filter for.
        user_group_ids: The set of SSO group IDs the user belongs to.
            Unlike make_decision_on_access_request, this always filters by group membership.
            Pass an empty set if group IDs are unavailable; only statements without
            required_group_membership will match (secure default).
    """
    eligible_statements = get_eligible_statements_for_user(statements, user_group_ids)
    return get_permission_sets_for_account(eligible_statements, account_id)


class OUStatement(BaseStatement):
    resource_type: ResourceType = Field(default=ResourceType.OU, frozen=True)
    resource: FrozenSet[Union[AWSOUName, WildCard]]


AWSSSOGroupID = Annotated[
    str, Field(pattern=r"^([0-9a-f]{10}-)?[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}$")
]


class GroupStatement(BaseModel):
    resource: FrozenSet[AWSSSOGroupID]
    allow_self_approval: bool | None = None
    approval_is_not_required: bool | None = None
    approvers: FrozenSet[EmailStr] = Field(default_factory=frozenset)

    def affects(self, group_id: str) -> bool:  # noqa: ANN101
        return group_id in self.resource


def get_affected_group_statements(statements: FrozenSet[GroupStatement], group_id: str) -> FrozenSet[GroupStatement]:
    return frozenset(statement for statement in statements if statement.affects(group_id))
