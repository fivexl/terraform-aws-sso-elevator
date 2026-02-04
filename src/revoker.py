from datetime import datetime, timedelta, timezone

import boto3
import slack_sdk
from mypy_boto3_cloudwatch import CloudWatchClient
from mypy_boto3_events import EventBridgeClient
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_organizations import OrganizationsClient
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import ValidationError
from slack_sdk.web.slack_response import SlackResponse

import analytics
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
cloudwatch_client: CloudWatchClient = boto3.client("cloudwatch")  # type: ignore # noqa: PGH003
slack_client = slack_sdk.WebClient(token=cfg.slack_bot_token)


def publish_stale_grants_metric(count: int, cloudwatch: CloudWatchClient) -> None:
    """Publish a CloudWatch metric for stale grants detected.

    This metric can be used to trigger alarms when grants exist that should have been revoked.
    """
    if count > 0:
        logger.info("Publishing stale grants metric", extra={"count": count})
    cloudwatch.put_metric_data(
        Namespace="SSOElevator",
        MetricData=[
            {
                "MetricName": "StaleGrantsDetected",
                "Value": count,
                "Unit": "Count",
                "Timestamp": datetime.now(timezone.utc),
            }
        ],
    )


def lambda_handler(event: dict, __) -> SlackResponse | None:  # type: ignore # noqa: ANN001, PGH003
    try:
        parsed_event = Event.model_validate(event).root
    except ValidationError as e:
        logger.warning("Got unexpected event:", extra={"event": event, "exception": e})
        raise e

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
                identitystore_client=identitystore_client,
            )

        case ScheduledGroupRevokeEvent():
            logger.info("Handling GroupRevokeEvent", extra={"event": parsed_event})
            return handle_scheduled_group_assignment_deletion(
                group_revoke_event=parsed_event.revoke_event,
                sso_client=sso_client,
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
            stale_group_count = check_on_groups_inconsistency(
                identity_store_client=identitystore_client,
                sso_client=sso_client,
                scheduler_client=scheduler_client,
                events_client=events_client,
                cfg=cfg,
                slack_client=slack_client,
            )
            stale_account_count = handle_check_on_inconsistency(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
                events_client=events_client,
            )
            publish_stale_grants_metric(stale_group_count + stale_account_count, cloudwatch_client)
            return

        case SSOElevatorScheduledRevocation():
            logger.info("Handling SSOElevatorScheduledRevocation event", extra={"event": parsed_event})
            handle_sso_elevator_group_scheduled_revocation(
                identity_store_client=identitystore_client,
                sso_client=sso_client,
                scheduler_client=scheduler_client,
                cfg=cfg,
                slack_client=slack_client,
            )
            return handle_sso_elevator_scheduled_revocation(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
            )
        case ApproverNotificationEvent():
            logger.info("Handling ApproverNotificationEvent event", extra={"event": parsed_event})
            return handle_approvers_renotification_event(
                event=parsed_event,
                slack_client=slack_client,
                scheduler_client=scheduler_client,
            )


def handle_early_account_revocation(  # noqa: PLR0913
    user_account_assignment: sso.UserAccountAssignment,
    schedule_name: str,
    revoker_slack_id: str,
    requester_slack_id: str,
    reason: str | None,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    cfg: config.Config,
    thread_ts: str | None = None,
) -> SlackResponse | None:
    """Handle early revocation of account access.

    Order of operations:
    1. Revoke SSO assignment FIRST (so if delete_schedule fails, perms are still revoked)
    2. Delete schedule (already handles ResourceNotFoundException gracefully)
    3. Log to S3 audit trail
    4. Post confirmation to thread
    """
    logger.info("Handling early account revocation", extra={"schedule_name": schedule_name})

    # 1. Revoke SSO assignment first
    try:
        assignment_status = sso.delete_account_assignment_and_wait_for_result(
            sso_client,
            user_account_assignment,
        )
    except Exception as e:
        logger.error("Failed to delete account assignment during early revocation", extra={"error": str(e)})
        raise

    permission_set = sso.describe_permission_set(
        sso_client,
        user_account_assignment.instance_arn,
        user_account_assignment.permission_set_arn,
    )

    # 2. Delete the scheduled revocation (handles ResourceNotFoundException)
    schedule.delete_schedule(scheduler_client, schedule_name)

    # 3. Log to S3 audit trail
    revoker = slack_helpers.get_user(slack_client, id=revoker_slack_id)
    requester = slack_helpers.get_user(slack_client, id=requester_slack_id)

    s3.log_operation(
        s3.AuditEntry(
            role_name=permission_set.name,
            account_id=user_account_assignment.account_id,
            reason=reason or "early_revocation",
            requester_slack_id=requester.id,
            requester_email=requester.email,
            request_id=assignment_status.request_id,
            approver_slack_id=revoker.id,
            approver_email=revoker.email,
            operation_type="early_revoke",
            permission_duration="NA",
            sso_user_principal_id=user_account_assignment.user_principal_id,
            audit_entry_type="account",
        ),
    )

    analytics.capture(
        event="aws_access_revoked_early",
        distinct_id=requester.email,
        properties={
            "account_id": user_account_assignment.account_id,
            "permission_set": permission_set.name,
            "revoker_email": revoker.email,
            "requester_email": requester.email,
            "reason": reason or "",
        },
    )

    # 4. Update header and post confirmation to thread
    logger.info(
        "Posting early revoke Slack updates",
        extra={"post_update_to_slack": cfg.post_update_to_slack, "thread_ts": thread_ts, "schedule_name": schedule_name},
    )
    if cfg.post_update_to_slack and thread_ts:
        # Update the original message header to ACCESS ENDED
        message = slack_helpers.get_message_from_timestamp(
            channel_id=cfg.slack_channel_id,
            message_ts=thread_ts,
            slack_client=slack_client,
        )
        if message:
            blocks = slack_helpers.HeaderSectionBlock.set_status(
                blocks=message["blocks"],
                status_text=cfg.access_ended_status,
            )
            slack_client.chat_update(
                channel=cfg.slack_channel_id,
                ts=thread_ts,
                blocks=blocks,
                text="Access ended",
            )

        reason_text = f" Reason: {reason}" if reason else ""
        text = f"<@{revoker_slack_id}> ended the session early.{reason_text}"
        slack_helpers.delete_early_revoke_button(slack_client, cfg.slack_channel_id, thread_ts)
        return slack_client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=text,
            thread_ts=thread_ts,
        )
    elif cfg.post_update_to_slack:
        # Full message when not in a thread (fallback)
        logger.info("No thread_ts available, posting to channel instead")
        try:
            account = organizations.describe_account(org_client, user_account_assignment.account_id)
            mention = slack_helpers.create_slack_mention_by_principal_id(
                sso_user_id=user_account_assignment.user_principal_id,
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
            reason_text = f" Reason: {reason}" if reason else ""
            text = (
                f"<@{revoker_slack_id}> ended the session early for {mention} (role {permission_set.name} in {account.name}).{reason_text}"
            )
            return slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=text,
            )
        except Exception as e:
            logger.error("Failed to post early revoke message to channel", extra={"error": str(e)})
            return slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"<@{revoker_slack_id}> ended a session early.",
            )
    else:
        logger.info("Slack updates disabled (post_update_to_slack=False)")


def handle_early_group_revocation(  # noqa: PLR0913
    group_assignment: sso.GroupAssignment,
    schedule_name: str,
    revoker_slack_id: str,
    requester_slack_id: str,
    reason: str | None,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    cfg: config.Config,
    thread_ts: str | None = None,
) -> SlackResponse | None:
    """Handle early revocation of group access.

    Order of operations:
    1. Remove user from group FIRST
    2. Delete schedule
    3. Log to S3 audit trail
    4. Post confirmation to thread
    """
    logger.info("Handling early group revocation", extra={"schedule_name": schedule_name})

    # 1. Remove user from group first
    try:
        sso.remove_user_from_group(
            group_assignment.identity_store_id,
            group_assignment.membership_id,
            identitystore_client,
        )
    except Exception as e:
        logger.error("Failed to remove user from group during early revocation", extra={"error": str(e)})
        raise

    # 2. Delete the scheduled revocation
    schedule.delete_schedule(scheduler_client, schedule_name)

    # 3. Log to S3 audit trail
    revoker = slack_helpers.get_user(slack_client, id=revoker_slack_id)
    requester = slack_helpers.get_user(slack_client, id=requester_slack_id)

    s3.log_operation(
        audit_entry=s3.AuditEntry(
            group_name=group_assignment.group_name,
            group_id=group_assignment.group_id,
            reason=reason or "early_revocation",
            requester_slack_id=requester.id,
            requester_email=requester.email,
            approver_slack_id=revoker.id,
            approver_email=revoker.email,
            operation_type="early_revoke",
            permission_duration="NA",
            sso_user_principal_id=group_assignment.user_principal_id,
            audit_entry_type="group",
        ),
    )

    # 4. Update header and post confirmation to thread
    logger.info(
        "Posting early revoke Slack updates",
        extra={"post_update_to_slack": cfg.post_update_to_slack, "thread_ts": thread_ts, "schedule_name": schedule_name},
    )
    if cfg.post_update_to_slack and thread_ts:
        # Update the original message header to ACCESS ENDED
        message = slack_helpers.get_message_from_timestamp(
            channel_id=cfg.slack_channel_id,
            message_ts=thread_ts,
            slack_client=slack_client,
        )
        if message:
            blocks = slack_helpers.HeaderSectionBlock.set_status(
                blocks=message["blocks"],
                status_text=cfg.access_ended_status,
            )
            slack_client.chat_update(
                channel=cfg.slack_channel_id,
                ts=thread_ts,
                blocks=blocks,
                text="Access ended",
            )

        reason_text = f" Reason: {reason}" if reason else ""
        text = f"<@{revoker_slack_id}> ended the session early.{reason_text}"
        slack_helpers.delete_early_revoke_button(slack_client, cfg.slack_channel_id, thread_ts)
        return slack_client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=text,
            thread_ts=thread_ts,
        )
    elif cfg.post_update_to_slack:
        # Full message when not in a thread (fallback)
        logger.info("No thread_ts available, posting to channel instead")
        try:
            mention = slack_helpers.create_slack_mention_by_principal_id(
                sso_user_id=group_assignment.user_principal_id,
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
            reason_text = f" Reason: {reason}" if reason else ""
            text = f"<@{revoker_slack_id}> ended the session early for {mention} (group {group_assignment.group_name}).{reason_text}"
            return slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=text,
            )
        except Exception as e:
            logger.error("Failed to post early revoke message to channel", extra={"error": str(e)})
            return slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"<@{revoker_slack_id}> ended a session early.",
            )
    else:
        logger.info("Slack updates disabled (post_update_to_slack=False)")


def handle_account_assignment_deletion(  # noqa: PLR0913
    account_assignment: sso.UserAccountAssignment,
    cfg: config.Config,
    sso_client: SSOAdminClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
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

    s3.log_operation(
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
        try:
            account = organizations.describe_account(org_client, account_assignment.account_id)
        except Exception:
            logger.warning("Failed to describe account, using account ID as fallback", extra={"account_id": account_assignment.account_id})
            account = entities.aws.Account(id=account_assignment.account_id, name=account_assignment.account_id)
        return slack_notify_user_on_revoke(
            cfg=cfg,
            account_assignment=account_assignment,
            permission_set=permission_set,
            account=account,
            sso_client=sso_client,
            identitystore_client=identitystore_client,
            slack_client=slack_client,
        )


def slack_notify_user_on_revoke(  # noqa: PLR0913
    cfg: config.Config,
    account_assignment: sso.AccountAssignment | sso.UserAccountAssignment,
    permission_set: entities.aws.PermissionSet,
    account: entities.aws.Account,
    sso_client: SSOAdminClient,
    identitystore_client: IdentityStoreClient,
    slack_client: slack_sdk.WebClient,
    thread_ts: str | None = None,
) -> SlackResponse:
    if thread_ts:
        # Update the original message header to ACCESS ENDED
        message = slack_helpers.get_message_from_timestamp(
            channel_id=cfg.slack_channel_id,
            message_ts=thread_ts,
            slack_client=slack_client,
        )
        if message:
            blocks = slack_helpers.HeaderSectionBlock.set_status(
                blocks=message["blocks"],
                status_text=cfg.access_ended_status,
            )
            slack_client.chat_update(
                channel=cfg.slack_channel_id,
                ts=thread_ts,
                blocks=blocks,
                text="Access ended",
            )
        # Delete the early revoke button from the thread
        slack_helpers.delete_early_revoke_button(slack_client, cfg.slack_channel_id, thread_ts)
        text = "Session complete."
    else:
        # Full message when not in a thread
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
        text = f"Revoked role {permission_set.name} for user {mention} in account {account.name}"
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=text,
        thread_ts=thread_ts,
    )


def slack_notify_user_on_group_access_revoke(  # noqa: PLR0913
    cfg: config.Config,
    group_assignment: sso.GroupAssignment,
    sso_client: SSOAdminClient,
    identitystore_client: IdentityStoreClient,
    slack_client: slack_sdk.WebClient,
    thread_ts: str | None = None,
) -> SlackResponse:
    if thread_ts:
        # Update the original message header to ACCESS ENDED
        message = slack_helpers.get_message_from_timestamp(
            channel_id=cfg.slack_channel_id,
            message_ts=thread_ts,
            slack_client=slack_client,
        )
        if message:
            blocks = slack_helpers.HeaderSectionBlock.set_status(
                blocks=message["blocks"],
                status_text=cfg.access_ended_status,
            )
            slack_client.chat_update(
                channel=cfg.slack_channel_id,
                ts=thread_ts,
                blocks=blocks,
                text="Access ended",
            )
        # Delete the early revoke button from the thread
        slack_helpers.delete_early_revoke_button(slack_client, cfg.slack_channel_id, thread_ts)
        text = "Session complete."
    else:
        # Full message when not in a thread
        mention = slack_helpers.create_slack_mention_by_principal_id(
            sso_user_id=group_assignment.user_principal_id,
            sso_client=sso_client,
            cfg=cfg,
            identitystore_client=identitystore_client,
            slack_client=slack_client,
        )
        text = f"User {mention} has been removed from the group {group_assignment.group_name}."
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=text,
        thread_ts=thread_ts,
    )


def handle_scheduled_account_assignment_deletion(  # noqa: PLR0913
    revoke_event: RevokeEvent,
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
) -> SlackResponse | None:
    logger.info("Handling scheduled account assignment deletion", extra={"revoke_event": revoke_event})

    user_account_assignment = revoke_event.user_account_assignment

    # Handle idempotency: assignment may have already been deleted by early revoke
    try:
        assignment_status = sso.delete_account_assignment_and_wait_for_result(
            sso_client,
            user_account_assignment,
        )
    except Exception as e:
        error_code = getattr(getattr(e, "response", {}), "get", lambda *_: None)("Error", {}).get("Code")
        # Check for botocore ClientError
        if hasattr(e, "response"):
            import jmespath as jp

            error_code = jp.search("Error.Code", e.response)
        if error_code == "ConflictException":
            logger.warning(
                "Account assignment already deleted (likely by early revoke), skipping",
                extra={"revoke_event": revoke_event},
            )
            schedule.delete_schedule(scheduler_client, revoke_event.schedule_name)
            return None
        raise

    # Use cached names from event if available, otherwise fall back to API calls
    # (for backwards compatibility with old events that don't have cached names)
    if revoke_event.permission_set_name:
        permission_set = entities.aws.PermissionSet(
            arn=user_account_assignment.permission_set_arn,
            name=revoke_event.permission_set_name,
            description=None,
        )
    else:
        permission_set = sso.describe_permission_set(
            sso_client,
            sso_instance_arn=user_account_assignment.instance_arn,
            permission_set_arn=user_account_assignment.permission_set_arn,
        )

    s3.log_operation(
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

    if cfg.post_update_to_slack:
        # Use cached account name from event if available
        if revoke_event.account_name:
            account = entities.aws.Account(id=user_account_assignment.account_id, name=revoke_event.account_name)
        else:
            try:
                account = organizations.describe_account(org_client, user_account_assignment.account_id)
            except Exception:
                logger.warning(
                    "Failed to describe account, using account ID as fallback", extra={"account_id": user_account_assignment.account_id}
                )
                account = entities.aws.Account(id=user_account_assignment.account_id, name=user_account_assignment.account_id)
        slack_notify_user_on_revoke(
            cfg=cfg,
            account_assignment=user_account_assignment,
            permission_set=permission_set,
            account=account,
            sso_client=sso_client,
            identitystore_client=identitystore_client,
            slack_client=slack_client,
            thread_ts=revoke_event.thread_ts,
        )


def handle_scheduled_group_assignment_deletion(  # noqa: PLR0913
    group_revoke_event: GroupRevokeEvent,
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
) -> SlackResponse | None:
    logger.info("Handling scheduled group access revokation", extra={"revoke_event": group_revoke_event})
    group_assignment = group_revoke_event.group_assignment

    # Handle idempotency: membership may have already been deleted by early revoke
    try:
        sso.remove_user_from_group(group_assignment.identity_store_id, group_assignment.membership_id, identitystore_client)
    except Exception as e:
        error_code = None
        if hasattr(e, "response"):
            import jmespath as jp

            error_code = jp.search("Error.Code", e.response)
        if error_code == "ResourceNotFoundException":
            logger.warning(
                "Group membership already deleted (likely by early revoke), skipping",
                extra={"group_revoke_event": group_revoke_event},
            )
            schedule.delete_schedule(scheduler_client, group_revoke_event.schedule_name)
            return None
        raise

    s3.log_operation(
        audit_entry=s3.AuditEntry(
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
    if cfg.post_update_to_slack:
        slack_notify_user_on_group_access_revoke(
            cfg=cfg,
            group_assignment=group_assignment,
            sso_client=sso_client,
            identitystore_client=identitystore_client,
            slack_client=slack_client,
            thread_ts=group_revoke_event.thread_ts,
        )


def handle_check_on_inconsistency(  # noqa: PLR0913
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
    events_client: EventBridgeClient,
) -> int:
    """Check for inconsistent account assignments and return the count of stale grants found."""
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

    stale_count = 0
    for account_assignment in account_assignments:
        if account_assignment not in account_assignments_from_events:
            stale_count += 1
            try:
                account = organizations.describe_account(org_client, account_assignment.account_id)
            except Exception:
                logger.warning(
                    "Failed to describe account, using account ID as fallback", extra={"account_id": account_assignment.account_id}
                )
                account = entities.aws.Account(id=account_assignment.account_id, name=account_assignment.account_id)
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
    return stale_count


def check_on_groups_inconsistency(  # noqa: PLR0913
    identity_store_client: IdentityStoreClient,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    events_client: EventBridgeClient,
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
) -> int:
    """Check for inconsistent group assignments and return the count of stale grants found."""
    identity_store_id = sso.get_identity_store_id(cfg, sso_client)
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    group_assignments = sso.get_group_assignments(identity_store_id, identity_store_client, cfg)
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
    stale_count = 0
    for group_assignment in group_assignments:
        if group_assignment not in group_assignments_from_events:
            stale_count += 1
            logger.warning("Group assignment is not in the scheduled events", extra={"assignment": group_assignment})
            mention = slack_helpers.create_slack_mention_by_principal_id(
                sso_user_id=group_assignment.user_principal_id,
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identity_store_client,
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
    return stale_count


def handle_sso_elevator_group_scheduled_revocation(  # noqa: PLR0913
    identity_store_client: IdentityStoreClient,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
) -> None:
    identity_store_id = sso.get_identity_store_id(cfg, sso_client)
    scheduled_revoke_events = schedule.get_scheduled_events(scheduler_client)
    group_assignments = sso.get_group_assignments(identity_store_id, identity_store_client, cfg)
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
            s3.log_operation(
                audit_entry=s3.AuditEntry(
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
                slack_notify_user_on_group_access_revoke(
                    cfg=cfg,
                    group_assignment=group_assignment,
                    sso_client=sso_client,
                    identitystore_client=identitystore_client,
                    slack_client=slack_client,
                )


def handle_sso_elevator_scheduled_revocation(  # noqa: PLR0913
    sso_client: SSOAdminClient,
    cfg: config.Config,
    scheduler_client: EventBridgeSchedulerClient,
    org_client: OrganizationsClient,
    slack_client: slack_sdk.WebClient,
    identitystore_client: IdentityStoreClient,
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
                    instance_arn=cfg.sso_instance_arn,
                ),
                sso_client=sso_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
                cfg=cfg,
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

    for block in message["blocks"]:
        if slack_helpers.get_block_id(block) == "buttons":
            blocks = slack_helpers.remove_blocks(message["blocks"], block_ids=["buttons"])
            text = f"Request expired after {cfg.request_expiration_hours} hour(s)."
            blocks.append(
                slack_helpers.SectionBlock(
                    block_id="footer",
                    text=slack_helpers.MarkdownTextObject(
                        text=text,
                    ),
                )
            )
            blocks = slack_helpers.HeaderSectionBlock.set_status(
                blocks=blocks,
                status_text=cfg.timed_out_status,
            )

            slack_client.chat_update(
                channel=event.channel_id,
                ts=message["ts"],
                blocks=blocks,
                text=text,
            )
            logger.info("Buttons were removed", extra={"event": event})
            return

    logger.info("Buttons were not found", extra={"event": event})


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

    for block in message["blocks"]:
        if slack_helpers.get_block_id(block) == "buttons":
            time_to_wait = timedelta(seconds=event.time_to_wait_in_seconds)
            if cfg.approver_renotification_backoff_multiplier != 0:
                time_to_wait = time_to_wait * cfg.approver_renotification_backoff_multiplier
            slack_response = slack_client.chat_postMessage(
                channel=event.channel_id,
                thread_ts=message["ts"],
                text="The request is still awaiting approval. The next reminder will be "
                f"sent in {time_to_wait.seconds // 60} minutes, "
                "unless the request is approved or denied beforehand.",
            )
            logger.info("Notifications to approvers were sent.")
            logger.debug("Slack response:", extra={"slack_response": slack_response})

            schedule.schedule_approver_notification_event(
                schedule_client=scheduler_client, channel_id=event.channel_id, message_ts=message["ts"], time_to_wait=time_to_wait
            )
            return

    logger.info("The request has already been approved or denied.", extra={"event": event})
    return
