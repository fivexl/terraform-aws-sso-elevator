import boto3
import slack_sdk
from pydantic import ValidationError
from slack_sdk.web.slack_response import SlackResponse


from mypy_boto3_organizations import OrganizationsClient
from mypy_boto3_sso_admin import SSOAdminClient
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_scheduler import EventBridgeSchedulerClient


import config
import entities
import organizations
import s3
import schedule
import slack
import sso
from events import (
    CheckOnInconsistency,
    DiscardButtonsEvent,
    Event,
    ScheduledRevokeEvent,
    SSOElevatorScheduledRevocation,
)

logger = config.get_logger(service="revoker")

cfg = config.get_config()
org_client = boto3.client("organizations")
sso_client = boto3.client("sso-admin")
identitystore_client = boto3.client("identitystore")
scheduler_client = boto3.client("scheduler")
slack_client = slack_sdk.WebClient(token=cfg.slack_bot_token)


def lambda_handler(event: dict, __) -> SlackResponse | None:  # type: ignore # noqa: ANN001, PGH003
    try:
        parsed_event = Event.parse_obj(event).__root__
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

        case DiscardButtonsEvent():
            logger.info("Handling DiscardButtonsEvent", extra={"event": parsed_event})

            return

        case CheckOnInconsistency():
            logger.info("Handling CheckOnInconsistency event", extra={"event": parsed_event})

            return handle_check_on_inconsistency(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
            )

        case SSOElevatorScheduledRevocation():
            logger.info("Handling SSOElevatorScheduledRevocation event", extra={"event": parsed_event})
            return handle_sso_elevator_scheduled_revocation(
                sso_client=sso_client,
                cfg=cfg,
                scheduler_client=scheduler_client,
                org_client=org_client,
                slack_client=slack_client,
                identitystore_client=identitystore_client,
            )


def handle_account_assignment_deletion(
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


def slack_notify_user_on_revoke(
    cfg: config.Config,
    account_assignment: sso.AccountAssignment | sso.UserAccountAssignment,
    permission_set: entities.aws.PermissionSet,
    account: entities.aws.Account,
    sso_client: SSOAdminClient,
    identitystore_client: IdentityStoreClient,
    slack_client: slack_sdk.WebClient,
) -> SlackResponse:
    mention = slack.create_slack_mention_by_principal_id(
        account_assignment=account_assignment,
        sso_client=sso_client,
        cfg=cfg,
        identitystore_client=identitystore_client,
        slack_client=slack_client,
    )
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=f"Revoked role {permission_set.name} for user {mention} in account {account.name}",
    )


def handle_scheduled_account_assignment_deletion(
    revoke_event: schedule.RevokeEvent,
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


def handle_check_on_inconsistency(
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
    ]

    for account_assignment in account_assignments:
        if account_assignment not in account_assignments_from_events:
            account = organizations.describe_account(org_client, account_assignment.account_id)
            logger.warning("Found an inconsistent account assignment", extra={"account_assignment": account_assignment})
            mention = slack.create_slack_mention_by_principal_id(
                account_assignment=account_assignment,
                sso_client=sso_client,
                cfg=cfg,
                identitystore_client=identitystore_client,
                slack_client=slack_client,
            )
            slack_client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=(
                    f"Found an inconsistent account assignment in {account.name}-{account.id} for {mention}. "
                    "There is no schedule for its revocation. Please check the revoker logs for more details."
                ),
            )


def handle_sso_elevator_scheduled_revocation(
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
