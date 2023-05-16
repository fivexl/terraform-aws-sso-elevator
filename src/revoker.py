import boto3
import slack_sdk
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import ValidationError
from slack_sdk.web.slack_response import SlackResponse

import config
import entities
import organizations
import s3
import schedule
import slack
import sso

cfg = config.get_config()
logger = config.get_logger(service="revoker")

org_client = boto3.client("organizations")
sso_client = boto3.client("sso-admin")
identitystore_client = boto3.client("identitystore")
scheduler_client = boto3.client("scheduler")
slack_client = slack_sdk.WebClient(token=cfg.slack_bot_token)


def lambda_handler(event: dict, __) -> SlackResponse | None:  # type: ignore # noqa: ANN001, PGH003
    logger.info("Got event", extra={"event": event})

    if event["action"] == "event_bridge_revoke":
        try:
            revoke_event = schedule.RevokeEvent.parse_raw(event["revoke_event"])
            return handle_scheduled_account_assignment_deletion(revoke_event, sso_client, cfg)
        except ValidationError as e:
            logger.exception(e, extra={"event": event})
            raise e

    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)

    accounts = organizations.get_accounts_from_config(org_client, cfg)
    permission_sets = sso.get_permission_sets_from_config(sso_client, cfg)

    account_assignments = sso.list_user_account_assignments(
        sso_client,
        cfg.sso_instance_arn,
        [a.id for a in accounts],
        [ps.arn for ps in permission_sets],
    )
    scheduled_revoke_events = schedule.get_scheduled_revoke_events(scheduler_client)

    if event["action"] == "check_on_inconsistency":
        account_assignments_from_events = [
            sso.AccountAssignment(
                permission_set_arn=revoke_event.user_account_assignment.permission_set_arn,
                account_id=revoke_event.user_account_assignment.account_id,
                principal_id=revoke_event.user_account_assignment.user_principal_id,
                principal_type="USER",
            )
            for revoke_event in scheduled_revoke_events
        ]

        for account_assignment in account_assignments:
            if account_assignment not in account_assignments_from_events:
                account = organizations.describe_account(org_client, account_assignment.account_id)
                logger.warning("Found an inconsistent account assignment", extra={"account_assignment": account_assignment})
                mention = create_slack_mention_by_principal_id(account_assignment)
                slack_client.chat_postMessage(
                    channel=cfg.slack_channel_id,
                    text=(
                        f"Found an inconsistent account assignment in {account.name}-{account.id} for {mention}. "
                        "There is no schedule for its revocation. Please check the revoker logs for more details."
                    ),
                )

    if event["action"] == "sso_elevator_scheduled_revocation":
        account_assignments_from_events = [
            sso.AccountAssignment(
                permission_set_arn=revoke_event.user_account_assignment.permission_set_arn,
                account_id=revoke_event.user_account_assignment.account_id,
                principal_id=revoke_event.user_account_assignment.user_principal_id,
                principal_type="USER",
            )
            for revoke_event in scheduled_revoke_events
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
                    cfg=cfg,
                )


def handle_account_assignment_deletion(account_assignment: sso.UserAccountAssignment, cfg: config.Config) -> SlackResponse | None:
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
        return slack_notify_user_on_revoke(cfg, account_assignment, permission_set, account)


def slack_notify_user_on_revoke(
    cfg: config.Config,
    account_assignment: sso.AccountAssignment | sso.UserAccountAssignment,
    permission_set: entities.aws.PermissionSet,
    account: entities.aws.Account,
) -> SlackResponse:
    mention = create_slack_mention_by_principal_id(account_assignment)
    return slack_client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=f"Revoked role {permission_set.name} for user {mention} in account {account.name}",
    )


def handle_scheduled_account_assignment_deletion(
    revoke_event: schedule.RevokeEvent, sso_client: SSOAdminClient, cfg: config.Config
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
        slack_notify_user_on_revoke(cfg, user_account_assignment, permission_set, account)


def create_slack_mention_by_principal_id(account_assignment: sso.AccountAssignment | sso.UserAccountAssignment) -> str:
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    aws_user_emails = sso.get_user_emails(
        identitystore_client,
        sso_instance.identity_store_id,
        account_assignment.principal_id if isinstance(account_assignment, sso.AccountAssignment) else account_assignment.user_principal_id,
    )
    user_name = None

    for email in aws_user_emails:
        try:
            slack_user = slack.get_user_by_email(slack_client, email)
            user_name = slack_user.real_name
        except Exception:
            continue

    return f"{user_name}" if user_name is not None else aws_user_emails[0]
