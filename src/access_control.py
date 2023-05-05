import datetime
from enum import Enum
from typing import FrozenSet

import boto3

import config
import dynamodb
import entities
import schedule
import sso
from entities import BaseModel
from statement import Statement, get_affected_statements

logger = config.get_logger("access_control")
cfg = config.get_config()

session = boto3._get_default_session()
org_client = session.client("organizations")
sso_client = session.client("sso-admin")
identitystore_client = session.client("identitystore")
schedule_client = session.client("scheduler")


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
    """Decision on approver request

    grant: bool - Create account assignment, if grant is True
    permit: bool - Allow approver to make an action (Approve/Deny) if permit is True
    based_on_statements: FrozenSet[Statement]
    """

    grant: bool
    permit: bool
    based_on_statements: FrozenSet[Statement]


def make_decision_on_approve_request(
    action: entities.ApproverAction,
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
                    grant=action == entities.ApproverAction.Approve,
                    permit=True,
                    based_on_statements=frozenset([statement]),
                )

    return ApproveRequestDecision(
        grant=False,
        permit=False,
        based_on_statements=affected_statements,
    )


def execute_decision(
    decision: AccessRequestDecision | ApproveRequestDecision,
    permission_set_name: str,
    account_id: str,
    permission_duration: datetime.timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
):
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, permission_set_name)
    user_principal_id = sso.get_user_principal_id_by_email(identitystore_client, sso_instance.identity_store_id, requester.email)
    account_assignment = sso.UserAccountAssignment(
        instance_arn=sso_instance.arn,
        account_id=account_id,
        permission_set_arn=permission_set.arn,
        user_principal_id=user_principal_id,
    )

    logger.info("Creating account assignment", extra={"account_assignment": account_assignment})

    account_assignment_status = sso.create_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )

    dynamodb.log_operation(
        table_name=cfg.dynamodb_table_name,
        audit_entry=dynamodb.AuditEntry(
            account_id=account_id,
            role_name=permission_set.name,
            reason=reason,
            requester_slack_id=requester.id,
            requester_email=requester.email,
            approver_slack_id=approver.id,
            approver_email=approver.email,
            request_id=account_assignment_status.request_id,
            operation_type="grant",
            permission_duration=permission_duration,
        ),
    )

    schedule.schedule_revoke_event(
        permission_duration=permission_duration,
        schedule_client=schedule_client,
        approver=approver,
        requester=requester,
        user_account_assignment=sso.UserAccountAssignment(
            instance_arn=sso_instance.arn,
            account_id=account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=user_principal_id,
        ),
    )
    return True  # Temporary solution for testing
