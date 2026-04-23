import asyncio
import json
import uuid
from datetime import timedelta
from typing import Callable

import boto3
from slack_bolt import Ack, App, BoltContext
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient
from slack_sdk.web.slack_response import SlackResponse

import access_control
import config
import entities
import group
import organizations
import request_store
import schedule
import slack_helpers
import sso
import teams_cards
import teams_users
from entities.elevator_request import ElevatorRequestKind, ElevatorRequestRecord, ElevatorRequestStatus
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


class SSOElevatorBot:
    """Teams bot that handles access request commands and card interactions."""

    async def on_message_activity(self, turn_context) -> None:  # noqa: ANN001
        """Handle /request-access and /request-group commands."""
        from botbuilder.schema import Activity  # type: ignore[import]

        text = (turn_context.activity.text or "").strip()

        if "/request-access" in text:
            if not cfg.statements:
                await turn_context.send_activity(
                    Activity(type="message", text="Statements are not configured, please check the configuration.")
                )
                return
            await self._open_task_module(turn_context, "account")

        elif "/request-group" in text:
            if not cfg.group_statements:
                await turn_context.send_activity(
                    Activity(type="message", text="Group statements are not configured, please check the configuration.")
                )
                return
            await self._open_task_module(turn_context, "group")

    async def _open_task_module(self, turn_context, kind: str) -> None:  # noqa: ANN001
        """Check SSO user exists then return a task module response."""
        from botbuilder.schema import Activity  # type: ignore[import]

        try:
            user = await teams_users.get_user_from_activity(turn_context)
            sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
            sso.get_user_principal_id_by_email(
                identity_store_client=identity_store_client,
                identity_store_id=sso_instance.identity_store_id,
                email=user.email,
                cfg=cfg,
            )
        except SSOUserNotFound:
            from_prop = turn_context.activity.from_property
            mention_text = f"<at>{from_prop.name}</at>"
            await turn_context.send_activity(
                Activity(
                    type="message",
                    text=f"{mention_text} Your request failed because SSO Elevator could not find your user in AWS SSO.",
                )
            )
            return
        except Exception as e:
            logger.exception(f"Error checking SSO user in on_message_activity: {e}")
            await turn_context.send_activity(Activity(type="message", text="An unexpected error occurred. Check the logs for details."))
            return

        # Store the kind in the activity value so task/fetch knows what to show
        # We respond with a task module invoke response
        from botbuilder.schema import InvokeResponse  # type: ignore[import]

        task_module_response = {
            "task": {
                "type": "continue",
                "value": {
                    "title": "Request AWS Account Access" if kind == "account" else "Request AWS Group Access",
                    "height": "large",
                    "width": "medium",
                    "card": {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": await self._build_form_card(kind),
                    },
                    "completionBotId": cfg.teams_microsoft_app_id,
                    "fallbackUrl": "",
                },
            }
        }
        await turn_context.send_activity(
            Activity(
                type="invokeResponse",
                value=InvokeResponse(status=200, body=task_module_response),
            )
        )

    async def on_invoke_activity(self, turn_context) -> None:  # noqa: ANN001
        """Handle task/fetch, task/submit, and adaptiveCard/action invokes."""
        from botbuilder.schema import Activity, InvokeResponse  # type: ignore[import]

        name = turn_context.activity.name

        try:
            if name == "task/fetch":
                result = await self._handle_task_fetch(turn_context)
                await turn_context.send_activity(Activity(type="invokeResponse", value=InvokeResponse(status=200, body=result)))
            elif name == "task/submit":
                result = await self._handle_task_submit(turn_context)
                await turn_context.send_activity(Activity(type="invokeResponse", value=InvokeResponse(status=200, body=result)))
            elif name == "adaptiveCard/action":
                await self._handle_card_action(turn_context)
                await turn_context.send_activity(Activity(type="invokeResponse", value=InvokeResponse(status=200, body={})))
            else:
                await turn_context.send_activity(Activity(type="invokeResponse", value=InvokeResponse(status=501, body={})))
        except Exception as e:
            logger.exception(f"Error in on_invoke_activity (name={name}): {e}")
            await turn_context.send_activity(Activity(type="invokeResponse", value=InvokeResponse(status=500, body={})))

    async def _build_form_card(self, kind: str) -> dict:
        """Build the account or group access form card."""
        duration_options = (
            [str(timedelta(hours=h)) for h in range(1, cfg.max_permissions_duration_time + 1)]
            if not cfg.permission_duration_list_override
            else cfg.permission_duration_list_override
        )

        if kind == "account":
            accounts = organizations.get_accounts_from_config_with_cache(org_client=org_client, s3_client=s3_client, cfg=cfg)
            permission_sets = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=cfg)
            return teams_cards.build_account_access_form(accounts, permission_sets, duration_options)
        else:
            sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
            groups = sso.get_groups_from_config(sso_instance.identity_store_id, identity_store_client, cfg)
            return teams_cards.build_group_access_form(groups, duration_options)

    async def _handle_task_fetch(self, turn_context) -> dict:  # noqa: ANN001
        """Return TaskModuleResponse with account or group access form card."""
        value = turn_context.activity.value or {}
        kind = value.get("kind", "account")
        card = await self._build_form_card(kind)
        return {
            "task": {
                "type": "continue",
                "value": {
                    "title": "Request AWS Account Access" if kind == "account" else "Request AWS Group Access",
                    "height": "large",
                    "width": "medium",
                    "card": {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    },
                },
            }
        }

    async def _handle_task_submit(self, turn_context) -> dict:  # noqa: ANN001, PLR0912, PLR0915
        """Parse form data, make access decision, store request, post approval card."""

        value = turn_context.activity.value or {}
        data = value.get("data", value)

        try:
            user = await teams_users.get_user_from_activity(turn_context)
        except Exception as e:
            logger.exception(f"Failed to get user in _handle_task_submit: {e}")
            return {"task": {"type": "message", "value": "Failed to identify user. Please try again."}}

        # Determine kind from submitted data
        is_group = "group_id" in data
        kind = "group" if is_group else "account"

        # Parse duration
        _DURATION_PARTS = 3  # noqa: N806
        duration_str = data.get("duration", "1:00:00")
        try:
            parts = duration_str.split(":")
            if len(parts) == _DURATION_PARTS:
                permission_duration = timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
            else:
                permission_duration = timedelta(hours=1)
        except Exception:
            permission_duration = timedelta(hours=1)

        reason = data.get("reason", "")
        elevator_id = str(uuid.uuid4())
        slack_user = user.to_slack_user()

        if kind == "account":
            account_id = data.get("account_id", "")
            permission_set_name = data.get("permission_set", "")

            decision = access_control.make_decision_on_access_request(
                cfg.statements,
                requester_email=user.email,
                account_id=account_id,
                permission_set_name=permission_set_name,
            )

            request_store.put_access_request(
                ElevatorRequestRecord(
                    elevator_request_id=elevator_id,
                    kind=ElevatorRequestKind.account,
                    status=ElevatorRequestStatus.awaiting_approval,
                    requester_slack_id=user.id,
                    reason=reason,
                    permission_duration_seconds=int(permission_duration.total_seconds()),
                    account_id=account_id,
                    permission_set_name=permission_set_name,
                )
            )

            try:
                account = organizations.describe_account(org_client, account_id)
            except Exception:
                account = entities.aws.Account(id=account_id, name=account_id)

            show_buttons = bool(decision.approvers)
            color_style = teams_cards.get_color_style(cfg.waiting_result_emoji)
            request_data = {
                "account_id": account_id,
                "permission_set": permission_set_name,
                "duration": duration_str,
                "reason": reason,
                "requester_id": user.id,
            }
            card = teams_cards.build_approval_card(
                requester_name=user.display_name,
                account=account,
                group=None,
                role_name=permission_set_name,
                reason=reason,
                permission_duration=duration_str,
                show_buttons=show_buttons,
                color_style=color_style,
                request_data=request_data,
                elevator_request_id=elevator_id,
            )
        else:
            group_id = data.get("group_id", "")

            decision = access_control.make_decision_on_access_request(
                cfg.group_statements,
                requester_email=user.email,
                group_id=group_id,
            )

            request_store.put_access_request(
                ElevatorRequestRecord(
                    elevator_request_id=elevator_id,
                    kind=ElevatorRequestKind.group,
                    status=ElevatorRequestStatus.awaiting_approval,
                    requester_slack_id=user.id,
                    reason=reason,
                    permission_duration_seconds=int(permission_duration.total_seconds()),
                    group_id=group_id,
                )
            )

            try:
                sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
                sso_group = sso.describe_group(sso_instance.identity_store_id, group_id, identity_store_client)
            except Exception:
                sso_group = entities.aws.SSOGroup(id=group_id, identity_store_id="", name=group_id)

            show_buttons = bool(decision.approvers)
            color_style = teams_cards.get_color_style(cfg.waiting_result_emoji)
            request_data = {
                "group_id": group_id,
                "duration": duration_str,
                "reason": reason,
                "requester_id": user.id,
            }
            card = teams_cards.build_approval_card(
                requester_name=user.display_name,
                account=None,
                group=sso_group,
                role_name=None,
                reason=reason,
                permission_duration=duration_str,
                show_buttons=show_buttons,
                color_style=color_style,
                request_data=request_data,
                elevator_request_id=elevator_id,
            )

        # Post approval card to channel
        try:
            from revoker import TeamsNotifier  # type: ignore[import]

            notifier = TeamsNotifier(cfg)
            activity_id = await notifier.send_message(text="New access request", card=card)
            if activity_id:
                request_store.update_teams_presentation(elevator_id, cfg.teams_approval_conversation_id, activity_id)

            if show_buttons:
                schedule.schedule_discard_buttons_event(
                    schedule_client=schedule_client,
                    time_stamp="",
                    channel_id="",
                    elevator_request_id=elevator_id,
                )
                schedule.schedule_approver_notification_event(
                    schedule_client=schedule_client,
                    message_ts="",
                    channel_id="",
                    time_to_wait=timedelta(minutes=cfg.approver_renotification_initial_wait_time),
                    elevator_request_id=elevator_id,
                )
        except Exception as e:
            logger.exception(f"Failed to post approval card to Teams channel: {e}")

        # Auto-execute if no approval needed
        if decision.grant:
            try:
                if kind == "account":
                    access_control.execute_decision(
                        decision=decision,
                        permission_set_name=permission_set_name,
                        account_id=account_id,
                        permission_duration=permission_duration,
                        approver=slack_user,
                        requester=slack_user,
                        reason=reason,
                        elevator_request_id=elevator_id,
                    )
                else:
                    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
                    access_control.execute_decision_on_group_request(
                        decision=decision,
                        group=sso_group,
                        permission_duration=permission_duration,
                        approver=slack_user,
                        requester=slack_user,
                        reason=reason,
                        identity_store_id=sso_instance.identity_store_id,
                        elevator_request_id=elevator_id,
                    )
                request_store.update_request_status(elevator_id, ElevatorRequestStatus.completed)
            except Exception as e:
                logger.exception(f"Failed to execute auto-approved decision: {e}")

        return {"task": {"type": "message", "value": "Your request has been submitted."}}

    async def _handle_card_action(self, turn_context) -> None:  # noqa: ANN001, PLR0912, PLR0915
        """Handle Approve/Discard button clicks on approval cards."""
        from botbuilder.schema import Activity  # type: ignore[import]

        value = turn_context.activity.value or {}
        action_data = value.get("action", {})
        if isinstance(action_data, dict):
            elevator_request_id = action_data.get("elevator_request_id") or value.get("elevator_request_id")
            action = action_data.get("action") or value.get("action")
        else:
            elevator_request_id = value.get("elevator_request_id")
            action = value.get("action")

        if not elevator_request_id:
            logger.warning("Card action missing elevator_request_id", extra={"value": value})
            return

        rec = request_store.get_access_request(elevator_request_id)
        if rec is None:
            logger.warning("Access request not found", extra={"elevator_request_id": elevator_request_id})
            return

        # Try to begin in-flight approval
        if not request_store.try_begin_in_flight_approval(
            requester_slack_id=rec.requester_slack_id,
            account_id=rec.account_id,
            permission_set_name=rec.permission_set_name,
            group_id=rec.group_id,
        ):
            await turn_context.send_activity(
                Activity(type="message", text="This request is already being processed, please wait for the result.")
            )
            return

        try:
            approver = await teams_users.get_user_from_activity(turn_context)
        except Exception as e:
            logger.exception(f"Failed to get approver in _handle_card_action: {e}")
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )
            return

        approver_slack = approver.to_slack_user()

        # Determine requester slack user (use stored id as placeholder)
        requester_slack = entities.slack.User(
            id=rec.requester_slack_id,
            email="",
            real_name=rec.requester_slack_id,
        )

        permission_duration = timedelta(seconds=rec.permission_duration_seconds)
        approver_action = entities.ApproverAction.Approve if action == "approve" else entities.ApproverAction.Discard

        if approver_action == entities.ApproverAction.Discard:
            request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.discarded)
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )
            # Update card to show discarded
            await self._update_approval_card(
                turn_context=turn_context,
                elevator_request_id=elevator_request_id,
                decision_action="discarded",
                approver_name=approver.display_name,
                color_style=teams_cards.get_color_style(cfg.bad_result_emoji),
            )
            return

        # Make approval decision
        if rec.kind == ElevatorRequestKind.account:
            decision = access_control.make_decision_on_approve_request(
                action=approver_action,
                statements=cfg.statements,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                approver_email=approver.email,
                requester_email=requester_slack.email,
            )
        else:
            decision = access_control.make_decision_on_approve_request(
                action=approver_action,
                statements=cfg.group_statements,
                group_id=rec.group_id,
                approver_email=approver.email,
                requester_email=requester_slack.email,
            )

        if not decision.permit:
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )
            await turn_context.send_activity(Activity(type="message", text=f"{approver.display_name} you cannot approve this request."))
            return

        # Update card to show approved
        await self._update_approval_card(
            turn_context=turn_context,
            elevator_request_id=elevator_request_id,
            decision_action="approved",
            approver_name=approver.display_name,
            color_style=teams_cards.get_color_style(cfg.good_result_emoji),
        )

        # Execute the decision
        try:
            if rec.kind == ElevatorRequestKind.account:
                access_control.execute_decision(
                    decision=decision,
                    permission_set_name=rec.permission_set_name,
                    account_id=rec.account_id,
                    permission_duration=permission_duration,
                    approver=approver_slack,
                    requester=requester_slack,
                    reason=rec.reason,
                    elevator_request_id=elevator_request_id,
                )
            else:
                sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
                sso_group = sso.describe_group(sso_instance.identity_store_id, rec.group_id, identity_store_client)
                access_control.execute_decision_on_group_request(
                    decision=decision,
                    group=sso_group,
                    permission_duration=permission_duration,
                    approver=approver_slack,
                    requester=requester_slack,
                    reason=rec.reason,
                    identity_store_id=sso_instance.identity_store_id,
                    elevator_request_id=elevator_request_id,
                )
            request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.completed)
        except Exception as e:
            logger.exception(f"Failed to execute decision in _handle_card_action: {e}")
        finally:
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )

    async def _update_approval_card(
        self,
        turn_context,  # noqa: ANN001
        elevator_request_id: str,
        decision_action: str,
        approver_name: str,
        color_style: str,
    ) -> None:
        """Update the approval card to reflect the decision."""
        from botbuilder.schema import Activity, Attachment  # type: ignore[import]

        raw = request_store._get_plain(elevator_request_id)  # noqa: SLF001
        if raw and raw.get("teams_activity_id"):
            # We don't have the original card stored, build a minimal updated card
            pass

        # Build a simple updated card showing the decision
        updated_card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {
                    "type": "Container",
                    "style": color_style,
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": "AWS Access Request",
                            "size": "large",
                            "weight": "bolder",
                        }
                    ],
                },
                {
                    "type": "TextBlock",
                    "text": f"Request {decision_action} by {approver_name}",
                    "wrap": True,
                    "weight": "bolder",
                },
            ],
        }

        try:
            activity = Activity(
                type="message",
                id=turn_context.activity.reply_to_id or turn_context.activity.id,
                attachments=[
                    Attachment(
                        content_type="application/vnd.microsoft.card.adaptive",
                        content=updated_card,
                    )
                ],
            )
            await turn_context.update_activity(activity)
        except Exception as e:
            logger.exception(f"Failed to update approval card: {e}")


async def handle_teams_event(event: dict, context) -> dict:  # noqa: ANN001, ARG001
    """Convert API Gateway event body to Bot Framework activity and process it."""
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings  # type: ignore[import]
    from botbuilder.schema import Activity  # type: ignore[import]

    settings = BotFrameworkAdapterSettings(
        app_id=cfg.teams_microsoft_app_id,
        app_password=cfg.teams_microsoft_app_password,
    )
    adapter = BotFrameworkAdapter(settings)

    body = event.get("body", "")
    if isinstance(body, str):
        body = json.loads(body) if body else {}

    headers = event.get("headers", {}) or {}

    activity = Activity.deserialize(body)

    bot = SSOElevatorBot()

    async def bot_callback(turn_context) -> None:  # noqa: ANN001
        activity_type = turn_context.activity.type

        if activity_type == "message":
            await bot.on_message_activity(turn_context)
        elif activity_type == "invoke":
            await bot.on_invoke_activity(turn_context)

    auth_header = headers.get("Authorization") or headers.get("authorization") or ""

    try:
        await adapter.process_activity(activity, auth_header, bot_callback)
    except Exception as e:
        logger.exception(f"Error processing Teams activity: {e}")
        return {"statusCode": 500, "body": ""}

    return {"statusCode": 200, "body": ""}


def lambda_handler(event: str, context):  # noqa: ANN001, ANN201
    if cfg.chat_platform == "teams":
        return asyncio.run(handle_teams_event(event, context))
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


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
        if user_id:
            request_store.put_view_id(
                str(user_id),
                callback_id,
                str(response.data["view"]["id"]),  # type: ignore # noqa: PGH003
            )
        logger.debug("Stored view_id in request store for modal update")

        return response

    return show_initial_form_for_request


def load_select_options_for_group_access_request(client: WebClient, body: dict) -> SlackResponse:
    logger.info("Loading select options for view (groups)")
    logger.debug("Request body", extra={"body": body})
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    groups = sso.get_groups_from_config(sso_instance.identity_store_id, identity_store_client, cfg)

    user_id = body.get("user", {}).get("id")
    callback_id = slack_helpers.RequestForGroupAccessView.CALLBACK_ID

    view_id = request_store.get_view_id(str(user_id), callback_id) if user_id else None
    if not view_id:
        logger.warning(
            f"View ID not found for user {user_id} callback {callback_id}. "
            "This happens when Lambda container is recycled between shortcut invocations. "
            "Opening a new view as fallback."
        )
        # Fallback: open a new view with the data already loaded
        trigger_id = body["trigger_id"]
        view = slack_helpers.RequestForGroupAccessView.update_with_groups(groups=groups)
        return client.views_open(trigger_id=trigger_id, view=view)

    view = slack_helpers.RequestForGroupAccessView.update_with_groups(groups=groups)
    return client.views_update(view_id=view_id, view=view)


def load_select_options_for_account_access_request(client: WebClient, body: dict) -> SlackResponse:
    logger.info("Loading select options for view (accounts and permission sets)")
    logger.debug("Request body", extra={"body": body})

    accounts = organizations.get_accounts_from_config_with_cache(org_client=org_client, s3_client=s3_client, cfg=cfg)
    permission_sets = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=cfg)

    user_id = body.get("user", {}).get("id")
    callback_id = slack_helpers.RequestForAccessView.CALLBACK_ID

    view_id = request_store.get_view_id(str(user_id), callback_id) if user_id else None
    if not view_id:
        logger.warning(
            f"View ID not found for user {user_id} callback {callback_id}. "
            "This happens when Lambda container is recycled between shortcut invocations. "
            "Opening a new view as fallback."
        )
        # Fallback: open a new view with the data already loaded
        trigger_id = body["trigger_id"]
        view = slack_helpers.RequestForAccessView.update_with_accounts_and_permission_sets(
            accounts=accounts, permission_sets=permission_sets
        )
        return client.views_open(trigger_id=trigger_id, view=view)

    view = slack_helpers.RequestForAccessView.update_with_accounts_and_permission_sets(accounts=accounts, permission_sets=permission_sets)
    return client.views_update(view_id=view_id, view=view)


app.shortcut("request_for_access")(
    build_initial_form_handler(view_class=slack_helpers.RequestForAccessView),  # type: ignore # noqa: PGH003
    load_select_options_for_account_access_request,
)

app.shortcut("request_for_group_membership")(
    build_initial_form_handler(view_class=slack_helpers.RequestForGroupAccessView),  # type: ignore # noqa: PGH003
    load_select_options_for_group_access_request,
)


@handle_errors
def handle_button_click(body: dict, client: WebClient, context: BoltContext) -> SlackResponse:  # noqa: ARG001
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

    if not request_store.try_begin_in_flight_approval(
        requester_slack_id=payload.request.requester_slack_id,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        group_id=None,
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
        if payload.elevator_request_id:
            request_store.update_request_status(payload.elevator_request_id, ElevatorRequestStatus.discarded)

        request_store.end_in_flight_approval(
            requester_slack_id=payload.request.requester_slack_id,
            account_id=payload.request.account_id,
            permission_set_name=payload.request.permission_set_name,
            group_id=None,
        )
        if cfg.send_dm_if_user_not_in_channel and not is_user_in_channel:
            logger.info(f"User {requester.id} is not in the channel. Sending DM with message: {dm_text}")
            client.chat_postMessage(channel=requester.id, text=dm_text)
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=text,
            thread_ts=payload.thread_ts,
        )

    decision = access_control.make_decision_on_approve_request(
        action=payload.action,
        statements=cfg.statements,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        approver_email=approver.email,
        requester_email=requester.email,
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    if not decision.permit:
        request_store.end_in_flight_approval(
            requester_slack_id=payload.request.requester_slack_id,
            account_id=payload.request.account_id,
            permission_set_name=payload.request.permission_set_name,
            group_id=None,
        )
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
        elevator_request_id=payload.elevator_request_id,
    )
    if payload.elevator_request_id:
        request_store.update_request_status(payload.elevator_request_id, ElevatorRequestStatus.completed)
    request_store.end_in_flight_approval(
        requester_slack_id=payload.request.requester_slack_id,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        group_id=None,
    )
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
    decision = access_control.make_decision_on_access_request(
        cfg.statements,
        account_id=request.account_id,
        permission_set_name=request.permission_set_name,
        requester_email=requester.email,
    )
    logger.info("Decision on request was made", extra={"decision": decision.dict()})

    account = organizations.describe_account(org_client, request.account_id)

    elevator_id = str(uuid.uuid4())
    request_store.put_access_request(
        ElevatorRequestRecord(
            elevator_request_id=elevator_id,
            kind=ElevatorRequestKind.account,
            status=ElevatorRequestStatus.awaiting_approval,
            requester_slack_id=request.requester_slack_id,
            reason=request.reason,
            permission_duration_seconds=int(request.permission_duration.total_seconds()),
            account_id=request.account_id,
            permission_set_name=request.permission_set_name,
        )
    )

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
            elevator_request_id=elevator_id,
        ),
        channel=cfg.slack_channel_id,
        text=f"Request for access to {account.name} account from {requester.real_name}",
    )
    if slack_response.get("ts") is not None:
        request_store.update_slack_presentation(elevator_id, cfg.slack_channel_id, str(slack_response["ts"]))

    if show_buttons:
        ts = slack_response["ts"]
        if ts is not None:
            schedule.schedule_discard_buttons_event(
                schedule_client=schedule_client,
                time_stamp=ts,
                channel_id=cfg.slack_channel_id,
                elevator_request_id=elevator_id,
            )
            schedule.schedule_approver_notification_event(
                schedule_client=schedule_client,
                message_ts=ts,
                channel_id=cfg.slack_channel_id,
                time_to_wait=timedelta(
                    minutes=cfg.approver_renotification_initial_wait_time,
                ),
                elevator_request_id=elevator_id,
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
        elevator_request_id=elevator_id,
    )
    if decision.grant and elevator_id:
        request_store.update_request_status(elevator_id, ElevatorRequestStatus.completed)

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
