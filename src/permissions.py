from dataclasses import dataclass
from typing import Literal, Optional, Union

from pydantic import BaseModel


class Statement(BaseModel):
    resource_type: Literal["Account", "OU"]
    resource: frozenset[Union[str, Literal["*"]]]
    permission_set: frozenset[Union[str, Literal["*"]]]
    approvers: Optional[frozenset[str]]
    approval_is_not_required: bool = False
    allow_self_approval: bool = False

    class Config:
        frozen = True

    def affects(self, account_id: str, permission_set_name: str) -> bool:
        account_match = account_id in self.resource or "*" in self.resource
        permission_set_match = permission_set_name in self.permission_set or "*" in self.permission_set
        return account_match and permission_set_match


@dataclass
class RequiresApproval:
    approvers: set


class ApprovalIsNotRequired:
    ...


class SelfApprovalIsAllowedAndRequesterIsApprover:
    ...


DecisionOnRequest = Union[RequiresApproval, ApprovalIsNotRequired, SelfApprovalIsAllowedAndRequesterIsApprover]


def get_affected_statements(statements: frozenset[Statement], account_id: str, permission_set_name: str) -> list[Statement]:
    return [
        statement
        for statement in statements
        if statement.affects(
            account_id=account_id,
            permission_set_name=permission_set_name,
        )
    ]


def make_decision_on_request(
    statements: frozenset[Statement],
    account_id: str,
    permission_set_name: str,
    requester_email: str,
) -> DecisionOnRequest:
    can_be_approved_by = set()
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    for statement in affected_statements:
        if statement.approval_is_not_required:
            return ApprovalIsNotRequired()

        if statement.approvers:
            if statement.allow_self_approval and requester_email in statement.approvers:
                return SelfApprovalIsAllowedAndRequesterIsApprover()

            can_be_approved_by.update(approver for approver in statement.approvers if approver != requester_email)
    return RequiresApproval(approvers=can_be_approved_by)


def get_approvers(statements: frozenset[Statement], account_id: str, permission_set_name: str, requester_email: str) -> set[str]:
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    can_be_approved_by = set()
    for statement in affected_statements:
        if statement.approvers:
            if requester_email in statement.approvers:
                if not statement.allow_self_approval:
                    can_be_approved_by.update(statement.approvers - {requester_email})
            else:
                can_be_approved_by.update(statement.approvers)
    return can_be_approved_by
