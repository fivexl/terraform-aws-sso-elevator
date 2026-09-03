import json
import re
from datetime import timedelta
from typing import Callable

import boto3
import slack_sdk.errors
from slack_bolt import Ack, App, BoltContext
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient
from slack_sdk.web.slack_response import SlackResponse

import access_control
import cli_auth
import config
import entities
import group
import organizations
import schedule
import slack_helpers
import sso
from errors import SSOUserNotFound, handle_errors

logger = config.get_logger(service="main")

session = boto3.Session()
schedule_client = session.client("scheduler")
org_client = session.client("organizations")
sso_client = session.client("sso-admin")
identity_store_client = session.client("identitystore")
s3_client = session.client("s3")

cfg = config.get_config()
app = App(
    process_before_response=True,
    # Logger removed to avoid pickle errors with lazy listeners in Lambda
    # Slack Bolt will use its own default logger instead
)


# Route path for the CLI's signed-request intake, on the same API Gateway HTTP
# API as the Slack route but with an AWS_IAM authorizer instead of Slack's own
# signature check — see api_resource_path_cli in locals.tf.
CLI_ACCESS_REQUEST_PATH = "/access-requester-cli"
# routeKey (not rawPath/requestContext.http.path) is what actually identifies which
# route matched: it's always exactly "{METHOD} {path}" as defined in the routes map,
# regardless of the stage name, whereas rawPath's relationship to a named (non-$default)
# stage's prefix isn't something to rely on without checking case by case.
CLI_ACCESS_REQUEST_ROUTE_KEY = f"POST {CLI_ACCESS_REQUEST_PATH}"


def lambda_handler(event: str, context):  # noqa: ANN001, ANN201
    if event.get("routeKey") == CLI_ACCESS_REQUEST_ROUTE_KEY:
        return handle_cli_access_request(event)
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def handle_cli_access_request(event: dict) -> dict:  # noqa: PLR0911
    """Handle a signed CLI request for AWS access, submitted via the AWS_IAM-authenticated
    CLI_ACCESS_REQUEST_PATH route instead of the Slack modal.

    API Gateway has already verified the caller's SigV4 signature by the time this runs;
    cli_auth.extract_identity only decides whether the resulting identity is trustworthy
    enough to act on. Past that point, this funnels into the same process_access_request
    the Slack modal path uses, so approval and everything downstream is unchanged.
    """
    logger.info("Handling CLI access request")
    try:
        # Each hop uses `or {}` rather than a .get(..., {}) default, since a
        # key can be present with an explicit JSON null value -- a default
        # only kicks in when the key is missing entirely, so
        # "authorizer": null would otherwise reach .get("iam") on None and
        # raise, turning a routine unverified-identity case into a 500 with
        # a Slack post instead of the clean 403 it should be.
        iam_context = ((event.get("requestContext") or {}).get("authorizer") or {}).get("iam") or {}
        user_arn = iam_context.get("userArn", "")
        logger.debug("Authorizer IAM userArn", extra={"user_arn": user_arn})

        try:
            identity_email = cli_auth.extract_identity(user_arn, identity_store_client, group.identity_store_id) if user_arn else None
        except cli_auth.TransientIAMError:
            # IAM couldn't answer iam:GetRole right now (throttled, a 5xx,
            # briefly unavailable) -- this says nothing about whether the
            # caller's identity is valid, so it shouldn't be reported as
            # GENERIC_REJECTION's "your credentials are invalid", nor paged
            # to the approvals channel as an unexpected error. A 503 tells
            # the caller this is worth retrying.
            logger.warning("Transient IAM error while verifying CLI identity; asking the caller to retry")
            return {
                "statusCode": 503,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "Could not verify your identity right now due to a transient AWS error. Please try again."}),
            }
        if not identity_email:
            logger.info("Rejected CLI request: could not verify a signed identity with an email")
            return cli_auth.GENERIC_REJECTION

        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "Request body must be valid JSON."}),
            }

        # Coerced to "" rather than left as whatever JSON type the caller
        # sent -- account_id in particular gets passed straight into
        # re.fullmatch below, which raises TypeError (not a clean 400) on
        # anything that isn't already a string, e.g. {"account": 123456789012}.
        account_id = body.get("account", "")
        account_id = account_id if isinstance(account_id, str) else ""
        permission_set_name = body.get("permission_set", "")
        permission_set_name = permission_set_name if isinstance(permission_set_name, str) else ""
        reason = body.get("reason", "")
        reason = reason if isinstance(reason, str) else ""
        if not account_id or not permission_set_name or not reason:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "account, permission_set, and reason are all required and must be non-empty."}),
            }

        # The Slack modal can't submit a malformed account or an unlisted
        # permission set at all -- both fields are populated selects built
        # from cfg.accounts/cfg.permission_sets, not free text. The CLI's
        # JSON body has no such constraint, so a malformed account ID or a
        # made-up permission set name would otherwise reach
        # organizations.describe_account() downstream, well after the
        # decision has already been made, and unwind to the generic
        # exception handler -- a 500 plus a Slack post into the approvals
        # channel for what's just bad caller input.
        # cfg.accounts/cfg.permission_sets can themselves literally contain
        # "*" (a statement configured for any account/permission set), so
        # membership has to allow that wildcard rather than require an
        # exact match against it -- access_control's own statement matching
        # treats "*" the same way.
        if not re.fullmatch(r"\d{12}", account_id) or not ({"*", account_id} & cfg.accounts):
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "account must be a 12-digit AWS account ID this deployment is configured for."}),
            }
        if not ({"*", permission_set_name} & cfg.permission_sets):
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "permission_set must be a permission set this deployment is configured for."}),
            }

        # A strict, length-bounded digit-string match rather than a bare
        # int(...) call -- Python's int() silently truncates a JSON *number*
        # like 2.7 to 2, and accepts underscore-separated digit strings like
        # "2_4" as 24; neither is a value this API should be quietly
        # reinterpreting on an authorization-relevant field. The {1,7} cap
        # (up to 9,999,999 minutes, ~19 years -- far beyond any real
        # duration) exists only to keep an attacker-supplied digit string
        # short enough that int() can't be used to hang the Lambda: CPython
        # rejects converting a >4300-digit string to int at all, and that
        # unbounded ValueError isn't a case this handler catches, so a huge
        # digit string used to reach the generic exception handler as a 500
        # plus a Slack post instead of a clean 400 here.
        duration_value = body.get("duration", "")
        minutes = int(duration_value) if isinstance(duration_value, str) and re.fullmatch(r"\d{1,7}", duration_value) else 0
        if minutes <= 0 or minutes > _max_allowed_minutes(cfg):
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps(
                    {
                        "message": (
                            f"duration must be a positive integer number of minutes, no greater than "
                            f"{_max_allowed_minutes(cfg)} (this deployment's configured maximum)."
                        )
                    }
                ),
            }

        try:
            requester = slack_helpers.get_user_by_email(app.client, identity_email)
        except slack_sdk.errors.SlackApiError:
            # A verified SSO identity with no matching Slack account is an
            # expected outcome, not a bug — it shouldn't page anyone via the
            # approvals channel. Reusing GENERIC_REJECTION's exact status
            # and body also avoids giving a caller a cheap way to tell
            # "identity accepted, no Slack match" apart from "identity
            # rejected outright", which would otherwise let someone probe
            # for which emails have a Slack account in this workspace.
            logger.info(f"No Slack user found for verified CLI identity {identity_email!r}")
            return cli_auth.GENERIC_REJECTION
        request = slack_helpers.RequestForAccess(
            permission_set_name=permission_set_name,
            account_id=account_id,
            reason=reason,
            requester_slack_id=requester.id,
            permission_duration=timedelta(minutes=minutes),
            request_source="cli",
            verified_arn=user_arn,
        )

        process_access_request(request=request, requester=requester, client=app.client)

        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"ok": True, "message": "Request received and posted for approval in Slack."}),
        }
    except Exception as e:
        logger.exception(f"Error handling CLI access request: {e}")
        app.client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text="A CLI access request encountered an unexpected error. Refer to the logs for more details.",
        )
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"message": "An unexpected error occurred while processing the request."}),
        }


def _max_allowed_minutes(cfg: config.Config) -> int:
    """The longest duration (in minutes) this deployment allows anyone to
    request -- derived from whichever policy governs the Slack dropdown's
    own upper bound (slack_helpers.get_max_duration_block), whether that's
    an explicit permission_duration_list_override or the computed
    max_permissions_duration_time increments. Not max_permissions_duration_time
    alone, which vars.tf documents as ignored once the override is set --
    reading it directly let the CLI accept durations a deployment had
    restricted Slack users to a shorter explicit list for.

    Unlike the Slack modal, the CLI isn't limited to the *specific* entries
    that dropdown shows (e.g. only 30/60/90-minute options) -- per an
    explicit decision that Slack's 30-minute increments are a dropdown-size
    constraint, not a real one (the underlying revoke timer accepts any
    value), the CLI may request any whole number of minutes up to this max."""
    return max(
        int(entry_hours) * 60 + int(entry_minutes)
        for option in slack_helpers.get_max_duration_block(cfg)
        for entry_hours, entry_minutes in [option.value.split(":")]
    )


user_view_map = {}
# To update the view, it is necessary to know the view_id. It is returned when the view is opened.
# But shortcut 'request_for_access' handled by two functions. The first one opens the view and the second one updates it.
# So we need to store the view_id somewhere. We use user_id + callback_id as the key since:
# - It's available in both handler functions
# - It persists across Lambda invocations within the same container
# - It's unique per user per request type
# - A user can only have one active modal of each type at a time
#
# NOTE: This in-memory map still has limitations in AWS Lambda:
# - Lambda containers can be recycled between invocations, causing the map to be empty
# - For production use with high traffic, consider using DynamoDB or ElastiCache
# - Current implementation gracefully handles missing view_id by opening a new view


def build_initial_form_handler(
    view_class: slack_helpers.RequestForAccessView | slack_helpers.RequestForGroupAccessView,
) -> Callable[[WebClient, dict, Ack], SlackResponse]:
    def show_initial_form_for_request(
        client: WebClient,
        body: dict,
        ack: Ack,
    ) -> SlackResponse:
        ack()
        if view_class == slack_helpers.RequestForGroupAccessView and not cfg.group_statements:
            return client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text="Group statements are not configured, please check the configuration. Or use another /command.",
            )
        if view_class == slack_helpers.RequestForAccessView and not cfg.statements:
            return client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text="Statements are not configured, please check the configuration. Or use another /command.",
            )

        # Try getting SSO user to check if user exist
        try:
            sso.get_user_principal_id_by_email(
                identity_store_client=identity_store_client,
                identity_store_id=sso.describe_sso_instance(sso_client, cfg.sso_instance_arn).identity_store_id,
                email=slack_helpers.get_user(client, id=body.get("user", {}).get("id")).email,
                cfg=cfg,
            )

        except SSOUserNotFound:
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"<@{body.get('user', {}).get('id') or 'UNKNOWN_USER'}>,"
                "Your request for AWS permissions failed because SSO Elevator could not find your user in SSO."
                "This often happens if your AWS SSO email differs from your Slack email."
                "Please check the SSO Elevator logs for more details.",
            )
            raise

        logger.info(f"Showing initial form for {view_class.__name__}")
        logger.debug("Request body", extra={"body": body})
        trigger_id = body["trigger_id"]
        user_id = body.get("user", {}).get("id")
        callback_id = view_class.CALLBACK_ID

        response = client.views_open(trigger_id=trigger_id, view=view_class.build())

        # Store view_id using user_id + callback_id as key for persistence across Lambda invocations
        view_key = f"{user_id}:{callback_id}"
        user_view_map[view_key] = response.data["view"]["id"]  # type: ignore # noqa: PGH003
        logger.debug(f"Stored view_id for key: {view_key}")

        return response

    return show_initial_form_for_request


def load_select_options_for_group_access_request(client: WebClient, body: dict) -> SlackResponse:
    logger.info("Loading select options for view (groups)")
    logger.debug("Request body", extra={"body": body})
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    groups = sso.get_groups_from_config(sso_instance.identity_store_id, identity_store_client, cfg)

    user_id = body.get("user", {}).get("id")

    # Only show groups the requester is eligible to request (AllowedGroups/AllowedUsers restrictions)
    requester = slack_helpers.get_user(client, id=user_id)
    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.group_statements, requester.email)
    eligible_group_ids = access_control.eligible_group_ids(cfg.group_statements, requester.email, requester_group_ids)
    groups = [group for group in groups if group.id in eligible_group_ids]

    if groups:
        view = slack_helpers.RequestForGroupAccessView.update_with_groups(groups=groups)
    else:
        logger.info("Requester is not eligible for any group statement, showing empty state", extra={"requester_email": requester.email})
        view = slack_helpers.RequestForGroupAccessView.build_no_available_options_view(
            "You are not allowed to request access to any SSO group. Please contact your administrator if you believe this is a mistake."
        )

    callback_id = slack_helpers.RequestForGroupAccessView.CALLBACK_ID
    view_key = f"{user_id}:{callback_id}"

    view_id = user_view_map.get(view_key)
    if not view_id:
        logger.warning(
            f"View ID not found for key: {view_key}. "
            "This happens when Lambda container is recycled between shortcut invocations. "
            "Opening a new view as fallback."
        )
        # Fallback: open a new view with the data already loaded
        trigger_id = body["trigger_id"]
        return client.views_open(trigger_id=trigger_id, view=view)

    logger.debug(f"Updating view with view_id from key: {view_key}")
    return client.views_update(view_id=view_id, view=view)


def load_select_options_for_account_access_request(client: WebClient, body: dict) -> SlackResponse:
    logger.info("Loading select options for view (accounts and permission sets)")
    logger.debug("Request body", extra={"body": body})

    accounts = organizations.get_accounts_from_config_with_cache(org_client=org_client, s3_client=s3_client, cfg=cfg)
    permission_sets = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=cfg)

    user_id = body.get("user", {}).get("id")

    # Only show accounts/permission sets the requester is eligible to request (AllowedGroups/AllowedUsers restrictions)
    requester = slack_helpers.get_user(client, id=user_id)
    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.statements, requester.email)
    accounts, permission_sets = access_control.filter_account_request_options(
        accounts=accounts,
        permission_sets=permission_sets,
        statements=cfg.statements,
        requester_email=requester.email,
        requester_group_ids=requester_group_ids,
    )

    if accounts and permission_sets:
        view = slack_helpers.RequestForAccessView.update_with_accounts_and_permission_sets(
            accounts=accounts, permission_sets=permission_sets
        )
    else:
        logger.info("Requester is not eligible for any statement, showing empty state", extra={"requester_email": requester.email})
        view = slack_helpers.RequestForAccessView.build_no_available_options_view(
            "You are not allowed to request access to any account or permission set. "
            "Please contact your administrator if you believe this is a mistake."
        )

    callback_id = slack_helpers.RequestForAccessView.CALLBACK_ID
    view_key = f"{user_id}:{callback_id}"

    view_id = user_view_map.get(view_key)
    if not view_id:
        logger.warning(
            f"View ID not found for key: {view_key}. "
            "This happens when Lambda container is recycled between shortcut invocations. "
            "Opening a new view as fallback."
        )
        # Fallback: open a new view with the data already loaded
        trigger_id = body["trigger_id"]
        return client.views_open(trigger_id=trigger_id, view=view)

    logger.debug(f"Updating view with view_id from key: {view_key}")
    return client.views_update(view_id=view_id, view=view)


app.shortcut("request_for_access")(
    build_initial_form_handler(view_class=slack_helpers.RequestForAccessView),  # type: ignore # noqa: PGH003
    load_select_options_for_account_access_request,
)

app.shortcut("request_for_group_membership")(
    build_initial_form_handler(view_class=slack_helpers.RequestForGroupAccessView),  # type: ignore # noqa: PGH003
    load_select_options_for_group_access_request,
)

cache_for_dublicate_requests = {}


@handle_errors
def handle_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse:  # noqa: ARG001
    logger.info("Handling button click")
    try:
        payload = slack_helpers.ButtonClickedPayload.model_validate(body)
    except Exception as e:
        logger.exception(e)
        return group.handle_group_button_click(body=body, client=client, context=context)

    logger.info("Button click payload", extra={"payload": payload})
    # Approver might be from different Slack workspace, if so, get_user will fail.
    try:
        approver = slack_helpers.get_user(client, id=payload.approver_slack_id)
    except Exception as e:
        logger.warning(f"Failed to get approver user info: {e}")
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"""Unable to process this approval - approver information could not be retrieved.
            This may happen if the approver <@{payload.approver_slack_id}> is from a different Slack workspace.
            Please check the module configuration.""",
            thread_ts=payload.thread_ts,
        )
    requester = slack_helpers.get_user(client, id=payload.request.requester_slack_id)
    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    if (
        cache_for_dublicate_requests.get("requester_slack_id") == payload.request.requester_slack_id
        and cache_for_dublicate_requests.get("account_id") == payload.request.account_id
        and cache_for_dublicate_requests.get("permission_set_name") == payload.request.permission_set_name
    ):
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> request is already in progress, please wait for the result.",
            thread_ts=payload.thread_ts,
        )
    if payload.action == entities.ApproverAction.Discard:
        blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
            blocks=payload.message["blocks"],
            color_coding_emoji=cfg.bad_result_emoji,
        )

        blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
        blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

        text = f"Request was discarded by<@{approver.id}> "
        dm_text = f"Your request was discarded by <@{approver.id}>."
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )

        cache_for_dublicate_requests.clear()
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )

    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.statements, requester.email)
    cache_for_dublicate_requests["requester_slack_id"] = payload.request.requester_slack_id
    cache_for_dublicate_requests["account_id"] = payload.request.account_id
    cache_for_dublicate_requests["permission_set_name"] = payload.request.permission_set_name

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.statements,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        approver_email=approver.email,
        requester_email=requester.email,
        requester_group_ids=requester_group_ids,
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    dm_text = f"Your request was approved by <@{approver.id}>. Permissions granted."
    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=payload.message["blocks"],
        color_coding_emoji=cfg.good_result_emoji,
    )

    blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())
    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)
    client.chat_update(
        channel=payload.channel_id,
        ts=payload.thread_ts,
        blocks=blocks,
        text=text,
    )

    access_control.execute_decision(
        decision=decision,
        permission_set_name=payload.request.permission_set_name,
        account_id=payload.request.account_id,
        permission_duration=payload.request.permission_duration,
        approver=approver,
        requester=requester,
        reason=payload.request.reason,
        request_source=payload.request.request_source,
        verified_arn=payload.request.verified_arn,
    )
    cache_for_dublicate_requests.clear()
    if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
        logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
        client.chat_postMessage(channel=requester.id, text=dm_text)
    return client.chat_postMessage(
        channel=payload.channel_id,
        text=text,
        thread_ts=payload.thread_ts,
    )


def acknowledge_request(ack: Ack):  # noqa: ANN201
    ack()


app.action(entities.ApproverAction.Approve.value)(
    ack=acknowledge_request,
    lazy=[handle_button_click],
)

app.action(entities.ApproverAction.Discard.value)(
    ack=acknowledge_request,
    lazy=[handle_button_click],
)


def process_access_request(  # noqa: PLR0915, PLR0912
    request: slack_helpers.RequestForAccess,
    requester: entities.slack.User,
    client: WebClient,
) -> None:
    """Decide on, post for approval (or auto-execute), and notify about an access request.

    Shared by both intake paths: the Slack modal submission (handle_request_for_access_submittion,
    below) and the CLI path (handle_cli_access_request). Both resolve a RequestForAccess and a
    requester by the time they get here; everything past that point — the decision, the Slack
    approval message, discard/renotify scheduling, and auto-execution — is identical regardless of
    where the request came from, so it lives in one place rather than being duplicated.
    """
    decision = access_control.make_decision_on_access_request(
        cfg.statements,
        account_id=request.account_id,
        permission_set_name=request.permission_set_name,
        requester_email=requester.email,
        requester_group_ids=access_control.get_requester_group_ids_if_needed(cfg.statements, requester.email),
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    account = organizations.describe_account(org_client, request.account_id)

    show_buttons = bool(decision.approvers)
    slack_response = client.chat_postMessage(
        blocks=slack_helpers.build_approval_request_message_blocks(
            sso_client=sso_client,
            identity_store_client=identity_store_client,
            slack_client=client,
            requester_slack_id=request.requester_slack_id,
            account=account,
            role_name=request.permission_set_name,
            reason=request.reason,
            permission_duration=request.permission_duration,
            show_buttons=show_buttons,
            color_coding_emoji=cfg.waiting_result_emoji,
            request_source=request.request_source,
            verified_arn=request.verified_arn,
        ),
        channel=cfg.slack_channel_id,
        text=f"Request for access to {account.name} account from {requester.real_name}",
    )

    if show_buttons:
        ts = slack_response["ts"]
        if ts is not None:
            schedule.schedule_discard_buttons_event(
                schedule_client=schedule_client,
                time_stamp=ts,
                channel_id=cfg.slack_channel_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=schedule_client,
                message_ts=ts,
                channel_id=cfg.slack_channel_id,
                time_to_wait=timedelta(
                    minutes=cfg.approver_renotification_initial_wait_time,
                ),
            )

    match decision.reason:
        case access_control.DecisionReason.ApprovalNotRequired:
            text = "Approval for this Permission Set & Account is not required. Request will be approved automatically."
            dm_text = "Approval for this Permission Set & Account is not required. Your request will be approved automatically."
            color_coding_emoji = cfg.good_result_emoji
        case access_control.DecisionReason.SelfApproval:
            text = "Self approval is allowed and requester is an approver. Request will be approved automatically."
            dm_text = "Self approval is allowed and you are an approver. Your request will be approved automatically."
            color_coding_emoji = cfg.good_result_emoji
        case access_control.DecisionReason.RequiresApproval:
            approvers, approver_emails_not_found = slack_helpers.find_approvers_in_slack(
                client,
                decision.approvers,  # type: ignore # noqa: PGH003
            )
            if not approvers:
                text = """
                None of the approvers from configuration could be found in Slack.
                Request cannot be processed. Please discard the request and check the module configuration.
                """
                dm_text = """
                Your request cannot be processed because none of the approvers from configuration could be found in Slack.
                Please discard the request and check the module configuration.
                """
                color_coding_emoji = cfg.bad_result_emoji
            else:
                mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
                text = f"{mention_approvers} there is a request waiting for the approval."
                if approver_emails_not_found:
                    missing_emails = ", ".join(approver_emails_not_found)
                    text += f"""
                    Note: Some approvers ({missing_emails}) could not be found in Slack.
                    Please discard the request and check the module configuration.
                    """
                dm_text = f"Your request is waiting for the approval from {mention_approvers}."
                color_coding_emoji = cfg.waiting_result_emoji
        case access_control.DecisionReason.NoApprovers:
            text = "Nobody can approve this request."
            dm_text = "Nobody can approve this request."
            color_coding_emoji = cfg.bad_result_emoji
        case access_control.DecisionReason.NoStatements:
            text = "There are no statements for this Permission Set & Account."
            dm_text = "There are no statements for this Permission Set & Account."
            color_coding_emoji = cfg.bad_result_emoji
        case access_control.DecisionReason.RequesterNotAllowed:
            text = f"<@{requester.id}> is not allowed to request access to this Permission Set & Account."
            dm_text = "You are not allowed to request access to this Permission Set & Account."
            color_coding_emoji = cfg.bad_result_emoji

    is_user_in_channel = slack_helpers.check_if_user_is_in_channel(client, cfg.slack_channel_id, requester.id)

    logger.info(f"Sending message to the channel {cfg.slack_channel_id}, message: {text}")
    client.chat_postMessage(text=text, thread_ts=slack_response["ts"], channel=cfg.slack_channel_id)
    if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
        logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
        client.chat_postMessage(
            channel=requester.id,
            text=f"""
            {dm_text} You are receiving this message in a DM because you are not a member of the channel <#{cfg.slack_channel_id}>.
            """,
        )

    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=slack_response["message"]["blocks"],
        color_coding_emoji=color_coding_emoji,
    )
    client.chat_update(
        channel=cfg.slack_channel_id,
        ts=slack_response["ts"],
        blocks=blocks,
        text=text,
    )

    access_control.execute_decision(
        decision=decision,
        permission_set_name=request.permission_set_name,
        account_id=request.account_id,
        permission_duration=request.permission_duration,
        approver=requester,
        requester=requester,
        reason=request.reason,
        request_source=request.request_source,
        verified_arn=request.verified_arn,
    )

    if decision.grant:
        client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=f"Permissions granted to <@{requester.id}>",
            thread_ts=slack_response["ts"],
        )
        if not is_user_in_channel and cfg.send_dm_if_user_not_in_channel:
            client.chat_postMessage(
                channel=requester.id,
                text="Your request was processed, permissions granted.",
            )


@handle_errors
def handle_request_for_access_submittion(
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> None:
    logger.info("Handling request for access submission")
    request = slack_helpers.RequestForAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)
    process_access_request(request=request, requester=requester, client=client)


app.view(slack_helpers.RequestForAccessView.CALLBACK_ID)(
    ack=acknowledge_request,
    lazy=[handle_request_for_access_submittion],
)

app.view(slack_helpers.RequestForGroupAccessView.CALLBACK_ID)(
    ack=acknowledge_request,
    lazy=[group.handle_request_for_group_access_submittion],
)


@app.action("duration_picker_action")
def handle_duration_picker_action(ack):  # noqa: ANN201, ANN001
    ack()
