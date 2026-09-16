import json
import re
from datetime import timedelta
from typing import Callable

import boto3
import botocore.exceptions
import slack_sdk.errors
from pydantic import ValidationError
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
import s3
import schedule
import slack_helpers
import sso
from errors import AmbiguousSSOUser, ShownOnRequest, SSOUserNotFound, handle_errors

logger = config.get_logger(service="main")

session = boto3.Session()
schedule_client = session.client("scheduler")
org_client = session.client("organizations")
sso_client = session.client("sso-admin")
identity_store_client = session.client("identitystore")
s3_client = session.client("s3")
ssm_client = session.client("ssm")

cfg = config.get_config()
app = App(
    process_before_response=True,
    token=config.get_slack_secret(ssm_client, config.SLACK_BOT_TOKEN_PARAMETER_ENV, degrade_on_failure=False),
    signing_secret=config.get_slack_secret(ssm_client, config.SLACK_SIGNING_SECRET_PARAMETER_ENV, degrade_on_failure=False),
    # Logger removed to avoid pickle errors with lazy listeners in Lambda
    # Slack Bolt will use its own default logger instead
)


# Must match api_resource_path_cli in locals.tf (the CLI REST API's resource path).
CLI_ACCESS_REQUEST_PATH = "/access-requester-cli"


def _is_cli_event(event: dict) -> bool:
    """Whether event is a REST API proxy event for the CLI route. Slack events come from an
    HTTP API (payload format 2.0, routeKey instead of httpMethod/resource), so they never match.
    """
    return event.get("httpMethod") == "POST" and event.get("resource") == CLI_ACCESS_REQUEST_PATH


def _transient_aws_error_response() -> dict:
    # Shared by every AWS call handle_cli_access_request makes that can fail
    # for a reason saying nothing about whether the request itself is valid
    # (throttling, a 5xx, a connectivity blip) -- a 503 tells the caller
    # this is worth retrying, instead of either GENERIC_REJECTION's "your
    # credentials are invalid" or the blanket handler's 500-plus-Slack-post
    # for what's just AWS being temporarily unavailable.
    return {
        "statusCode": 503,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": "Could not verify your request right now due to a transient AWS error. Please try again."}),
    }


def lambda_handler(event: str, context):  # noqa: ANN001, ANN201
    if _is_cli_event(event):
        return handle_cli_access_request(event)
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def handle_cli_access_request(event: dict) -> dict:  # noqa: PLR0911, PLR0912, PLR0915
    """Handle a CLI access request on CLI_ACCESS_REQUEST_PATH. API Gateway has verified the
    signature; cli_auth decides whether the identity may act. Then it joins the Slack path at
    process_access_request."""
    logger.info("Handling CLI access request")
    try:
        # `or {}` because a key may hold an explicit JSON null. user_arn is read early so every
        # rejection can log it; it is authorizer-verified, and the stage has no access logging.
        request_context = event.get("requestContext") or {}
        user_arn = (request_context.get("identity") or {}).get("userArn", "")

        # When the CLI route is disabled the expected API id is "", so a forged direct invoke
        # carrying "apiId": "" would pass the comparison. Rejecting outright keeps a deployment
        # that never enabled the CLI from accepting CLI-shaped events.
        if not cfg.cli_expected_api_id:
            logger.info("Rejected CLI request: the CLI route is not enabled", extra={"user_arn": user_arn})
            return cli_auth.GENERIC_REJECTION

        # Defense-in-depth only; see config.cli_expected_api_id.
        if request_context.get("apiId") != cfg.cli_expected_api_id:
            logger.info(
                "Rejected CLI request: requestContext.apiId did not match this deployment's API Gateway",
                extra={"user_arn": user_arn},
            )
            return cli_auth.GENERIC_REJECTION

        logger.info("CLI caller userArn", extra={"user_arn": user_arn})

        # Validate the body before extract_identity: its Identity Store scan is the most expensive
        # call here, and any signer could otherwise drive it with garbage payloads.
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "Request body must be valid JSON."}),
            }
        # A syntactically valid JSON document isn't necessarily an object --
        # e.g. "[]" or "42" both pass json.loads above, and body.get below
        # would then raise AttributeError, unwinding to the blanket
        # exception handler as a 500 plus a Slack post for what's just bad
        # caller input.
        if not isinstance(body, dict):
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "Request body must be a JSON object."}),
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
        # Cheap first cut; the full size check needs the account name, so it runs once that is known.
        if len(reason) > slack_helpers.REASON_MAX_LENGTH:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": f"{slack_helpers.REASON_TOO_LONG}."}),
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
        #
        # [0-9], not \d: Python's re module matches \d against every Unicode
        # decimal digit, not just ASCII, and int() itself accepts them too
        # (int("１０") == 10, int("١٠") == 10) -- so a duration value could
        # already be silently "reinterpreted" from a non-ASCII digit string,
        # exactly what the comment above says this strict match exists to
        # avoid on an authorization-relevant field.
        #
        # Checked before the account/permission-set catalog lookups below,
        # not after: those two lookups are themselves expensive -- organizations:ListAccounts
        # or sso:ListPermissionSets plus one sso:DescribePermissionSet per
        # entry, all behind a per-route throttle any SSO principal in the
        # org can drive at 1 rps sustained -- and a malformed duration is
        # the cheapest possible thing to reject first, before paying for
        # AWS calls whose result this request is about to be rejected
        # regardless of.
        duration_value = body.get("duration", "")
        minutes = int(duration_value) if isinstance(duration_value, str) and re.fullmatch(r"[0-9]{1,7}", duration_value) else 0
        max_allowed_minutes = _max_allowed_minutes(cfg)
        if minutes <= 0 or minutes > max_allowed_minutes:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps(
                    {
                        "message": (
                            f"duration must be a positive integer number of minutes, no greater than "
                            f"{max_allowed_minutes} (this deployment's configured maximum)."
                        )
                    }
                ),
            }

        try:
            identity = cli_auth.extract_identity(user_arn, identity_store_client, group.identity_store_id, s3_client) if user_arn else None
        except cli_auth.TransientIdentityStoreError as e:
            # The Identity Store couldn't answer right now; that says nothing about the caller,
            # so ask them to retry. The exception is raised bare, so the detail is on __cause__.
            logger.warning(f"Transient Identity Store error while verifying CLI identity; asking the caller to retry: {e.__cause__}")
            return _transient_aws_error_response()
        if not identity:
            logger.info("Rejected CLI request: could not verify a signed identity with an email", extra={"user_arn": user_arn})
            return cli_auth.GENERIC_REJECTION
        identity_email, identity_user_id, list_of_users = identity

        # The Slack modal can't submit a malformed account or an unlisted
        # permission set at all -- both fields are populated selects built
        # from the *resolved* account/permission-set lists below, not free
        # text. The CLI's JSON body has no such constraint, so a malformed
        # account ID or a made-up permission set name would otherwise reach
        # organizations.describe_account() downstream, well after the
        # decision has already been made, and unwind to the generic
        # exception handler -- a 500 plus a Slack post into the approvals
        # channel for what's just bad caller input.
        #
        # cfg.accounts/cfg.permission_sets can themselves literally be {"*"}
        # (a statement configured for "any account"/"any permission set"),
        # so membership can't be a literal-string check against that
        # config value the way it can for a concrete list -- it has to be
        # checked against what "*" actually expands to. Using the same
        # cached, config-aware resolution the Slack modal's own dropdowns
        # are built from (organizations.get_accounts_from_config_with_cache /
        # sso.get_permission_sets_from_config_with_cache) keeps this exactly
        # as strict as what the modal can offer: any ID that isn't a real,
        # existing account/permission set is rejected here, before this
        # reaches organizations.describe_account() or access_control at all.
        # No separate \d{12} format check needed here: a real AWS account ID
        # is always exactly 12 digits, so real_account_ids (built from an
        # actual Organizations account list) can never contain anything a
        # format check would catch that membership doesn't already reject.
        #
        # Both catalog calls are cache-backed (with_cache_resilience), but
        # that only shields a *warm* cache -- with caching disabled or a
        # cold cache, a throttle/5xx propagates the raw botocore error, same
        # as any other AWS call on this path.
        try:
            real_account_ids = {ac.id for ac in organizations.get_accounts_from_config_with_cache(org_client, s3_client, cfg)}
            real_permission_sets = {ps.name: ps for ps in sso.get_permission_sets_from_config_with_cache(sso_client, s3_client, cfg)}
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            if not sso.is_transient_aws_error(e):
                raise
            # The exception object itself is logged here, not just the fact
            # that a transient error happened (#194 AGENTS.md convention
            # pass) -- e was already bound for is_transient_aws_error above,
            # but wasn't actually included in the message.
            logger.warning(f"Transient AWS error while fetching the account/permission-set catalog; asking the caller to retry: {e}")
            return _transient_aws_error_response()
        # One shared message for both checks, not a distinct one naming
        # which field was wrong (#194 B8): any authenticated SSO caller can
        # already reach this point (this route's AWS_IAM authorizer proves
        # signing capability, not that the signer is one this deployment's
        # policy actually intends to allow), so telling "account" and
        # "permission_set" apart here would let one walk the account ID and
        # permission-set name spaces separately, confirming each real value
        # one field at a time instead of needing a whole matching pair
        # before learning anything.
        if account_id not in real_account_ids or permission_set_name not in real_permission_sets:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "account and permission_set must both be ones this deployment is configured for."}),
            }
        # No check that the caller already holds this assignment: elevation grants access the
        # caller lacks, so such a check would reject every genuine request.
        try:
            requester = slack_helpers.get_user_by_email(app.client, identity_email)
        except slack_sdk.errors.SlackApiError as e:
            # Only "users_not_found" -- a verified SSO identity with no
            # matching Slack account -- is treated as the expected, benign
            # outcome it is: not a bug, shouldn't page anyone via the
            # approvals channel. Reusing GENERIC_REJECTION's exact status
            # and body also avoids giving a caller a cheap way to tell
            # "identity accepted, no Slack match" apart from "identity
            # rejected outright", which would otherwise let someone probe
            # for which emails have a Slack account in this workspace.
            #
            # Every *other* Slack error code (#194 B5) -- invalid_auth or
            # missing_scope from a rotated/revoked bot token or a dropped
            # OAuth scope, ratelimited exhausting get_user_by_email's own
            # retry budget, internal_error/a Slack outage -- says nothing
            # about whether the caller's AWS credentials are valid, so it
            # must not be folded into the same GENERIC_REJECTION response:
            # that would falsely tell every CLI caller their credentials are
            # bad while this integration is actually broken, with nothing
            # above logger.info to reveal that this stopped working for
            # everyone. Re-raising lets these fall through to this
            # function's own blanket exception handler below, which already
            # does the right thing for a real failure: logs it loudly
            # (logger.exception, not info), posts to the approvals channel,
            # and returns a 500 the caller can tell apart from a genuine
            # rejection.
            if e.response["error"] != "users_not_found":
                raise
            logger.info(f"No Slack user found for verified CLI identity {identity_email!r}")
            return cli_auth.GENERIC_REJECTION

        # Email round-trip / UserId threading: identity_user_id above is the
        # UserId this specific, IAM-authenticated session was actually
        # verified against (matched by session_name, not by email at all).
        # But everything downstream of this point -- execute_decision's own
        # account-assignment call -- re-derives a UserId independently, by
        # looking requester.email (Slack's own profile email, not something
        # this function threads through) back up in the Identity Store.
        # That's a second, independent lookup this PR's CLI path adds on top
        # of shared code, and if it ever resolved to someone other than the
        # user actually verified, the grant would go to the wrong person.
        # Checked here with the strict, primary-email-only matcher (not
        # get_user_principal_id_by_email's secondary-domain fallback --
        # deliberately: that fallback is exactly the mechanism the
        # collision fix above closes off, so it must not be trusted here to
        # confirm this cross-check).
        #
        # Reuses list_of_users from extract_identity: a second Identity Store scan would double the
        # cost and lose extract_identity's transient-error handling.
        try:
            requester_user_id = sso.find_user_principal_id_by_email_strict(requester.email, list_of_users)
        except SSOUserNotFound, AmbiguousSSOUser:
            # Neither "nobody has this email" nor "more than one user has
            # this email" can confirm the requester's identity -- both are
            # treated the same as an outright mismatch below, not as an
            # unexpected error.
            requester_user_id = None
        if requester_user_id != identity_user_id:
            logger.warning(
                "Rejected CLI request: requester's Slack email does not resolve back to the verified identity",
                extra={"identity_user_id": identity_user_id, "requester_user_id": requester_user_id},
            )
            return cli_auth.GENERIC_REJECTION

        request = _with_account_name(
            slack_helpers.RequestForAccess(
                permission_set_name=permission_set_name,
                account_id=account_id,
                reason=reason,
                requester_slack_id=requester.id,
                permission_duration=timedelta(minutes=minutes),
                request_source="cli",
                verified_arn=user_arn,
                verified_user_id=identity_user_id,
                verified_email=identity_email,
            )
        )
        if rejection := slack_helpers.request_rejection(request):
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": f"{rejection}."}),
            }

        decision, succeeded = process_access_request(request=request, requester=requester, client=app.client)

        if not succeeded:
            logger.info("CLI request was refused", extra={"decision_reason": decision.reason.value})
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"ok": False, "message": f"Request was not submitted for approval: {decision.reason.value}."}),
            }

        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"ok": True, "message": "Request received and posted for approval in Slack."}),
        }
    except ShownOnRequest as e:
        # The request's own Slack message and thread already show this failure.
        logger.exception(f"CLI access request failed after it was posted: {e}")
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"message": "Access could not be granted. Details are in the request's Slack thread."}),
        }
    except Exception as e:
        logger.exception(f"Error handling CLI access request: {e}")
        # Guarded separately from the logger.exception above (found live by
        # Andrey Devyatkin): this chat_postMessage is a best-effort
        # notification, not part of what this handler promises its caller --
        # a second Slack failure here (e.g. invalid_auth alongside whatever
        # already failed) used to propagate uncaught past this except block
        # entirely, so the caller got API Gateway's opaque "Internal Server
        # Error" instead of the documented {"message": "An unexpected error
        # occurred..."} 500 body below.
        try:
            app.client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text="A CLI access request encountered an unexpected error. Refer to the logs for more details.",
            )
        except Exception:
            logger.exception("Failed to post the CLI access request error notification to Slack")
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
    value), the CLI may request any whole number of minutes up to this max.

    default=0, not a bare max() over the generator: get_max_duration_block
    returns an *empty* list when there's no override and
    max_permissions_duration_time == 0 (its own computed range becomes
    range(1, 1)) -- max() over an empty sequence with no default raises
    ValueError, which used to reach the blanket exception handler as a 500
    plus a Slack post on every single request, CLI and Slack both, for a
    Terraform input this module never validated is positive."""
    return max(
        (
            int(entry_hours) * 60 + int(entry_minutes)
            for option in slack_helpers.get_max_duration_block(cfg)
            for entry_hours, entry_minutes in [option.value.split(":")]
        ),
        default=0,
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
        except AmbiguousSSOUser:
            # Distinct from SSOUserNotFound above: the requester *is* in SSO,
            # just more than once for this email (case-insensitively), so
            # the "could not find your user" message above would tell them
            # the opposite of what happened.
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"<@{body.get('user', {}).get('id') or 'UNKNOWN_USER'}>,"
                "Your request for AWS permissions failed because more than one AWS SSO user shares your email "
                "address (case-insensitively), and SSO Elevator can't tell which one you are."
                "Contact whoever manages your AWS SSO users to resolve the email collision.",
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
def handle_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse | None:  # noqa: ARG001, PLR0911
    logger.info("Handling button click")
    try:
        payload = slack_helpers.ButtonClickedPayload.model_validate(body)
    except ValidationError as e:
        logger.warning(f"Button value is not a request, treating the message as pre-upgrade: {e}")
        message, channel_id = body["message"], body["channel"]["id"]
        slack_helpers.post_thread_reply(client, channel_id, message["ts"], slack_helpers.OLD_REQUEST_REPLY)
        # Expiry can be disabled, so the old buttons might otherwise stay forever.
        slack_helpers.strip_buttons(client, channel_id, message)
        return None
    if isinstance(payload.request, slack_helpers.RequestForGroupAccess):
        return group.handle_group_button_click(payload=payload, client=client, context=context)
    request = payload.request

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
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)
    dm_requester = slack_helpers.should_dm(client, requester.id)
    card = slack_helpers.RequestCard.from_message(payload.message, slack_helpers.request_subject(request), requester.id)

    if (
        cache_for_dublicate_requests.get("requester_slack_id") == request.requester_slack_id
        and cache_for_dublicate_requests.get("account_id") == request.account_id
        and cache_for_dublicate_requests.get("permission_set_name") == request.permission_set_name
    ):
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> request is already in progress, please wait for the result.",
            thread_ts=payload.thread_ts,
        )
    if payload.action == entities.ApproverAction.Discard:
        # Discard ends the request before any decision is made, so it is audited here,
        # once the buttons are gone (like revoker's Expired path).
        if slack_helpers.discard_request(client, payload.channel_id, payload.thread_ts, card, approver.id, requester.id, dm_requester):
            s3.log_operation_best_effort(
                s3.AuditEntry(
                    account_id=request.account_id,
                    role_name=request.permission_set_name,
                    reason=request.reason,
                    requester_slack_id=requester.id,
                    requester_email=requester.email,
                    approver_slack_id=approver.id,
                    approver_email=approver.email,
                    operation_type="declined",
                    permission_duration=request.permission_duration,
                    sso_user_principal_id="NA",
                    audit_entry_type="account",
                    request_source=request.request_source,
                    verified_arn=request.verified_arn,
                    decision_reason="Discarded",
                ),
            )
        cache_for_dublicate_requests.clear()
        return None

    # A CLI request is evaluated against the identity verified at submission
    # (#194 B4), which is also the one execute_decision grants to; the
    # requester's current Slack email may have drifted since.
    eligibility_email = request.verified_email if request.request_source == "cli" and request.verified_email != "NA" else requester.email
    eligibility_verified_user_id = (
        request.verified_user_id if request.request_source == "cli" and request.verified_user_id != "NA" else None
    )  # noqa: E501
    requester_group_ids = access_control.get_requester_group_ids_if_needed(cfg.statements, eligibility_email, eligibility_verified_user_id)
    cache_for_dublicate_requests["requester_slack_id"] = request.requester_slack_id
    cache_for_dublicate_requests["account_id"] = request.account_id
    cache_for_dublicate_requests["permission_set_name"] = request.permission_set_name

    # Every exit below clears the dedup cache, or a retry would be stuck on
    # "already in progress" for this container's life (#194 A3).
    try:
        decision = access_control.make_decision_on_approve_request(
            action=payload.action,
            statements=cfg.statements,
            account_id=request.account_id,
            permission_set_name=request.permission_set_name,
            approver_email=approver.email,
            requester_email=eligibility_email,
            requester_group_ids=requester_group_ids,
        )
    except Exception:
        cache_for_dublicate_requests.clear()
        raise
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not approve this request",
            thread_ts=payload.thread_ts,
        )

    # Buttons go before the grant runs (#194 A2): the dedup cache is
    # per-container, so only removing the buttons stops a second approver's
    # click in another container from granting twice.
    decided_by = slack_helpers.approved_by(approver.id)
    slack_helpers.update_request_message(
        client, payload.channel_id, payload.thread_ts, card, slack_helpers.RequestState.processing(decided_by)
    )

    replaced, grant_error = [], None
    try:
        replaced = access_control.execute_decision(
            decision=decision,
            permission_set_name=request.permission_set_name,
            account_id=request.account_id,
            permission_duration=request.permission_duration,
            approver=approver,
            requester=requester,
            reason=request.reason,
            request_source=request.request_source,
            verified_arn=request.verified_arn,
            verified_user_id=request.verified_user_id,
            channel_id=payload.channel_id,
            message_ts=payload.thread_ts,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(f"execute_decision failed: {e}", extra={"decision": decision.dict()})
    cache_for_dublicate_requests.clear()

    slack_helpers.report_grant_outcome(
        client,
        channel_id=payload.channel_id,
        ts=payload.thread_ts,
        card=card,
        decided_by=decided_by,
        auto=False,
        duration=request.permission_duration,
        replaced=replaced or [],
        error=grant_error,
        dm_requester=dm_requester,
    )
    return None


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


def process_access_request(
    request: slack_helpers.RequestForAccess,
    requester: entities.slack.User,
    client: WebClient,
) -> tuple[access_control.AccessRequestDecision, bool]:
    """Decide on, post, and (for auto-approval) grant an access request, which
    _with_account_name has named and request_rejection has passed.

    Shared by the Slack modal and the CLI. Returns the decision and whether the
    request was granted or queued for approval: RequiresApproval with no
    approver found in Slack keeps its reason yet queues nothing. Raises
    ShownOnRequest when an auto-grant failed after the request was posted.
    """
    # Pinned to the CLI's verified identity, same as at approval time (#194 B4).
    eligibility_email = request.verified_email if request.request_source == "cli" and request.verified_email != "NA" else requester.email
    eligibility_verified_user_id = (
        request.verified_user_id if request.request_source == "cli" and request.verified_user_id != "NA" else None
    )  # noqa: E501
    decision = access_control.make_decision_on_access_request(
        cfg.statements,
        account_id=request.account_id,
        permission_set_name=request.permission_set_name,
        requester_email=eligibility_email,
        requester_group_ids=access_control.get_requester_group_ids_if_needed(
            cfg.statements, eligibility_email, eligibility_verified_user_id
        ),
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    # A CLI request is granted to its verified UserId, never via the fallback.
    secondary_domain_used = request.request_source == "slack" and _secondary_domain_used(requester.email)
    card = slack_helpers.RequestCard.for_request(request, secondary_domain_used)
    outcome = slack_helpers.intake_outcome(client, decision, requester.id)

    text, blocks = slack_helpers.build_request_message(card, outcome.state)
    ts = client.chat_postMessage(channel=cfg.slack_channel_id, blocks=blocks, text=text)["ts"]
    dm_requester = slack_helpers.should_dm(client, requester.id)
    if outcome.thread_reply:
        slack_helpers.post_thread_reply(client, cfg.slack_channel_id, ts, outcome.thread_reply)
    if outcome.dm and dm_requester:
        slack_helpers.send_dm(client, requester.id, outcome.dm)
    if outcome.state.is_pending:
        _schedule_pending_request_events(ts)

    # Called for denials too: one that ends the request is audited as "declined".
    # Granted before the outcome is shown, so a failure is never reported as success.
    replaced, grant_error = [], None
    try:
        replaced = access_control.execute_decision(
            decision=decision,
            permission_set_name=request.permission_set_name,
            account_id=request.account_id,
            permission_duration=request.permission_duration,
            approver=requester,
            requester=requester,
            reason=request.reason,
            request_source=request.request_source,
            verified_arn=request.verified_arn,
            verified_user_id=request.verified_user_id,
            channel_id=cfg.slack_channel_id,
            message_ts=ts,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(f"execute_decision failed: {e}", extra={"decision": decision.dict()})
    if decision.grant:
        slack_helpers.report_grant_outcome(
            client,
            channel_id=cfg.slack_channel_id,
            ts=ts,
            card=card,
            decided_by=slack_helpers.AUTO_APPROVAL_LABELS[decision.reason],
            auto=True,
            duration=request.permission_duration,
            replaced=replaced or [],
            error=grant_error,
            dm_requester=dm_requester,
        )
    return decision, not outcome.state.is_failed


def _with_account_name(request: slack_helpers.RequestForAccess) -> slack_helpers.RequestForAccess:
    account = organizations.describe_account(org_client, request.account_id)
    return request.model_copy(update={"account_name": account.name})


def _secondary_domain_used(email: str) -> bool:
    _, used = sso.get_user_principal_id_by_email(
        identity_store_client=identity_store_client,
        identity_store_id=sso.describe_sso_instance(sso_client, cfg.sso_instance_arn).identity_store_id,
        email=email,
        cfg=cfg,
    )
    return used


def _schedule_pending_request_events(ts: str) -> None:
    schedule.schedule_discard_buttons_event(schedule_client=schedule_client, time_stamp=ts, channel_id=cfg.slack_channel_id)
    schedule.schedule_approver_notification_event(
        schedule_client=schedule_client,
        message_ts=ts,
        channel_id=cfg.slack_channel_id,
        time_to_wait=timedelta(minutes=cfg.approver_renotification_initial_wait_time),
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
    request = _with_account_name(request)
    if rejection := slack_helpers.request_rejection(request):
        slack_helpers.dm_rejection(client, requester.id, rejection)
        return
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
