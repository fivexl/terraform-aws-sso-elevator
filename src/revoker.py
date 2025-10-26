from datetime import datetime, timedelta

import boto3
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
slack_client = slack_sdk.WebClient(token=cfg.slack_bot_token)


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
            check_on_groups_inconsistency(
                identity_store_client=identitystore_client,
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
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=f"Revoked role {permission_set.name} for user {mention} in account {account.name}",
    )


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
        text=f"User {mention} has been removed from the group {group_assignment.group_name}.",
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
    assignment_status = sso.delete_account_assignment_and_wait_for_result(
        sso_client,
        user_account_assignment,
    )
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
        account = organizations.describe_account(org_client, user_account_assignment.account_id)
        slack_notify_user_on_revoke(
            cfg=cfg,
            account_assignment=user_account_assignment,
            permission_set=permission_set,
            account=account,
            sso_client=sso_client,
            identitystore_client=identitystore_client,
            slack_client=slack_client,
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
    sso.remove_user_from_group(group_assignment.identity_store_id, group_assignment.membership_id, identitystore_client)
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
    identity_store_client: IdentityStoreClient,
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
        if group_assignment not in group_assignments_from_events:
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
                    f"""Inconsistent group assignment detected in {
                        group_assignment.group_name}-{group_assignment.group_id} for user {mention}."""
                    f"The unidentified assignment will be automatically revoked.{time_notice}"
                ),
            )


def handle_sso_elevator_group_scheduled_revocation(  # noqa: PLR0913
    identity_store_client: IdentityStoreClient,
    sso_client: SSOAdminClient,
    scheduler_client: EventBridgeSchedulerClient,
    cfg: config.Config,
    slack_client: slack_sdk.WebClient,
) -> None:
    sso_instance_arn = cfg.sso_instance_arn
    sso_instance = sso.describe_sso_instance(sso_client, sso_instance_arn)
    identity_store_id = sso_instance.identity_store_id
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
            blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
                blocks=blocks,
                color_coding_emoji=cfg.discarded_result_emoji,
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
                f"sent in {time_to_wait.seconds//60} minutes, "
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
