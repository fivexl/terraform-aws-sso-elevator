import datetime
from enum import Enum
from typing import FrozenSet

import boto3

import config
import entities
import s3
import schedule
import sso
from entities import BaseModel
from statement import GroupStatement, Statement, get_affected_group_statements, get_affected_statements, get_eligible_statements_for_user

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
    based_on_statements: FrozenSet[Statement] | FrozenSet[GroupStatement]
    approvers: FrozenSet[str] = frozenset()


def determine_affected_statements(
    statements: FrozenSet[Statement] | FrozenSet[GroupStatement],
    account_id: str | None = None,
    permission_set_name: str | None = None,
    group_id: str | None = None,
) -> FrozenSet[Statement] | FrozenSet[GroupStatement]:
    if isinstance(statements, FrozenSet) and all(isinstance(item, Statement) for item in statements):
        return get_affected_statements(statements, account_id, permission_set_name)  # type: ignore # noqa: PGH003

    if isinstance(statements, FrozenSet) and all(isinstance(item, GroupStatement) for item in statements):
        return get_affected_group_statements(statements, group_id)  # type: ignore # noqa: PGH003

    # About type ignore:
    # For some reason, pylance is not able to understand that we already checked the type of the items in the set,
    # and shows a type error for "statements"
    raise TypeError("Statements contain mixed or unsupported types.")


def make_decision_on_access_request(  # noqa: PLR0911, PLR0913
    statements: FrozenSet[Statement] | FrozenSet[GroupStatement],
    requester_email: str,
    permission_set_name: str | None = None,
    account_id: str | None = None,
    group_id: str | None = None,
    user_group_ids: set[str] | None = None,
) -> AccessRequestDecision:
    """Make a decision on an access request.

    Args:
        statements: The statements to evaluate.
        requester_email: The email of the user requesting access.
        permission_set_name: The name of the permission set being requested.
        account_id: The AWS account ID being requested.
        group_id: The group ID for group-based access requests.
        user_group_ids: The set of SSO group IDs the user belongs to.
            If None, group-based filtering is skipped (backwards compatible).
            If an empty set, only statements without required_group_membership will match.
    """
    # Filter statements by user's group membership eligibility if user_group_ids provided
    # This is only applicable to Statement (not GroupStatement)
    if user_group_ids is not None and isinstance(statements, frozenset) and all(isinstance(s, Statement) for s in statements):
        statements = get_eligible_statements_for_user(statements, user_group_ids)  # type: ignore # noqa: PGH003

    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)

    decision_based_on_statements: set[Statement] | set[GroupStatement] = set()
    potential_approvers = set()

    explicit_deny_self_approval = any(
        statement.allow_self_approval is False and requester_email in statement.approvers for statement in affected_statements
    )
    explicit_deny_approval_not_required = any(statement.approval_is_not_required is False for statement in affected_statements)

    for statement in affected_statements:
        if statement.approval_is_not_required and not explicit_deny_approval_not_required:
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset([statement]),  # type: ignore # noqa: PGH003
            )
        if requester_email in statement.approvers and statement.allow_self_approval and not explicit_deny_self_approval:
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.SelfApproval,
                based_on_statements=frozenset([statement]),  # type: ignore # noqa: PGH003
            )

        decision_based_on_statements.add(statement)  # type: ignore # noqa: PGH003
        potential_approvers.update(approver for approver in statement.approvers if approver != requester_email)

    if not decision_based_on_statements:
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
    permit: bool - Allow approver to make an action Approve if permit is True
    based_on_statements: FrozenSet[Statement]
    """

    grant: bool
    permit: bool
    based_on_statements: FrozenSet[Statement] | FrozenSet[GroupStatement]


class ExecuteDecisionResult(BaseModel):
    """Result of executing an access decision."""

    granted: bool
    schedule_name: str | None = None
    instance_arn: str | None = None
    permission_set_arn: str | None = None
    permission_set_name: str | None = None
    account_id: str | None = None
    user_principal_id: str | None = None
    # For group access
    group_id: str | None = None
    group_name: str | None = None
    identity_store_id: str | None = None
    membership_id: str | None = None


def make_decision_on_approve_request(  # noqa: PLR0913
    action: entities.ApproverAction,
    statements: frozenset[Statement],
    approver_email: str,
    requester_email: str,
    permission_set_name: str | None = None,
    account_id: str | None = None,
    group_id: str | None = None,
) -> ApproveRequestDecision:
    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)

    for statement in affected_statements:
        if approver_email in statement.approvers:
            is_self_approval = approver_email == requester_email
            if is_self_approval and statement.allow_self_approval or not is_self_approval:
                return ApproveRequestDecision(
                    grant=action == entities.ApproverAction.Approve,
                    permit=True,
                    based_on_statements=frozenset([statement]),  # type: ignore # noqa: PGH003
                )

    return ApproveRequestDecision(
        grant=False,
        permit=False,
        based_on_statements=affected_statements,  # type: ignore # noqa: PGH003
    )


def execute_decision(  # noqa: PLR0913
    decision: AccessRequestDecision | ApproveRequestDecision,
    permission_set_name: str,
    account_id: str,
    permission_duration: datetime.timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
    thread_ts: str | None = None,
) -> ExecuteDecisionResult:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return ExecuteDecisionResult(granted=False)

    identity_store_id = sso.get_identity_store_id(cfg, sso_client)
    permission_set = sso.get_permission_set(sso_client, cfg.sso_instance_arn, permission_set_name)
    sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
        identity_store_client=identitystore_client, identity_store_id=identity_store_id, email=requester.email, cfg=cfg
    )

    account_assignment = sso.UserAccountAssignment(
        instance_arn=cfg.sso_instance_arn,
        account_id=account_id,
        permission_set_arn=permission_set.arn,
        user_principal_id=sso_user_principal_id,
    )

    logger.info("Creating account assignment", extra={"account_assignment": account_assignment})

    account_assignment_status = sso.create_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )

    s3.log_operation(
        audit_entry=s3.AuditEntry(
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
            sso_user_principal_id=sso_user_principal_id,
            audit_entry_type="account",
            secondary_domain_was_used=secondary_domain_was_used,
        ),
    )

    _, schedule_name = schedule.schedule_revoke_event(
        permission_duration=permission_duration,
        schedule_client=schedule_client,
        approver=approver,
        requester=requester,
        user_account_assignment=sso.UserAccountAssignment(
            instance_arn=sso_instance.arn,
            account_id=account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=sso_user_principal_id,
        ),
        thread_ts=thread_ts,
    )

    return ExecuteDecisionResult(
        granted=True,
        schedule_name=schedule_name,
        instance_arn=sso_instance.arn,
        permission_set_arn=permission_set.arn,
        permission_set_name=permission_set.name,
        account_id=account_id,
        user_principal_id=sso_user_principal_id,
    )


def execute_decision_on_group_request(  # noqa: PLR0913
    decision: AccessRequestDecision | ApproveRequestDecision,
    group: entities.aws.SSOGroup,
    permission_duration: datetime.timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
    identity_store_id: str,
    thread_ts: str | None = None,
) -> ExecuteDecisionResult:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return ExecuteDecisionResult(granted=False)

    sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
        identity_store_client=identitystore_client,
        identity_store_id=identity_store_id,
        email=requester.email,
        cfg=cfg,
    )

    if membership_id := sso.is_user_in_group(
        identity_store_id=identity_store_id,
        group_id=group.id,
        sso_user_id=sso_user_principal_id,
        identity_store_client=identitystore_client,
    ):
        logger.info(
            "User is already in the group", extra={"group_id": group.id, "user_id": sso_user_principal_id, "membership_id": membership_id}
        )
    else:
        membership_id = sso.add_user_to_a_group(group.id, sso_user_principal_id, identity_store_id, identitystore_client)["MembershipId"]
        logger.info(
            "User added to the group", extra={"group_id": group.id, "user_id": sso_user_principal_id, "membership_id": membership_id}
        )

    s3.log_operation(
        audit_entry=s3.AuditEntry(
            group_name=group.name,
            group_id=group.id,
            reason=reason,
            requester_slack_id=requester.id,
            requester_email=requester.email,
            approver_slack_id=approver.id,
            approver_email=approver.email,
            operation_type="grant",
            permission_duration=permission_duration,
            audit_entry_type="group",
            sso_user_principal_id=sso_user_principal_id,
            secondary_domain_was_used=secondary_domain_was_used,
        ),
    )

    _, schedule_name = schedule.schedule_group_revoke_event(
        permission_duration=permission_duration,
        schedule_client=schedule_client,
        approver=approver,
        requester=requester,
        group_assignment=sso.GroupAssignment(
            identity_store_id=identity_store_id,
            group_name=group.name,
            group_id=group.id,
            user_principal_id=sso_user_principal_id,
            membership_id=membership_id,
        ),
        thread_ts=thread_ts,
    )

    return ExecuteDecisionResult(
        granted=True,
        schedule_name=schedule_name,
        group_id=group.id,
        group_name=group.name,
        identity_store_id=identity_store_id,
        membership_id=membership_id,
        user_principal_id=sso_user_principal_id,
    )
