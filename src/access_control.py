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
from statement import GroupStatement, Statement, get_affected_group_statements, get_affected_statements, requester_allowed

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
    RequesterNotAllowed = "RequesterNotAllowed"


class AccessRequestDecision(BaseModel):
    grant: bool
    reason: DecisionReason
    based_on_statements: FrozenSet[Statement] | FrozenSet[GroupStatement]
    approvers: FrozenSet[str] = frozenset()


def requester_email_variants(requester_email: str) -> FrozenSet[str]:
    """Lowercased requester address plus local-part + each ``secondary_fallback_email_domains`` entry.

    Mirrors the fallback used by :func:`sso.get_user_principal_id_by_email`, so ``allowed_users``
    matches whichever address variant an admin configured.
    """
    email = (requester_email or "").strip()
    if not email:
        return frozenset()
    variants: set[str] = {email.lower()}
    if "@" in email:
        first_part, _ = email.split("@", 1)
        for domain in cfg.secondary_fallback_email_domains or []:
            variants.add((first_part + domain).lower())
    return frozenset(variants)


def get_requester_group_ids(requester_email: str) -> FrozenSet[str]:
    """Resolve the SSO group IDs the requester belongs to.

    Lookup errors are propagated so they cannot be confused with a successful lookup returning
    no memberships.
    """
    try:
        sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
        user_principal_id, _ = sso.get_user_principal_id_by_email(
            identity_store_client=identitystore_client,
            identity_store_id=sso_instance.identity_store_id,
            email=requester_email,
            cfg=cfg,
        )
        return sso.list_groups_for_user(sso_instance.identity_store_id, user_principal_id, identitystore_client)
    except Exception as error:  # noqa: BLE001
        logger.exception(f"Could not resolve requester group memberships: {error}")
        raise


def get_requester_group_ids_if_needed(
    statements: FrozenSet[Statement] | FrozenSet[GroupStatement],
    requester_email: str,
) -> FrozenSet[str]:
    """Resolve requester group memberships only when some statement restricts by ``allowed_groups``."""
    if not any(statement.allowed_groups for statement in statements):
        return frozenset()
    return get_requester_group_ids(requester_email)


def _filter_statements_for_requester(
    statements: FrozenSet[Statement] | FrozenSet[GroupStatement],
    requester_email: str,
    requester_group_ids: FrozenSet[str],
) -> FrozenSet[Statement] | FrozenSet[GroupStatement]:
    """Drop statements the requester isn't eligible for, based on ``allowed_groups``/``allowed_users``."""
    emails = requester_email_variants(requester_email)
    return frozenset(st for st in statements if requester_allowed(st, emails, requester_group_ids))  # type: ignore # noqa: PGH003


def eligible_group_ids(
    statements: FrozenSet[GroupStatement],
    requester_email: str,
    requester_group_ids: FrozenSet[str],
) -> FrozenSet[str]:
    """Group IDs the requester may request, per each statement's ``allowed_groups``/``allowed_users``.

    Used to filter the request modal so a requester only sees groups they can actually request.
    """
    emails = requester_email_variants(requester_email)
    return frozenset(
        group_id for statement in statements if requester_allowed(statement, emails, requester_group_ids) for group_id in statement.resource
    )


def eligible_accounts_and_permission_sets(
    statements: FrozenSet[Statement],
    requester_email: str,
    requester_group_ids: FrozenSet[str],
) -> tuple[FrozenSet[str] | None, FrozenSet[str] | None]:
    """Account IDs and permission-set names the requester may request, per ``allowed_groups``/``allowed_users``.

    ``None`` for either element means "unrestricted" — a ``*`` wildcard appeared in an eligible
    statement, so all accounts / permission sets should be shown.
    """
    emails = requester_email_variants(requester_email)
    accounts: set[str] = set()
    permission_sets: set[str] = set()
    accounts_wildcard = False
    permission_sets_wildcard = False
    for statement in statements:
        if not requester_allowed(statement, emails, requester_group_ids):
            continue
        if "*" in statement.permission_set:
            permission_sets_wildcard = True
        else:
            permission_sets.update(statement.permission_set)
        if statement.resource_type == "Account":
            if "*" in statement.resource:
                accounts_wildcard = True
            else:
                accounts.update(statement.resource)
    return (
        None if accounts_wildcard else frozenset(accounts),
        None if permission_sets_wildcard else frozenset(permission_sets),
    )


def filter_account_request_options(  # noqa: PLR0913
    accounts: list[entities.aws.Account],
    permission_sets: list[entities.aws.PermissionSet],
    statements: FrozenSet[Statement],
    requester_email: str,
    requester_group_ids: FrozenSet[str],
) -> tuple[list[entities.aws.Account], list[entities.aws.PermissionSet]]:
    """Filter the request modal's account/permission-set options to what the requester may request."""
    allowed_accounts, allowed_permission_sets = eligible_accounts_and_permission_sets(statements, requester_email, requester_group_ids)
    if allowed_accounts is not None:
        accounts = [account for account in accounts if account.id in allowed_accounts]
    if allowed_permission_sets is not None:
        permission_sets = [permission_set for permission_set in permission_sets if permission_set.name in allowed_permission_sets]
    return accounts, permission_sets


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
    requester_group_ids: FrozenSet[str] = frozenset(),
) -> AccessRequestDecision:
    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)
    eligible_statements = _filter_statements_for_requester(affected_statements, requester_email, requester_group_ids)
    if affected_statements and not eligible_statements:
        return AccessRequestDecision(
            grant=False,
            reason=DecisionReason.RequesterNotAllowed,
            based_on_statements=frozenset(),
        )
    affected_statements = eligible_statements

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


def make_decision_on_approve_request(  # noqa: PLR0913
    action: entities.ApproverAction,
    statements: frozenset[Statement] | frozenset[GroupStatement],
    approver_email: str,
    requester_email: str,
    permission_set_name: str | None = None,
    account_id: str | None = None,
    group_id: str | None = None,
    requester_group_ids: FrozenSet[str] = frozenset(),
) -> ApproveRequestDecision:
    affected_statements = determine_affected_statements(statements, account_id, permission_set_name, group_id)
    affected_statements = _filter_statements_for_requester(affected_statements, requester_email, requester_group_ids)

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
    request_source: str = "slack",
    verified_arn: str = "NA",
    verified_user_id: str = "NA",
) -> bool:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, permission_set_name)
    if request_source == "cli" and verified_user_id != "NA":
        # Grant against the exact UserId the CLI's SigV4-verified session was
        # actually checked against at submission time (cli_auth.extract_identity,
        # cross-checked again by handle_cli_access_request's email round-trip),
        # not a fresh, independent lookup by requester.email -- re-resolving
        # here is a *second* identity resolution that could disagree with the
        # one actually verified (a directory change between submit and
        # approve, or a primary-email lookup that legitimately falls through
        # to a different person via the secondary-domain fallback), silently
        # granting to someone other than the verified caller. If
        # verified_user_id no longer names a real Identity Store user,
        # create_account_assignment_and_wait_for_result below fails outright
        # rather than silently substituting a different, currently-resolvable
        # user -- fail closed instead of granting to the wrong person.
        sso_user_principal_id = verified_user_id
        secondary_domain_was_used = False
    else:
        sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
            identity_store_client=identitystore_client, identity_store_id=sso_instance.identity_store_id, email=requester.email, cfg=cfg
        )

    account_assignment = sso.UserAccountAssignment(
        instance_arn=sso_instance.arn,
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
            request_source=request_source,
            verified_arn=verified_arn,
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
            user_principal_id=sso_user_principal_id,
        ),
    )
    return True  # Temporary solution for testing


def execute_decision_on_group_request(  # noqa: PLR0913
    decision: AccessRequestDecision | ApproveRequestDecision,
    group: entities.aws.SSOGroup,
    permission_duration: datetime.timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
    identity_store_id: str,
) -> bool:
    logger.info("Executing decision")
    if not decision.grant:
        logger.info("Access request denied")
        return False  # Temporary solution for testing

    sso_user_principal_id, secondary_domain_was_used = sso.get_user_principal_id_by_email(
        identity_store_client=identitystore_client,
        identity_store_id=sso.describe_sso_instance(sso_client, cfg.sso_instance_arn).identity_store_id,
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

    schedule.schedule_group_revoke_event(
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
    )
    return  # type: ignore # noqa: PGH003
