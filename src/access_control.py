from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, FrozenSet

import boto3

import config
import entities
import s3
import schedule
import sso
from entities import BaseModel
from statement import GroupStatement, Statement, get_affected_group_statements, get_affected_statements

if TYPE_CHECKING:
    from datetime import timedelta
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_organizations import OrganizationsClient
    from mypy_boto3_scheduler import EventBridgeSchedulerClient
    from mypy_boto3_sso_admin import SSOAdminClient

logger = config.get_logger("access_control")
cfg = config.get_config()

# Lazy AWS clients — initialized on first use, not at import time.
_org_client: Any = None
_sso_client: Any = None
_identitystore_client: Any = None
_schedule_client: Any = None


def _get_org_client() -> OrganizationsClient:
    global _org_client  # noqa: PLW0603
    if _org_client is None:
        _org_client = boto3.client("organizations")  # type: ignore[assignment]
    return _org_client


def _get_sso_client() -> SSOAdminClient:
    global _sso_client  # noqa: PLW0603
    if _sso_client is None:
        _sso_client = boto3.client("sso-admin")  # type: ignore[assignment]
    return _sso_client


def _get_identitystore_client() -> IdentityStoreClient:
    global _identitystore_client  # noqa: PLW0603
    if _identitystore_client is None:
        _identitystore_client = boto3.client("identitystore")  # type: ignore[assignment]
    return _identitystore_client


def _get_schedule_client() -> EventBridgeSchedulerClient:
    global _schedule_client  # noqa: PLW0603
    if _schedule_client is None:
        _schedule_client = boto3.client("scheduler")  # type: ignore[assignment]
    return _schedule_client


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


def _approver_in_configured_approvers(approver_email: str, approvers: frozenset) -> bool:
    """``approvers`` from policy vs. Teams: match exact/case, or ``local@`` + ``secondary_fallback_email_domains`` (same as SSO)."""
    candidates = sso.email_variants_with_secondary_domains(approver_email, cfg)
    allowed = {str(a).strip().lower() for a in approvers if str(a).strip()}
    return bool(candidates & allowed)


def _requester_is_same_user_as_approver(approver_email: str, requester_email: str) -> bool:
    """Same person if addresses overlap, including after secondary domain expansion."""
    ae = (approver_email or "").strip()
    re = (requester_email or "").strip()
    if not ae and not re:
        return True
    if not ae or not re:
        return False
    return bool(sso.email_variants_with_secondary_domains(ae, cfg) & sso.email_variants_with_secondary_domains(re, cfg))


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


def make_decision_on_access_request(  # noqa: PLR0911
    statements: FrozenSet[Statement] | FrozenSet[GroupStatement],
    requester_email: str,
    permission_set_name: str | None = None,
    account_id: str | None = None,
    group_id: str | None = None,
) -> AccessRequestDecision:
    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)

    decision_based_on_statements: set[Statement] | set[GroupStatement] = set()
    potential_approvers = set()

    explicit_deny_self_approval = any(
        statement.allow_self_approval is False and _approver_in_configured_approvers(requester_email, statement.approvers)
        for statement in affected_statements
    )
    explicit_deny_approval_not_required = any(statement.approval_is_not_required is False for statement in affected_statements)

    for statement in affected_statements:
        if statement.approval_is_not_required and not explicit_deny_approval_not_required:
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset([statement]),  # type: ignore # noqa: PGH003
            )
        if (
            _approver_in_configured_approvers(requester_email, statement.approvers)
            and statement.allow_self_approval
            and not explicit_deny_self_approval
        ):
            return AccessRequestDecision(
                grant=True,
                reason=DecisionReason.SelfApproval,
                based_on_statements=frozenset([statement]),  # type: ignore # noqa: PGH003
            )

        decision_based_on_statements.add(statement)  # type: ignore # noqa: PGH003
        potential_approvers.update(
            approver for approver in statement.approvers if not _requester_is_same_user_as_approver(approver, requester_email)
        )

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


def make_decision_on_approve_request(  # noqa: PLR0913
    action: entities.ApproverAction,
    statements: frozenset[Statement] | frozenset[GroupStatement],
    approver_email: str,
    requester_email: str,
    permission_set_name: str | None = None,
    account_id: str | None = None,
    group_id: str | None = None,
) -> ApproveRequestDecision:
    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)

    for statement in affected_statements:
        if not _approver_in_configured_approvers(approver_email, statement.approvers):
            continue
        is_self_approval = _requester_is_same_user_as_approver(approver_email, requester_email)
        if (is_self_approval and statement.allow_self_approval) or (not is_self_approval):
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
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
    elevator_request_id: str | None = None,
) -> bool:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    sso_instance = sso.describe_sso_instance(_get_sso_client(), cfg.sso_instance_arn)
    permission_set = sso.get_permission_set_by_name(_get_sso_client(), sso_instance.arn, permission_set_name)
    sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
        identity_store_client=_get_identitystore_client(), identity_store_id=sso_instance.identity_store_id, email=requester.email, cfg=cfg
    )

    account_assignment = sso.UserAccountAssignment(
        instance_arn=sso_instance.arn,
        account_id=account_id,
        permission_set_arn=permission_set.arn,
        user_principal_id=sso_user_principal_id,
    )

    logger.info("Creating account assignment", extra={"account_assignment": account_assignment})

    account_assignment_status = sso.create_account_assignment_and_wait_for_result(
        _get_sso_client(),
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
            elevator_request_id=elevator_request_id or "NA",
            operation_type="grant",
            permission_duration=permission_duration,
            sso_user_principal_id=sso_user_principal_id,
            audit_entry_type="account",
            secondary_domain_was_used=secondary_domain_was_used,
        ),
    )

    schedule.schedule_revoke_event(
        permission_duration=permission_duration,
        schedule_client=_get_schedule_client(),
        approver=approver,
        requester=requester,
        user_account_assignment=sso.UserAccountAssignment(
            instance_arn=sso_instance.arn,
            account_id=account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=sso_user_principal_id,
        ),
        elevator_request_id=elevator_request_id,
    )
    return True  # Temporary solution for testing


def execute_decision_on_group_request(  # noqa: PLR0913
    decision: AccessRequestDecision | ApproveRequestDecision,
    group: entities.aws.SSOGroup,
    permission_duration: timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
    identity_store_id: str,
    elevator_request_id: str | None = None,
) -> bool:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
        identity_store_client=_get_identitystore_client(),
        identity_store_id=sso.describe_sso_instance(_get_sso_client(), cfg.sso_instance_arn).identity_store_id,
        email=requester.email,
        cfg=cfg,
    )

    if membership_id := sso.is_user_in_group(
        identity_store_id=identity_store_id,
        group_id=group.id,
        sso_user_id=sso_user_principal_id,
        identity_store_client=_get_identitystore_client(),
    ):
        logger.info(
            "User is already in the group", extra={"group_id": group.id, "user_id": sso_user_principal_id, "membership_id": membership_id}
        )
    else:
        membership_id = sso.add_user_to_a_group(group.id, sso_user_principal_id, identity_store_id, _get_identitystore_client())[
            "MembershipId"
        ]
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
            elevator_request_id=elevator_request_id or "NA",
            operation_type="grant",
            permission_duration=permission_duration,
            audit_entry_type="group",
            sso_user_principal_id=sso_user_principal_id,
            secondary_domain_was_used=secondary_domain_was_used,
        ),
    )

    schedule.schedule_group_revoke_event(
        permission_duration=permission_duration,
        schedule_client=_get_schedule_client(),
        approver=approver,
        requester=requester,
        group_assignment=sso.GroupAssignment(
            identity_store_id=identity_store_id,
            group_name=group.name,
            group_id=group.id,
            user_principal_id=sso_user_principal_id,
            membership_id=membership_id,
        ),
        elevator_request_id=elevator_request_id,
    )
    return True  # type: ignore # noqa: PGH003
