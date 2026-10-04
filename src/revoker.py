from datetime import datetime, timedelta
from time import monotonic

import boto3
import botocore.exceptions
import slack_sdk
from mypy_boto3_events import EventBridgeClient
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_organizations import OrganizationsClient
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import ValidationError
from slack_sdk.web.slack_response import SlackResponse

import config
import entities
import organizations
import s3
import schedule
import slack_helpers
import sso
from events import (
    ApproverNotificationEvent,
    CheckOnInconsistency,
    DiscardButtonsEvent,
    Event,
    GroupRevokeEvent,
    RevokeEvent,
    ScheduledGroupRevokeEvent,
    ScheduledRevokeEvent,
    SSOElevatorScheduledRevocation,
)

logger = config.get_logger(service="revoker")

cfg = config.get_config()
org_client = boto3.client("organizations")  # type: ignore  # noqa: PGH003
sso_client = boto3.client("sso-admin")  # type: ignore # noqa: PGH003
identitystore_client = boto3.client("identitystore")  # type: ignore # noqa: PGH003
scheduler_client = boto3.client("scheduler")  # type: ignore # noqa: PGH003
events_client = boto3.client("events")  # type: ignore # noqa: PGH003
ssm_client = boto3.client("ssm")  # type: ignore # noqa: PGH003


def lambda_handler(event: dict, __) -> SlackResponse | None:  # type: ignore # noqa: ANN001, PGH003
    try:
        parsed_event = Event.model_validate(event).root
    except ValidationError as e:
        logger.warning("Got unexpected event:", extra={"event": event, "exception": e})
        raise e

    # Read per invocation, so a rotated token is picked up without waiting for a cold start.
    slack_token = config.get_slack_secret(ssm_client, config.SLACK_BOT_TOKEN_PARAMETER_ENV, degrade_on_failure=True)
    slack_client = slack_sdk.WebClient(token=slack_token)

    match parsed_event:
        case ScheduledRevokeEvent():
            logger.info("Handling ScheduledRevokeEvent", extra={"event": parsed_event})

            return handle_scheduled_account_assignment_deletion(
                revoke_event=parsed_event.revoke_event,
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
            )

        case ScheduledGroupRevokeEvent():
            logger.info("Handling GroupRevokeEvent", extra={"event": parsed_event})
            return handle_scheduled_group_assignment_deletion(
                group_revoke_event=parsed_event.revoke_event,
                cfg=cfg,
                scheduler_client=scheduler_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
            )

        case DiscardButtonsEvent():
            logger.info("Handling DiscardButtonsEvent", extra={"event": parsed_event})
            handle_discard_buttons_event(event=parsed_event, slack_client=slack_client, scheduler_client=scheduler_client)
            return

        case CheckOnInconsistency():
            logger.info("Handling CheckOnInconsistency event", extra={"event": parsed_event})
            check_on_groups_inconsistency(
                identitystore_client=identitystore_client,
                sso_client=sso_client,
                scheduler_client=scheduler_client,
                events_client=events_client,
                cfg=cfg,
                slack_client=slack_client,
            )
            return handle_check_on_inconsistency(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
                events_client=events_client,
            )

        case SSOElevatorScheduledRevocation():
            logger.info("Handling SSOElevatorScheduledRevocation event", extra={"event": parsed_event})
            sweep_audit = SweepAudit()
            handle_sso_elevator_group_scheduled_revocation(
                identitystore_client=identitystore_client,
                sso_client=sso_client,
                scheduler_client=scheduler_client,
                cfg=cfg,
                slack_client=slack_client,
                sweep_audit=sweep_audit,
            )
            return handle_sso_elevator_scheduled_revocation(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
                sweep_audit=sweep_audit,
            )
        case ApproverNotificationEvent():
            logger.info("Handling ApproverNotificationEvent event", extra={"event": parsed_event})
            return handle_approvers_renotification_event(
                event=parsed_event,
                slack_client=slack_client,
                scheduler_client=scheduler_client,
            )


class SweepAudit:
    """Audit writes for one reconciliation sweep. After the first failed or slow S3 write the rest skip S3
    and go to the log only, so a slow S3 cannot use up the run's time before every removal is done."""

    # A write that succeeds but takes this long still trips the breaker: a run has only ~30 s.
    SLOW_WRITE_SECONDS = 1

    def __init__(self) -> None:  # noqa: ANN101
        self.skip_s3 = False

    def log(self, audit_entry: s3.AuditEntry) -> None:  # noqa: ANN101
        if self.skip_s3:
            logger.warning(
                f"Skipped {audit_entry.operation_type} audit write to S3 after an earlier failed or slow write in this sweep",
                extra={"audit_entry": s3.audit_record(audit_entry)},
            )
            return
        started = monotonic()
        failed = s3.log_operation_best_effort(audit_entry) is not None
        self.skip_s3 = failed or monotonic() - started >= self.SLOW_WRITE_SECONDS


def handle_account_assignment_deletion(  # noqa: PLR0913
    account_assignment: sso.UserAccountAssignment,
    cfg: config.Config,
    sso_client: SSOAdminClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    sweep_audit: SweepAudit,
) -> SlackResponse | None:
    logger.info("Handling account assignment deletion", extra={"account_assignment": account_assignment})

    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )

    permission_set = sso.describe_permission_set(
        sso_client,
        account_assignment.instance_arn,
        account_assignment.permission_set_arn,
    )

    sweep_audit.log(
        s3.AuditEntry(
            role_name=permission_set.name,
            account_id=account_assignment.account_id,
            reason="automated revocation",
            requester_slack_id="NA",
            requester_email="NA",
            request_id=assignment_status.request_id,
            approver_slack_id="NA",
            approver_email="NA",
            operation_type="revoke",
            permission_duration="NA",
            sso_user_principal_id=account_assignment.user_principal_id,
            audit_entry_type="account",
        ),
    )

    if cfg.post_update_to_slack:
        # The revocation already succeeded. handle_sso_elevator_scheduled_revocation calls this
        # in a loop, so a Slack failure must not abort the remaining revocations.
        try:
            account = organizations.describe_account(org_client, account_assignment.account_id)
            return slack_notify_user_on_revoke(
                cfg=cfg,
                account_assignment=account_assignment,
                permission_set=permission_set,
                account=account,
                sso_client=sso_client,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
        except Exception as e:
            logger.exception(
                f"Failed to notify Slack about a completed revocation, the revocation itself succeeded: {e}",
                extra={"account_assignment": account_assignment},
            )
    return None


def slack_notify_user_on_revoke(  # noqa: PLR0913
    cfg: config.Config,
    account_assignment: sso.AccountAssignment | sso.UserAccountAssignment,
    permission_set: entities.aws.PermissionSet,
    account: entities.aws.Account,
    sso_client: SSOAdminClient,
    identitystore_client: IdentityStoreClient,
    slack_client: slack_sdk.WebClient,
) -> SlackResponse:
    mention = slack_helpers.create_slack_mention_by_principal_id(
        sso_user_id=(
            account_assignment.principal_id
            if isinstance(account_assignment, sso.AccountAssignment)
            else account_assignment.user_principal_id
        ),
        sso_client=sso_client,
        cfg=cfg,
        identitystore_client=identitystore_client,
        slack_client=slack_client,
    )
    subject = slack_helpers.account_subject(permission_set.name, account.name, account.id)
    return slack_client.chat_postMessage(channel=cfg.slack_channel_id, text=untracked_revocation_text(f"{subject} for {mention}"))


def slack_notify_user_on_group_access_revoke(  # noqa: PLR0913
    cfg: config.Config,
    group_assignment: sso.GroupAssignment,
    sso_client: SSOAdminClient,
    identitystore_client: IdentityStoreClient,
    slack_client: slack_sdk.WebClient,
) -> SlackResponse:
    mention = slack_helpers.create_slack_mention_by_principal_id(
        sso_user_id=group_assignment.user_principal_id,
        sso_client=sso_client,
        cfg=cfg,
        identitystore_client=identitystore_client,
        slack_client=slack_client,
    )
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=untracked_revocation_text(f"{mention} removed from {slack_helpers.group_subject(group_assignment.group_name)}"),
    )


def untracked_revocation_text(what: str) -> str:
    # "Untracked", not "outside Elevator": a missing request link does not prove where access came from.
    return f":broom: Revoked untracked access · {what}"


def report_scheduled_revocation(  # noqa: PLR0913
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
    revoke_event: RevokeEvent | GroupRevokeEvent,
    subject: str,
    ended: str,
    untracked: str,
) -> None:
    """Flips the request message to Ended with a thread reply; without one, posts
    a standalone notice. Never raises: the revocation itself already happened."""
    try:
        if revoke_event.channel_id and revoke_event.message_ts:
            if slack_helpers.end_request_message(
                slack_client,
                revoke_event.channel_id,
                revoke_event.message_ts,
                subject,
                revoke_event.requester.id,
                revoke_event.approver.id,
                f"Access ended: {ended}",
            ):
                return
            logger.warning("Request message not found for a scheduled revocation", extra={"revoke_event": revoke_event})
    except Exception as e:
        logger.exception(f"Failed to show a scheduled revocation on its request message: {e}")
    if not cfg.post_update_to_slack:
        return
    try:
        slack_client.chat_postMessage(channel=cfg.slack_channel_id, text=untracked_revocation_text(untracked))
    except Exception as e:
        logger.exception(f"Failed to post a revocation notice, the revocation itself succeeded: {e}")


def handle_scheduled_account_assignment_deletion(  # noqa: PLR0913
    revoke_event: RevokeEvent,
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
) -> None:
    logger.info("Handling scheduled account assignment deletion", extra={"revoke_event": revoke_event})

    user_account_assignment = revoke_event.user_account_assignment
    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        user_account_assignment,
    )
    permission_set = sso.describe_permission_set(
        sso_client,
        sso_instance_arn=user_account_assignment.instance_arn,
        permission_set_arn=user_account_assignment.permission_set_arn,
    )

    # Access is already gone: a failed audit write must not skip the cleanup and Slack update below.
    s3.log_operation_best_effort(
        s3.AuditEntry(
            role_name=permission_set.name,
            account_id=user_account_assignment.account_id,
            reason="scheduled_revocation",
            requester_slack_id=revoke_event.requester.id,
            requester_email=revoke_event.requester.email,
            request_id=assignment_status.request_id,
            approver_slack_id=revoke_event.approver.id,
            approver_email=revoke_event.approver.email,
            operation_type="revoke",
            permission_duration=revoke_event.permission_duration,
            sso_user_principal_id=user_account_assignment.user_principal_id,
            audit_entry_type="account",
        ),
    )
    schedule.delete_schedule(scheduler_client, revoke_event.schedule_name)

    account_id = user_account_assignment.account_id
    try:
        account_name = organizations.describe_account(org_client, account_id).name
    except Exception as e:
        logger.exception(f"Failed to describe account {account_id} for the revocation notice: {e}")
        account_name = account_id
    subject = slack_helpers.account_subject(permission_set.name, account_name, account_id)
    report_scheduled_revocation(
        cfg,
        slack_client,
        revoke_event,
        subject=subject,
        ended=f"{slack_helpers.escape_mrkdwn(permission_set.name)} removed from {slack_helpers.escape_mrkdwn(account_name)} #{account_id}",
        untracked=f"{subject} for <@{revoke_event.requester.id}>",
    )


def handle_scheduled_group_assignment_deletion(
    group_revoke_event: GroupRevokeEvent,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
) -> None:
    logger.info("Handling scheduled group access revokation", extra={"revoke_event": group_revoke_event})
    group_assignment = group_revoke_event.group_assignment
    try:
        sso.remove_user_from_group(group_assignment.identity_store_id, group_assignment.membership_id, identitystore_client)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        # Already removed, e.g. by the other of two revokes racing Approves scheduled for one grant (#212).
        logger.warning(f"Group membership already gone, nothing to revoke: {e}")
        schedule.delete_schedule(scheduler_client, group_revoke_event.schedule_name)
        return
    # As for accounts: the membership is gone, so the audit write is best-effort.
    s3.log_operation_best_effort(
        s3.AuditEntry(
            group_name=group_assignment.group_name,
            group_id=group_assignment.group_id,
            reason="scheduled_revocation",
            requester_slack_id=group_revoke_event.requester.id,
            requester_email=group_revoke_event.requester.email,
            approver_slack_id=group_revoke_event.approver.id,
            approver_email=group_revoke_event.approver.email,
            operation_type="revoke",
            permission_duration=group_revoke_event.permission_duration,
            sso_user_principal_id=group_assignment.user_principal_id,
            audit_entry_type="group",
        ),
    )
    schedule.delete_schedule(scheduler_client, group_revoke_event.schedule_name)
    group = slack_helpers.group_subject(group_assignment.group_name)
    report_scheduled_revocation(
        cfg,
        slack_client,
        group_revoke_event,
        subject=group,
        ended=f"removed from {group}",
        untracked=f"<@{group_revoke_event.requester.id}> removed from {group}",
    )


def handle_check_on_inconsistency(  # noqa: PLR0913
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    events_client: EventBridgeClient,
) -> None:
    account_assignments = sso.get_account_assignment_information(sso_client, cfg, org_client)
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    account_assignments_from_events = [
        sso.AccountAssignment(
            permission_set_arn=scheduled_event.revoke_event.user_account_assignment.permission_set_arn,
            account_id=scheduled_event.revoke_event.user_account_assignment.account_id,
            principal_id=scheduled_event.revoke_event.user_account_assignment.user_principal_id,
            principal_type="USER",
        )
        for scheduled_event in scheduled_revoke_events
        if isinstance(scheduled_event, ScheduledRevokeEvent)
    ]

    for account_assignment in account_assignments:
        if account_assignment not in account_assignments_from_events:
            account = organizations.describe_account(org_client, account_assignment.account_id)
            logger.warning("Found an inconsistent account assignment", extra={"account_assignment": account_assignment})
            mention = slack_helpers.create_slack_mention_by_principal_id(
                sso_user_id=(
                    account_assignment.principal_id
                    if isinstance(account_assignment, sso.AccountAssignment)
                    else account_assignment.user_principal_id
                ),
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
            rule = schedule.get_event_bridge_rule(
                event_bridge_client=events_client, rule_name=cfg.sso_elevator_scheduled_revocation_rule_name
            )
            next_run_time_or_expression = schedule.check_rule_expression_and_get_next_run(rule)
            time_notice = ""
            if isinstance(next_run_time_or_expression, datetime):
                time_notice = f" The next scheduled revocation is set for {next_run_time_or_expression}."
            elif isinstance(next_run_time_or_expression, str):
                time_notice = f" The revocation schedule is set as: {next_run_time_or_expression}."  # noqa: Q000

            slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=(
                    f"Inconsistent account assignment detected in {account.name}-{account.id} for {mention}. "
                    f"The unidentified assignment will be automatically revoked.{time_notice}"
                ),
            )


def check_on_groups_inconsistency(  # noqa: PLR0913
    identitystore_client: IdentityStoreClient,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    events_client: EventBridgeClient,
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
) -> None:
    sso_instance_arn = cfg.sso_instance_arn
    sso_instance = sso.describe_sso_instance(sso_client, sso_instance_arn)
    identity_store_id = sso_instance.identity_store_id
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    group_assignments = sso.get_group_assignments(identity_store_id, identitystore_client, cfg)
    group_assignments_from_events = [
        sso.GroupAssignment(
            group_name=scheduled_event.revoke_event.group_assignment.group_name,
            group_id=scheduled_event.revoke_event.group_assignment.group_id,
            user_principal_id=scheduled_event.revoke_event.group_assignment.user_principal_id,
            membership_id=scheduled_event.revoke_event.group_assignment.membership_id,
            identity_store_id=scheduled_event.revoke_event.group_assignment.identity_store_id,
        )
        for scheduled_event in scheduled_revoke_events
        if isinstance(scheduled_event, ScheduledGroupRevokeEvent)
    ]
    for group_assignment in group_assignments:
        if group_assignment not in group_assignments_from_events:
            logger.warning("Group assignment is not in the scheduled events", extra={"assignment": group_assignment})
            mention = slack_helpers.create_slack_mention_by_principal_id(
                sso_user_id=group_assignment.user_principal_id,
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
            rule = schedule.get_event_bridge_rule(
                event_bridge_client=events_client, rule_name=cfg.sso_elevator_scheduled_revocation_rule_name
            )
            next_run_time_or_expression = schedule.check_rule_expression_and_get_next_run(rule)
            time_notice = ""
            if isinstance(next_run_time_or_expression, datetime):
                time_notice = f" The next scheduled revocation is set for {next_run_time_or_expression}."
            elif isinstance(next_run_time_or_expression, str):
                time_notice = f" The revocation schedule is set as: {next_run_time_or_expression}."  # noqa: Q000
            slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=(
                    f"""Inconsistent group assignment detected in {group_assignment.group_name}-{group_assignment.group_id} for user {
                        mention
                    }."""
                    f"The unidentified assignment will be automatically revoked.{time_notice}"
                ),
            )


def handle_sso_elevator_group_scheduled_revocation(  # noqa: PLR0913
    identitystore_client: IdentityStoreClient,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
    sweep_audit: SweepAudit,
) -> None:
    sso_instance_arn = cfg.sso_instance_arn
    sso_instance = sso.describe_sso_instance(sso_client, sso_instance_arn)
    identity_store_id = sso_instance.identity_store_id
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    group_assignments = sso.get_group_assignments(identity_store_id, identitystore_client, cfg)
    group_assignments_from_events = [
        sso.GroupAssignment(
            group_name=scheduled_event.revoke_event.group_assignment.group_name,
            group_id=scheduled_event.revoke_event.group_assignment.group_id,
            user_principal_id=scheduled_event.revoke_event.group_assignment.user_principal_id,
            membership_id=scheduled_event.revoke_event.group_assignment.membership_id,
            identity_store_id=scheduled_event.revoke_event.group_assignment.identity_store_id,
        )
        for scheduled_event in scheduled_revoke_events
        if isinstance(scheduled_event, ScheduledGroupRevokeEvent)
    ]
    for group_assignment in group_assignments:
        if group_assignment in group_assignments_from_events:
            logger.info(
                "Group assignment already scheduled for revocation. Skipping.",
                extra={"group_assignment": group_assignment},
            )
            continue
        else:
            sso.remove_user_from_group(group_assignment.identity_store_id, group_assignment.membership_id, identitystore_client)
            sweep_audit.log(
                s3.AuditEntry(
                    group_name=group_assignment.group_name,
                    group_id=group_assignment.group_id,
                    reason="scheduled_revocation",
                    requester_slack_id="NA",
                    requester_email="NA",
                    approver_slack_id="NA",
                    approver_email="NA",
                    operation_type="revoke",
                    permission_duration="NA",
                    audit_entry_type="group",
                    sso_user_principal_id=group_assignment.user_principal_id,
                ),
            )
            if cfg.post_update_to_slack:
                # The removal already succeeded; a Slack failure must not abort the loop and
                # leave the remaining group assignments un-revoked.
                try:
                    slack_notify_user_on_group_access_revoke(
                        cfg=cfg,
                        group_assignment=group_assignment,
                        sso_client=sso_client,
                        identitystore_client=identitystore_client,
                        slack_client=slack_client,
                    )
                except Exception as e:
                    logger.exception(
                        f"Failed to notify Slack about a completed group revocation, the revocation itself succeeded: {e}",
                        extra={"group_assignment": group_assignment},
                    )


def handle_sso_elevator_scheduled_revocation(  # noqa: PLR0913
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    sweep_audit: SweepAudit,
) -> None:
    account_assignments = sso.get_account_assignment_information(sso_client, cfg, org_client)
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    account_assignments_from_events = [
        sso.AccountAssignment(
            permission_set_arn=scheduled_event.revoke_event.user_account_assignment.permission_set_arn,
            account_id=scheduled_event.revoke_event.user_account_assignment.account_id,
            principal_id=scheduled_event.revoke_event.user_account_assignment.user_principal_id,
            principal_type="USER",
        )
        for scheduled_event in scheduled_revoke_events
        if isinstance(scheduled_event, ScheduledRevokeEvent)
    ]
    for account_assignment in account_assignments:
        if account_assignment in account_assignments_from_events:
            logger.info(
                "Account assignment already scheduled for revocation. Skipping.",
                extra={"account_assignment": account_assignment},
            )
            continue
        else:
            handle_account_assignment_deletion(
                account_assignment=sso.UserAccountAssignment(
                    account_id=account_assignment.account_id,
                    permission_set_arn=account_assignment.permission_set_arn,
                    user_principal_id=account_assignment.principal_id,
                    instance_arn=sso_instance.arn,
                ),
                sso_client=sso_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
                cfg=cfg,
                sweep_audit=sweep_audit,
            )


def handle_discard_buttons_event(
    event: DiscardButtonsEvent, slack_client: slack_sdk.WebClient, scheduler_client: EventBridgeSchedulerClient
) -> None:
    message = slack_helpers.get_message_from_timestamp(
        channel_id=event.channel_id,
        message_ts=event.time_stamp,
        slack_client=slack_client,
    )
    schedule.delete_schedule(scheduler_client, event.schedule_name)
    if message is None:
        logger.warning("Message was not found", extra={"event": event})
        return
    if not any(slack_helpers.get_block_id(block) == "buttons" for block in message.get("blocks") or []):
        logger.info("Buttons were not found", extra={"event": event})
        return

    request = slack_helpers.pending_request(message)
    if request is None:
        slack_helpers.strip_buttons(slack_client, event.channel_id, message)
        logger.info("Buttons were removed from a pre-upgrade request", extra={"event": event})
        return
    card = slack_helpers.RequestCard.from_message(message, slack_helpers.request_subject(request), request.requester_slack_id)
    text, blocks = slack_helpers.build_request_message(
        card, slack_helpers.RequestState.expired(timedelta(hours=cfg.request_expiration_hours))
    )
    slack_client.chat_update(channel=event.channel_id, ts=message["ts"], blocks=blocks, text=text)
    logger.info("Request expired", extra={"event": event})
    _log_expired_request(request=request, slack_client=slack_client)


def _log_expired_request(
    request: slack_helpers.RequestForAccess | slack_helpers.RequestForGroupAccess, slack_client: slack_sdk.WebClient
) -> None:
    # Best-effort: the buttons are already gone, so an audit failure must not fail the event.
    try:
        requester_email = slack_helpers.get_user(slack_client, id=request.requester_slack_id).email
    except Exception as e:
        logger.exception(f"Failed to look up requester for expired request audit entry: {e}")
        requester_email = "NA"
    if isinstance(request, slack_helpers.RequestForGroupAccess):
        target = {"group_id": request.group_id, "group_name": request.group_name or "NA", "audit_entry_type": "group"}
    else:
        target = {
            "account_id": request.account_id,
            "role_name": request.permission_set_name,
            "audit_entry_type": "account",
            "request_source": request.request_source,
            "verified_arn": request.verified_arn,
        }
    s3.log_operation_best_effort(
        s3.AuditEntry(
            reason=request.reason,
            requester_slack_id=request.requester_slack_id,
            requester_email=requester_email,
            operation_type="declined",
            permission_duration=request.permission_duration,
            sso_user_principal_id="NA",
            decision_reason="Expired",
            **target,
        )
    )


def handle_approvers_renotification_event(
    event: ApproverNotificationEvent, slack_client: slack_sdk.WebClient, scheduler_client: EventBridgeSchedulerClient
) -> None:
    message = slack_helpers.get_message_from_timestamp(
        channel_id=event.channel_id,
        message_ts=event.time_stamp,
        slack_client=slack_client,
    )
    schedule.delete_schedule(scheduler_client, event.schedule_name)
    if message is None:
        logger.warning("Message not found", extra={"event": event})
        return

    for block in message.get("blocks") or []:
        if slack_helpers.get_block_id(block) == "buttons":
            time_to_wait = timedelta(seconds=event.time_to_wait_in_seconds)
            if cfg.approver_renotification_backoff_multiplier != 0:
                time_to_wait = time_to_wait * cfg.approver_renotification_backoff_multiplier
            slack_response = slack_client.chat_postMessage(
                channel=event.channel_id,
                thread_ts=message["ts"],
                text="The request is still awaiting approval. The next reminder will be "
                f"sent in {time_to_wait.seconds // 60} minutes, "
                "unless the request is approved or discarded beforehand.",
            )
            logger.info("Notifications to approvers were sent.")
            logger.debug("Slack response:", extra={"slack_response": slack_response})

            schedule.schedule_approver_notification_event(
                schedule_client=scheduler_client, channel_id=event.channel_id, message_ts=message["ts"], time_to_wait=time_to_wait
            )
            return

    logger.info("The request has already been approved or discarded.", extra={"event": event})
    return
