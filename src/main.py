import json
import re
from datetime import timedelta
from typing import Callable

import boto3
import botocore.exceptions
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
from errors import AmbiguousSSOUser, SSOUserNotFound, handle_errors

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
    if event.get("routeKey") == CLI_ACCESS_REQUEST_ROUTE_KEY:
        return handle_cli_access_request(event)
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def handle_cli_access_request(event: dict) -> dict:  # noqa: PLR0911, PLR0912, PLR0915
    """Handle a signed CLI request for AWS access, submitted via the AWS_IAM-authenticated
    CLI_ACCESS_REQUEST_PATH route instead of the Slack modal.

    API Gateway has already verified the caller's SigV4 signature by the time this runs;
    cli_auth.extract_identity only decides whether the resulting identity is trustworthy
    enough to act on. Past that point, this funnels into the same process_access_request
    the Slack modal path uses, so approval and everything downstream is unchanged.
    """
    logger.info("Handling CLI access request")
    try:
        # Defense-in-depth, not a real access control: this only blocks a
        # direct lambda:InvokeFunction call that doesn't bother forging
        # requestContext.apiId (an accidental or naive one), not a
        # deliberate one -- a direct invoker controls the entire event JSON,
        # so this value is guessable (visible via DescribeApi/Terraform
        # state to anyone with read access), not secret. The real trust
        # boundary is the IAM policy on who may invoke this Lambda at all;
        # see the README's CLI section. cli_expected_api_id defaults to ""
        # when the CLI route doesn't exist, which a real event's apiId can
        # never equal, so this also fails closed for that case for free.
        if (event.get("requestContext") or {}).get("apiId") != cfg.cli_expected_api_id:
            logger.info("Rejected CLI request: requestContext.apiId did not match this deployment's API Gateway")
            return cli_auth.GENERIC_REJECTION

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
            identity = cli_auth.extract_identity(user_arn, identity_store_client, group.identity_store_id) if user_arn else None
        except cli_auth.TransientIAMError:
            # IAM couldn't answer iam:GetRole right now (throttled, a 5xx,
            # briefly unavailable) -- this says nothing about whether the
            # caller's identity is valid, so it shouldn't be reported as
            # GENERIC_REJECTION's "your credentials are invalid", nor paged
            # to the approvals channel as an unexpected error. A 503 tells
            # the caller this is worth retrying.
            logger.warning("Transient IAM error while verifying CLI identity; asking the caller to retry")
            return _transient_aws_error_response()
        if not identity:
            logger.info("Rejected CLI request: could not verify a signed identity with an email")
            return cli_auth.GENERIC_REJECTION
        identity_email, identity_user_id, list_of_users = identity

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
        # Slack's section-block text fields cap at 2000 chars
        # (build_approval_request_message_blocks embeds reason in one), and
        # slack_sdk doesn't validate this client-side -- an oversized reason
        # reaches chat_postMessage, gets rejected with invalid_blocks, and
        # unwinds to the same 500-plus-Slack-post blanket handler. The Slack
        # modal is implicitly bounded by its own input widget; the CLI isn't.
        # The cap has to subtract the "Reason: " prefix reason gets wrapped
        # in for that field, not just Slack's own 2000: a reason of, say,
        # 1996 characters passed a plain 2000 check but produced a
        # 2004-character field once wrapped, still overflowing.
        reason_prefix = "Reason: "
        max_reason_length = 2000 - len(reason_prefix)
        if len(reason) > max_reason_length:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": f"reason must be at most {max_reason_length} characters."}),
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
        # Checked before the account/permission-set catalog lookups below
        # (and before has_account_assignment further down), not after: those
        # two lookups are themselves expensive -- organizations:ListAccounts
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
            logger.warning("Transient AWS error while fetching the account/permission-set catalog; asking the caller to retry")
            return _transient_aws_error_response()
        if account_id not in real_account_ids:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "account must be a 12-digit AWS account ID this deployment is configured for."}),
            }
        if permission_set_name not in real_permission_sets:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"message": "permission_set must be a permission set this deployment is configured for."}),
            }
        permission_set_arn = real_permission_sets[permission_set_name].arn

        # Defense-in-depth against round-1 finding #6 (session name not
        # bound to any real SSO account assignment): iam:GetRole (inside
        # extract_identity) proves role_name is genuinely IAM Identity
        # Center-provisioned, but says nothing about whether this specific
        # user was ever actually assigned this permission set on this
        # account -- a session under a reserved-path role that's still
        # technically valid but was orphaned (e.g. after this exact
        # assignment was revoked) would otherwise still pass. Placed here,
        # after catalog resolution, rather than right after identity
        # verification: has_account_assignment needs the real
        # permission_set_arn to query list_account_assignments (a
        # per-account-and-permission-set call, not a principal-wide one --
        # see its docstring for why), and that's only available once the
        # caller's permission_set_name has already been resolved against
        # the real catalog above.
        try:
            has_assignment = sso.has_account_assignment(sso_client, cfg.sso_instance_arn, identity_user_id, account_id, permission_set_arn)
        except sso.TransientSSOError:
            logger.warning("Transient AWS error while checking the account assignment; asking the caller to retry")
            return _transient_aws_error_response()
        if not has_assignment:
            logger.warning(
                "Rejected CLI request: verified identity has no SSO account assignment for this account/permission set",
                extra={"user_id": identity_user_id, "account_id": account_id, "permission_set_arn": permission_set_arn},
            )
            return cli_auth.GENERIC_REJECTION

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
        # Reuses list_of_users from the identity tuple above rather than
        # calling sso.list_users(...) again -- that's the exact full
        # paginated Identity Store scan cli_auth.extract_identity's own
        # comment calls "the call on this path most likely to throttle", and
        # a second, unguarded call here would both double that cost per
        # request and reintroduce the transient-error gap (ClientError/
        # BotoCoreError -> 500 + Slack post) extract_identity was
        # specifically hardened against.
        try:
            requester_user_id = sso.find_user_principal_id_by_email_strict(requester.email, list_of_users)
        except (SSOUserNotFound, AmbiguousSSOUser):
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

        request = slack_helpers.RequestForAccess(
            permission_set_name=permission_set_name,
            account_id=account_id,
            reason=reason,
            requester_slack_id=requester.id,
            permission_duration=timedelta(minutes=minutes),
            request_source="cli",
            verified_arn=user_arn,
            verified_user_id=identity_user_id,
        )

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
def handle_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse | None:  # noqa: ARG001, PLR0915
    # Registered as a Bolt lazy listener below -- its return value isn't
    # consumed by the framework, so the None the final best-effort
    # notification block can now produce (if that whole block fails) isn't
    # a behavior change, just an honest type for what was already possible
    # in spirit (nothing here ever depended on getting a real SlackResponse
    # back from this function).
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
    color_coding_emoji = cfg.good_result_emoji

    # execute_decision runs before the chat_update/notifications below, not
    # after: the old order recolored the message green and said
    # "Permissions granted" before the grant had actually been attempted,
    # so a failure here (a stale/bogus permission set name, IAM Identity
    # Center throttling, anything) left that message incorrect with no
    # visible correction -- @handle_errors' own generic error post is a
    # separate message, not a fix to this one. Same shape as the CLI/
    # self-approval path in process_access_request, for the same reason.
    grant_error: Exception | None = None
    try:
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
            verified_user_id=payload.request.verified_user_id,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(
            "execute_decision failed -- overriding the message to reflect the actual outcome", extra={"decision": decision.dict()}
        )
        color_coding_emoji = cfg.bad_result_emoji
        text = f"An error occurred while granting access: {e}"
        dm_text = text

    # The dedup cache is cleared once the outcome is decided (success or a
    # caught failure), not only on the success path -- otherwise a failed
    # execute_decision left this exact request permanently stuck reporting
    # "already in progress" to any retry, since nothing else ever clears it.
    cache_for_dublicate_requests.clear()

    blocks = slack_helpers.HeaderSectionBlock.set_color_coding(
        blocks=payload.message["blocks"],
        color_coding_emoji=color_coding_emoji,
    )
    blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

    # Best-effort from here, not re-raised: once execute_decision has
    # either succeeded (access is live) or definitively failed (captured as
    # grant_error above), a Slack API hiccup while posting/updating these
    # notifications must never be reported as "this failed" on top of a
    # grant that actually succeeded -- the exact inverted-truth outcome the
    # reordering above exists to prevent, just from the notification side
    # instead of the ordering side.
    result: SlackResponse | None = None
    try:
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        result = client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fully post/update notifications about this approval's outcome (best-effort, not re-raised)")

    if grant_error is not None:
        # Re-raised after the messages above already reflect the failure
        # (best-effort -- see above) -- @handle_errors still needs to know
        # this happened, to post its own generic error notification.
        raise grant_error

    return result


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
) -> tuple[access_control.AccessRequestDecision, bool]:
    """Decide on, post for approval (or auto-execute), and notify about an access request.

    Shared by both intake paths: the Slack modal submission (handle_request_for_access_submittion,
    below) and the CLI path (handle_cli_access_request). Both resolve a RequestForAccess and a
    requester by the time they get here; everything past that point — the decision, the Slack
    approval message, discard/renotify scheduling, and auto-execution — is identical regardless of
    where the request came from, so it lives in one place rather than being duplicated.

    Returns the decision alongside a `succeeded` bool: whether the request was actually granted or
    successfully queued for approval, as opposed to e.g. RequiresApproval resolving zero approvers
    in Slack -- which keeps decision.reason == RequiresApproval even though nothing was queued.
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
            verified_user_id=request.verified_user_id,
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

    # execute_decision runs before every notification below -- the thread
    # reply, the DM, and the header's chat_update -- not just before the
    # last of the three. For an auto-grant decision (ApprovalNotRequired/
    # SelfApproval), text/dm_text/color_coding_emoji are already set to their
    # "will be approved automatically" / good_result_emoji wording purely
    # from the *decision* above, before the grant has actually been
    # attempted. Sending any of these three before running the real AWS
    # calls in execute_decision meant a failure there (a stale/bogus
    # permission set name, IAM Identity Center throttling, anything) left
    # the thread reply and/or DM permanently reading "will be approved
    # automatically" with no correction, even once the header's own
    # chat_update was fixed to reflect the real outcome -- a reader
    # following the thread, or the requester's DM, would see no indication
    # the grant actually failed. Overriding text/dm_text/color_coding_emoji
    # here, before any of the three sends below, keeps all of them
    # consistent with each other and with what actually happened.
    #
    # For RequiresApproval, decision.grant is still False here (nothing has
    # been approved yet), so execute_decision's own `if not decision.grant:
    # return False` makes this a no-op -- this reordering only changes
    # behavior for the two auto-grant reasons above.
    grant_error: Exception | None = None
    try:
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
            verified_user_id=request.verified_user_id,
        )
    except Exception as e:  # noqa: BLE001
        grant_error = e
        logger.exception(
            "execute_decision failed -- overriding the message to reflect the actual outcome", extra={"decision": decision.dict()}
        )
        color_coding_emoji = cfg.bad_result_emoji
        text = f"An error occurred while granting access: {e}"
        dm_text = text

    # Everything below is notification about an outcome that's already
    # final by this point (execute_decision either succeeded -- access is
    # live -- or failed outright -- nothing was granted, captured as
    # grant_error above). Wrapped as one best-effort block, not re-raised:
    # once the grant itself succeeded, a Slack API hiccup while posting or
    # updating a message must never surface to the caller as "the request
    # failed" when access was actually granted -- that would be exactly the
    # inverted-truth outcome grant_error's own reordering exists to
    # prevent, just from the opposite direction. Only grant_error (raised
    # below, after this block, unconditionally) can still make the
    # caller-visible outcome a failure.
    try:
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

        if decision.grant and grant_error is None:
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
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fully post/update notifications about this request's outcome (best-effort, not re-raised)")

    if grant_error is not None:
        # Re-raised after the messages above already reflect the failure
        # (best-effort -- see above) -- both callers
        # (handle_request_for_access_submittion's @handle_errors,
        # handle_cli_access_request's own blanket handler) still need to
        # know this happened, e.g. to report a 500 to a CLI caller.
        raise grant_error

    # succeeded, not "decision.reason not in some hand-maintained denylist":
    # color_coding_emoji is the single value every branch above (including
    # the RequiresApproval/no-approvers-found sub-case, which keeps
    # decision.reason == RequiresApproval even though the request could not
    # be processed) already funnels its real outcome into -- deriving from
    # it instead of duplicating that logic in a second set keeps the two
    # from silently drifting apart the way DENIED_DECISION_REASONS did.
    succeeded = color_coding_emoji != cfg.bad_result_emoji
    return decision, succeeded


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
