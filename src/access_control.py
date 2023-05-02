from enum import Enum
from typing import FrozenSet

from entities import BaseModel
from statement import Statement, get_affected_statements


class DecisionReason(Enum):
    RequiresApproval = "RequiresApproval"
    ApprovalNotRequired = "ApprovalNotRequired"
    SelfApproval = "SelfApproval"
    NoStatements = "NoStatements"
    NoApprovers = "NoApprovers"


class AccessRequestDecision(BaseModel):
    grant: bool
    reason: DecisionReason
    based_on_statements: FrozenSet[Statement]
    approvers: FrozenSet[str] = frozenset()


def make_decision_on_access_request(
    statements: FrozenSet[Statement],
    permission_set_name: str,
    account_id: str,
    requester_email: str,
) -> AccessRequestDecision:
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    decision_based_on_statements: set[Statement] = set()
    potential_approvers = set()

    for statement in affected_statements:
        if statement.approval_is_not_required:
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset([statement]),
            )
        if requester_email in statement.approvers and statement.allow_self_approval:
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.SelfApproval,
                based_on_statements=frozenset([statement]),
            )

        decision_based_on_statements.add(statement)
        potential_approvers.update(approver for approver in statement.approvers if approver != requester_email)

    if len(decision_based_on_statements) == 0:  # sourcery skip
        return AccessRequestDecision(
            grant=False,
            reason=DecisionReason.NoStatements,
            based_on_statements=frozenset(decision_based_on_statements),
        )

    if not potential_approvers:
        return AccessRequestDecision(
            grant=False,
            reason=DecisionReason.NoApprovers,
            based_on_statements=frozenset(decision_based_on_statements),
        )

    return AccessRequestDecision(
        grant=False,
        reason=DecisionReason.RequiresApproval,
        approvers=frozenset(potential_approvers),
        based_on_statements=frozenset(decision_based_on_statements),
    )


class ApproveRequestDecision(BaseModel):
    permit: bool
    based_on_statements: FrozenSet[Statement]


def make_decision_on_approve_request(
    statements: frozenset[Statement],
    permission_set_name: str,
    account_id: str,
    approver_email: str,
    requester_email: str,
) -> ApproveRequestDecision:
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)

    for statement in affected_statements:
        if approver_email in statement.approvers:
            is_self_approval = approver_email == requester_email
            if is_self_approval and statement.allow_self_approval or not is_self_approval:
                return ApproveRequestDecision(
                    permit=True,
                    based_on_statements=frozenset([statement]),
                )

    return ApproveRequestDecision(
        permit=False,
        based_on_statements=affected_statements,
    )
