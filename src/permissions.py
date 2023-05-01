import os
from dataclasses import dataclass
from typing import Literal, Optional, Union

from aws_lambda_powertools import Logger
from pydantic import BaseModel

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)




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
            logger.info(f"By this statement: {statement}, approval is not required for request")
            return ApprovalIsNotRequired()

        if statement.approvers:
            if statement.allow_self_approval and requester_email in statement.approvers:
                logger.info(f"By this statement: {statement}, requester: {requester_email}, can self approve request")
                return SelfApprovalIsAllowedAndRequesterIsApprover()

            can_be_approved_by.update(approver for approver in statement.approvers if approver != requester_email)
    logger.info(f"Request requres approval, by these statements:{affected_statements}, request can be approved by: {can_be_approved_by}")
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
    logger.info(f"By these statements: {affected_statements}, request can be approved by: {can_be_approved_by}")
    return can_be_approved_by
