from datetime import timedelta
from typing import Callable

import boto3
import jmespath as jp
from slack_bolt import Ack, App, BoltContext
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient
from slack_sdk.web.slack_response import SlackResponse

import access_control
import analytics
import config
import entities
import group
import organizations
import revoker
import schedule
import slack_helpers
import sso
import statement
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


def lambda_handler(event: str, context):  # noqa: ANN001, ANN201
    global cfg  # noqa: PLW0603
    cfg = config.check_and_refresh_config(s3_client)
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


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
                identity_store_id=sso.get_identity_store_id(cfg, sso_client),
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
    identity_store_id = sso.get_identity_store_id(cfg, sso_client)
    groups = sso.get_groups_from_config(identity_store_id, identity_store_client, cfg)

    user_id = body.get("user", {}).get("id")
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
        view = slack_helpers.RequestForGroupAccessView.update_with_groups(groups=groups)
        return client.views_open(trigger_id=trigger_id, view=view)

    logger.debug(f"Updating view with view_id from key: {view_key}")
    view = slack_helpers.RequestForGroupAccessView.update_with_groups(groups=groups)
    return client.views_update(view_id=view_id, view=view)


def load_select_options_for_account_access_request(client: WebClient, body: dict) -> SlackResponse:
    logger.info("Loading select options for view (accounts only)")
    logger.debug("Request body", extra={"body": body})

    user_id = body.get("user", {}).get("id")
    callback_id = slack_helpers.RequestForAccessView.CALLBACK_ID
    view_key = f"{user_id}:{callback_id}"

    # Get user's SSO info and group memberships for filtering
    identity_store_id = sso.get_identity_store_id(cfg, sso_client)
    user_email = slack_helpers.get_user(client, id=user_id).email
    user_principal_id, _ = sso.get_user_principal_id_by_email(
        identity_store_client=identity_store_client,
        identity_store_id=identity_store_id,
        email=user_email,
        cfg=cfg,
    )
    user_group_ids = sso.get_user_group_ids(
        identity_store_client=identity_store_client,
        identity_store_id=identity_store_id,
        user_principal_id=user_principal_id,
    )

    # Cache user info for use in handle_account_selection and handle_request_for_access_submittion
    user_view_map[f"{view_key}:group_ids"] = user_group_ids
    user_view_map[f"{view_key}:user_principal_id"] = user_principal_id
    user_view_map[f"{view_key}:user_email"] = user_email

    # Filter accounts based on user's eligible statements
    eligible_account_ids = statement.get_accounts_for_user(cfg.statements, user_group_ids)

    view_id = user_view_map.get(view_key)

    # If no eligible accounts, show empty view
    if not eligible_account_ids:
        logger.info("User has no eligible accounts", extra={"user_id": user_id})
        view = slack_helpers.RequestForAccessView.build_no_eligible_accounts_view()
        if view_id:
            return client.views_update(view_id=view_id, view=view)
        trigger_id = body["trigger_id"]
        return client.views_open(trigger_id=trigger_id, view=view)

    # Get all accounts and filter to eligible ones
    all_accounts = organizations.get_accounts_from_config_with_cache(org_client=org_client, s3_client=s3_client, cfg=cfg)
    if "*" in eligible_account_ids:
        accounts = all_accounts
    else:
        accounts = [a for a in all_accounts if a.id in eligible_account_ids]

    if not view_id:
        logger.warning(
            f"View ID not found for key: {view_key}. "
            "This happens when Lambda container is recycled between shortcut invocations. "
            "Opening a new view as fallback."
        )
        trigger_id = body["trigger_id"]
        view = slack_helpers.RequestForAccessView.update_with_accounts(accounts=accounts)
        return client.views_open(trigger_id=trigger_id, view=view)

    logger.debug(f"Updating view with view_id from key: {view_key}")
    view = slack_helpers.RequestForAccessView.update_with_accounts(accounts=accounts)
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
def handle_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse:  # noqa: ARG001, PLR0915
    logger.info("Handling button click")
    try:
        payload = slack_helpers.ButtonClickedPayload.model_validate(body)
    except Exception as e:
        logger.exception(e)
        return group.handle_group_button_click(body, client, context)

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
    cache_for_dublicate_requests["requester_slack_id"] = payload.request.requester_slack_id
    cache_for_dublicate_requests["account_id"] = payload.request.account_id
    cache_for_dublicate_requests["permission_set_name"] = payload.request.permission_set_name

    # Look up permission set to get ARN for matching and name for display
    permission_set = sso.get_permission_set(sso_client, cfg.sso_instance_arn, payload.request.permission_set_name)

    if payload.action == entities.ApproverAction.Deny:
        blocks = slack_helpers.HeaderSectionBlock.set_status(
            blocks=payload.message["blocks"],
            status_text=cfg.denied_status,
        )

        blocks = slack_helpers.remove_blocks(blocks, block_ids=["buttons"])
        blocks.append(slack_helpers.button_click_info_block(payload.action, approver.id).to_dict())

        text = f"Request was denied by <@{approver.id}>."
        dm_text = f"Your request was denied by <@{approver.id}>."
        client.chat_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            blocks=blocks,
            text=text,
        )

        analytics.capture(
            event="aws_access_denied",
            distinct_id=requester.email,
            properties={
                "account_id": payload.request.account_id,
                "permission_set": permission_set.name,
                "approver_email": approver.email,
                "requester_email": requester.email,
            },
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

    # Create a resolver function that resolves approver groups per-statement
    # This prevents cross-statement authorization bypass where someone in GroupY
    # could approve requests for Statement A just because Statement A has some groups
    resolver_cache: dict[frozenset[str], set[str]] = {}

    def approver_group_resolver(group_ids: frozenset[str]) -> set[str]:
        if not group_ids:
            return set()
        if group_ids in resolver_cache:
            return resolver_cache[group_ids]
        group_users, _ = slack_helpers.resolve_approver_groups(client, group_ids)
        result = {u.id for u in group_users}
        resolver_cache[group_ids] = result
        return result

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.statements,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        approver_email=approver.email,
        requester_email=requester.email,
        permission_set_arn=permission_set.arn,
        approver_slack_id=approver.id,
        approver_group_resolver=approver_group_resolver,
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        cache_for_dublicate_requests.clear()
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> You cannot approve this request.",
            thread_ts=payload.thread_ts,
        )

    text = f"Permissions granted to <@{requester.id}> by <@{approver.id}>."
    dm_text = f"Your request was approved by <@{approver.id}>. Permissions granted."
    blocks = slack_helpers.HeaderSectionBlock.set_status(
        blocks=payload.message["blocks"],
        status_text=cfg.granted_status,
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

    result = access_control.execute_decision(
        decision=decision,
        permission_set_name=payload.request.permission_set_name,
        account_id=payload.request.account_id,
        permission_duration=payload.request.permission_duration,
        approver=approver,
        requester=requester,
        reason=payload.request.reason,
        thread_ts=payload.thread_ts,
    )

    if result.granted:
        analytics.capture(
            event="aws_access_approved",
            distinct_id=requester.email,
            properties={
                "account_id": payload.request.account_id,
                "permission_set": permission_set.name,
                "approver_email": approver.email,
                "requester_email": requester.email,
                "duration_hours": payload.request.permission_duration.total_seconds() / 3600,
                "self_approved": approver.email == requester.email,
            },
        )

    cache_for_dublicate_requests.clear()
    if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
        logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
        client.chat_postMessage(channel=requester.id, text=dm_text)

    # Post the "End session early" button after permissions are granted
    if result.granted and result.schedule_name:
        first_statement = list(decision.based_on_statements)[0] if decision.based_on_statements else None
        approver_emails = list(first_statement.approvers) if first_statement else []
        approver_groups = list(first_statement.approver_groups) if first_statement else []
        early_revoke_payload = slack_helpers.EarlyRevokeButtonPayload(
            schedule_name=result.schedule_name,
            requester_slack_id=requester.id,
            account_id=result.account_id,
            permission_set_name=result.permission_set_name,
            permission_set_arn=result.permission_set_arn,
            instance_arn=result.instance_arn,
            user_principal_id=result.user_principal_id,
            approver_emails=approver_emails,
            approver_groups=approver_groups,
        )
        client.chat_postMessage(
            channel=payload.channel_id,
            thread_ts=payload.thread_ts,
            blocks=[slack_helpers.build_early_revoke_button(early_revoke_payload).to_dict()],
            text="End session early",
        )

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

app.action(entities.ApproverAction.Deny.value)(
    ack=acknowledge_request,
    lazy=[handle_button_click],
)


@handle_errors
def handle_request_for_access_submittion(  # noqa: PLR0915, PLR0912
    body: dict,
    ack: Ack,  # noqa: ARG001
    client: WebClient,
    context: BoltContext,  # noqa: ARG001
) -> SlackResponse | None:
    logger.info("Handling request for access submission")
    request = slack_helpers.RequestForAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack_helpers.get_user(client, id=request.requester_slack_id)

    # Try to use cached user info from load_select_options_for_account_access_request
    callback_id = slack_helpers.RequestForAccessView.CALLBACK_ID
    view_key = f"{request.requester_slack_id}:{callback_id}"
    cached_user_principal_id = user_view_map.get(f"{view_key}:user_principal_id")
    cached_group_ids = user_view_map.get(f"{view_key}:group_ids")

    identity_store_id = sso.get_identity_store_id(cfg, sso_client)

    if cached_user_principal_id and cached_group_ids is not None:
        logger.debug("Using cached user info", extra={"view_key": view_key})
        user_principal_id = cached_user_principal_id
        user_group_ids = cached_group_ids
    else:
        # Fall back to API calls if cache miss (defense in depth)
        logger.debug("Cache miss, fetching user info from API", extra={"view_key": view_key})
        user_principal_id, _ = sso.get_user_principal_id_by_email(
            identity_store_client=identity_store_client,
            identity_store_id=identity_store_id,
            email=requester.email,
            cfg=cfg,
        )
        user_group_ids = sso.get_user_group_ids(
            identity_store_client=identity_store_client,
            identity_store_id=identity_store_id,
            user_principal_id=user_principal_id,
        )

    # Look up permission set to get ARN for matching against ARN-based config
    permission_set = sso.get_permission_set(sso_client, cfg.sso_instance_arn, request.permission_set_name)

    # Create a resolver function for self-approval via group membership
    resolver_cache: dict[frozenset[str], set[str]] = {}

    def approver_group_resolver(group_ids: frozenset[str]) -> set[str]:
        if not group_ids:
            return set()
        if group_ids in resolver_cache:
            return resolver_cache[group_ids]
        group_users, _ = slack_helpers.resolve_approver_groups(client, group_ids)
        result = {u.id for u in group_users}
        resolver_cache[group_ids] = result
        return result

    decision = access_control.make_decision_on_access_request(
        cfg.statements,
        account_id=request.account_id,
        permission_set_name=request.permission_set_name,
        requester_email=requester.email,
        user_group_ids=user_group_ids,
        permission_set_arn=permission_set.arn,
        requester_slack_id=request.requester_slack_id,
        approver_group_resolver=approver_group_resolver,
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    analytics.capture(
        event="aws_access_requested",
        distinct_id=requester.email,
        properties={
            "account_id": request.account_id,
            "permission_set": permission_set.name,
            "requester_email": requester.email,
            "decision_reason": decision.reason.value,
            "granted": decision.grant,
            "duration_hours": request.permission_duration.total_seconds() / 3600,
        },
    )

    try:
        account = organizations.describe_account(org_client, request.account_id)
    except Exception:
        logger.warning("Failed to describe account, using account ID as fallback", extra={"account_id": request.account_id})
        account = entities.aws.Account(id=request.account_id, name=request.account_id)

    show_buttons = bool(decision.approvers)
    slack_response = client.chat_postMessage(
        blocks=slack_helpers.build_approval_request_message_blocks(
            sso_client=sso_client,
            identity_store_client=identity_store_client,
            slack_client=client,
            requester_slack_id=request.requester_slack_id,
            account=account,
            role_name=permission_set.name,
            reason=request.reason,
            permission_duration=request.permission_duration,
            show_buttons=show_buttons,
            status_text=cfg.pending_status,
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
            status_text = cfg.granted_status
        case access_control.DecisionReason.SelfApproval:
            text = "Self-approval is allowed and requester is an approver. Request will be approved automatically."
            dm_text = "Self-approval is allowed and you are an approver. Your request will be approved automatically."
            status_text = cfg.granted_status
        case access_control.DecisionReason.RequiresApproval:
            approvers, approver_emails_not_found = slack_helpers.find_approvers_in_slack(
                client,
                decision.approvers,  # type: ignore # noqa: PGH003
            )
            group_mentions = slack_helpers.build_approver_group_mentions(decision.approver_groups)

            if not approvers and not decision.approver_groups:
                text = """
                None of the approvers from configuration could be found in Slack.
                Request cannot be processed. Please deny the request and check the module configuration.
                """
                dm_text = """
                Your request cannot be processed because none of the approvers from configuration could be found in Slack.
                Please deny the request and check the module configuration.
                """
                status_text = cfg.denied_status
            else:
                mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
                all_mentions = " ".join(filter(None, [mention_approvers, group_mentions]))
                text = f"{all_mentions} Request awaiting approval."
                if approver_emails_not_found:
                    missing_emails = ", ".join(approver_emails_not_found)
                    text += f"""
                    Note: Some approvers ({missing_emails}) could not be found in Slack.
                    Please deny the request and check the module configuration.
                    """
                dm_text = f"Your request is awaiting approval from {all_mentions}."
                status_text = cfg.pending_status
        case access_control.DecisionReason.NoApprovers:
            text = "Nobody can approve this request."
            dm_text = "Nobody can approve this request."
            status_text = cfg.denied_status
        case access_control.DecisionReason.NoStatements:
            text = "There are no statements for this Permission Set & Account."
            dm_text = "There are no statements for this Permission Set & Account."
            status_text = cfg.denied_status

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

    blocks = slack_helpers.HeaderSectionBlock.set_status(
        blocks=slack_response["message"]["blocks"],
        status_text=status_text,
    )
    client.chat_update(
        channel=cfg.slack_channel_id,
        ts=slack_response["ts"],
        blocks=blocks,
        text=text,
    )

    result = access_control.execute_decision(
        decision=decision,
        permission_set_name=request.permission_set_name,
        account_id=request.account_id,
        permission_duration=request.permission_duration,
        approver=requester,
        requester=requester,
        reason=request.reason,
        thread_ts=slack_response["ts"],
    )

    if result.granted:
        analytics.capture(
            event="aws_access_approved",
            distinct_id=requester.email,
            properties={
                "account_id": request.account_id,
                "permission_set": permission_set.name,
                "approver_email": requester.email,
                "requester_email": requester.email,
                "duration_hours": request.permission_duration.total_seconds() / 3600,
                "self_approved": True,
            },
        )

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

        # Post the "End session early" button
        if result.schedule_name:
            first_statement = list(decision.based_on_statements)[0] if decision.based_on_statements else None
            approver_emails = list(first_statement.approvers) if first_statement else []
            approver_groups = list(first_statement.approver_groups) if first_statement else []
            early_revoke_payload = slack_helpers.EarlyRevokeButtonPayload(
                schedule_name=result.schedule_name,
                requester_slack_id=requester.id,
                account_id=result.account_id,
                permission_set_name=result.permission_set_name,
                permission_set_arn=result.permission_set_arn,
                instance_arn=result.instance_arn,
                user_principal_id=result.user_principal_id,
                approver_emails=approver_emails,
                approver_groups=approver_groups,
            )
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                thread_ts=slack_response["ts"],
                blocks=[slack_helpers.build_early_revoke_button(early_revoke_payload).to_dict()],
                text="End session early",
            )


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


@app.action(slack_helpers.RequestForAccessView.ACCOUNT_ACTION_ID)
def handle_account_selection(ack: Ack, body: dict, client: WebClient) -> SlackResponse:
    ack()
    logger.info("Handling account selection")

    account_id = jp.search(
        f"view.state.values.{slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID}"
        f".{slack_helpers.RequestForAccessView.ACCOUNT_ACTION_ID}.selected_option.value",
        body,
    )
    logger.info(f"Selected account: {account_id}")

    # Get cached user_group_ids
    user_id = body.get("user", {}).get("id")
    callback_id = slack_helpers.RequestForAccessView.CALLBACK_ID
    view_key = f"{user_id}:{callback_id}"
    group_ids_key = f"{view_key}:group_ids"
    user_group_ids = user_view_map.get(group_ids_key)
    if user_group_ids is None:
        logger.warning(
            f"User group IDs not found in cache for key: {group_ids_key}. "
            "This may happen if Lambda container was recycled between form load and account selection. "
            "Defaulting to empty set, which will restrict visibility to statements without required_group_membership."
        )
        user_group_ids = set()

    # Filter permission sets based on user's eligible statements
    valid_ps_names = statement.get_permission_sets_for_account_and_user(cfg.statements, account_id, user_group_ids)
    logger.info(f"Valid permission sets for account and user: {valid_ps_names}")

    if not valid_ps_names:
        view_id = body["view"]["id"]
        updated_view = slack_helpers.RequestForAccessView.build_no_permission_sets_view(view_blocks=body["view"]["blocks"])
        return client.views_update(view_id=view_id, view=updated_view)

    if "*" in valid_ps_names:
        permission_sets = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=cfg)
    else:
        all_ps = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=cfg)
        permission_sets = [ps for ps in all_ps if ps.name in valid_ps_names or ps.arn in valid_ps_names]

    # Handle case where filtered list is empty (configured names don't exist in SSO)
    if not permission_sets:
        view_id = body["view"]["id"]
        updated_view = slack_helpers.RequestForAccessView.build_no_permission_sets_view(view_blocks=body["view"]["blocks"])
        return client.views_update(view_id=view_id, view=updated_view)

    view_id = body["view"]["id"]
    updated_view = slack_helpers.RequestForAccessView.update_with_permission_sets(
        view_blocks=body["view"]["blocks"],
        permission_sets=permission_sets,
    )
    return client.views_update(view_id=view_id, view=updated_view)


# Early Revoke Handlers
# ----------------------


def check_early_revoke_authorization(
    clicker_slack_id: str,
    requester_slack_id: str,
    approver_emails: list[str],
    client: WebClient,
    approver_groups: list[str] | None = None,
) -> bool:
    """Check if the user clicking the button is authorized to end the session.

    Returns True if:
    - cfg.allow_anyone_to_end_session_early is True, OR
    - clicker is the requester, OR
    - clicker is one of the individual approvers, OR
    - clicker is a member of one of the approver groups
    """
    if cfg.allow_anyone_to_end_session_early:
        return True

    # Requester can always end their own session
    if clicker_slack_id == requester_slack_id:
        return True

    # Check if clicker is an individual approver
    try:
        clicker = slack_helpers.get_user(client, id=clicker_slack_id)
        if clicker.email in approver_emails:
            return True
    except Exception as e:
        logger.warning(f"Failed to get user info for authorization check: {e}")

    # Check if clicker is in an approver group
    if approver_groups:
        group_users, _ = slack_helpers.resolve_approver_groups(client, frozenset(approver_groups))
        if clicker_slack_id in {u.id for u in group_users}:
            return True

    return False


@handle_errors
def handle_early_revoke_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse | None:  # noqa: ARG001
    """Handle the 'End session early' button click."""
    import json

    logger.info("Handling early revoke button click")

    clicker_slack_id = jp.search("user.id", body)
    channel_id = jp.search("channel.id", body)
    thread_ts = jp.search("message.thread_ts", body) or jp.search("message.ts", body)

    logger.info(
        "Early revoke button context",
        extra={"thread_ts": thread_ts, "channel_id": channel_id, "clicker_slack_id": clicker_slack_id},
    )
    if not thread_ts:
        logger.warning("Could not extract thread_ts from button click body")

    # Parse the button value
    button_value = jp.search("actions[0].value", body)
    try:
        button_payload = slack_helpers.EarlyRevokeButtonPayload.model_validate(json.loads(button_value))
    except Exception as e:
        logger.error(f"Failed to parse early revoke button payload: {e}")
        return client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Failed to process early revoke request. Please try again.",
        )

    # Check authorization
    if not check_early_revoke_authorization(
        clicker_slack_id=clicker_slack_id,
        requester_slack_id=button_payload.requester_slack_id,
        approver_emails=button_payload.approver_emails,
        client=client,
        approver_groups=button_payload.approver_groups,
    ):
        who_can = "requester or approvers" if button_payload.approver_emails or button_payload.approver_groups else "requester"
        return client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"<@{clicker_slack_id}> You are not authorized to end this session. Only the {who_can} can do this.",
        )

    # Determine if this is account access or group access
    if button_payload.account_id and button_payload.permission_set_name:
        # Account access - get account name for modal
        try:
            account = organizations.describe_account(org_client, button_payload.account_id)
            account_name = account.name
        except Exception:
            account_name = button_payload.account_id

        private_metadata = json.dumps(
            {
                "button_payload": button_payload.model_dump(mode="json"),
                "channel_id": channel_id,
                "thread_ts": thread_ts,
            }
        )

        modal = slack_helpers.EarlyRevokeModal.build(
            account_name=account_name,
            account_id=button_payload.account_id,
            permission_set_name=button_payload.permission_set_name,
            private_metadata=private_metadata,
        )
    elif button_payload.group_id and button_payload.group_name:
        # Group access
        private_metadata = json.dumps(
            {
                "button_payload": button_payload.model_dump(mode="json"),
                "channel_id": channel_id,
                "thread_ts": thread_ts,
            }
        )

        modal = slack_helpers.EarlyRevokeModal.build(
            group_name=button_payload.group_name,
            group_id=button_payload.group_id,
            private_metadata=private_metadata,
        )
    else:
        return client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Invalid early revoke request: missing access details.",
        )

    # Open the modal
    trigger_id = jp.search("trigger_id", body)
    return client.views_open(trigger_id=trigger_id, view=modal)


app.action(entities.ApproverAction.EarlyRevoke.value)(
    ack=acknowledge_request,
    lazy=[handle_early_revoke_button_click],
)


@handle_errors
def handle_early_revoke_modal_submission(body: dict, client: WebClient, context: BoltContext) -> SlackResponse | None:  # noqa: ARG001
    """Handle the early revoke modal submission."""
    logger.info("Handling early revoke modal submission")

    try:
        payload = slack_helpers.EarlyRevokeModalPayload.model_validate(body)
    except Exception as e:
        logger.error(f"Failed to parse early revoke modal payload: {e}")
        return None

    button_payload = payload.button_payload

    # Perform the revocation
    if button_payload.account_id and button_payload.permission_set_arn:
        # Account access revocation
        user_account_assignment = sso.UserAccountAssignment(
            instance_arn=button_payload.instance_arn,
            account_id=button_payload.account_id,
            permission_set_arn=button_payload.permission_set_arn,
            user_principal_id=button_payload.user_principal_id,
        )

        revoker.handle_early_account_revocation(
            user_account_assignment=user_account_assignment,
            schedule_name=button_payload.schedule_name,
            revoker_slack_id=payload.revoker_slack_id,
            requester_slack_id=button_payload.requester_slack_id,
            reason=payload.reason,
            sso_client=sso_client,
            scheduler_client=schedule_client,
            org_client=org_client,
            slack_client=client,
            identitystore_client=identity_store_client,
            cfg=cfg,
            thread_ts=payload.thread_ts,
        )
    elif button_payload.group_id and button_payload.membership_id:
        # Group access revocation
        group_assignment = sso.GroupAssignment(
            group_name=button_payload.group_name,
            group_id=button_payload.group_id,
            user_principal_id=button_payload.user_principal_id,
            membership_id=button_payload.membership_id,
            identity_store_id=button_payload.identity_store_id,
        )

        revoker.handle_early_group_revocation(
            group_assignment=group_assignment,
            schedule_name=button_payload.schedule_name,
            revoker_slack_id=payload.revoker_slack_id,
            requester_slack_id=button_payload.requester_slack_id,
            reason=payload.reason,
            sso_client=sso_client,
            scheduler_client=schedule_client,
            slack_client=client,
            identitystore_client=identity_store_client,
            cfg=cfg,
            thread_ts=payload.thread_ts,
        )


app.view(slack_helpers.EarlyRevokeModal.CALLBACK_ID)(
    ack=acknowledge_request,
    lazy=[handle_early_revoke_modal_submission],
)
