"""Register Microsoft Teams App routes (microsoft-teams-apps) for SSO Elevator."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import access_control
import config
import entities
import group
import organizations
import request_store
import schedule
import sso
import teams_cards
import teams_users
from entities.elevator_request import ElevatorRequestKind, ElevatorRequestRecord, ElevatorRequestStatus
from errors import SSOUserNotFound
from microsoft_teams.api import (
    Attachment,
    InvokeResponse,
    MessageActivity,
    MessageActivityInput,
    TaskFetchInvokeActivity,
    TaskSubmitInvokeActivity,
    AdaptiveCardInvokeActivity,
)
from microsoft_teams.api.models.task_module.task_module_continue_response import TaskModuleContinueResponse
from microsoft_teams.api.models.task_module.task_module_message_response import TaskModuleMessageResponse
from microsoft_teams.api.models.task_module.task_module_response import TaskModuleResponse
from microsoft_teams.api.models.task_module.task_module_task_info import CardTaskModuleTaskInfo
from microsoft_teams.apps import App
from microsoft_teams.apps.routing.activity_context import ActivityContext

import teams_activity_helpers
from teams_deps import TeamsDependencies
from teams_notifier import TeamsNotifier

log = config.get_logger(service="teams_handlers")


def register_teams_app_handlers(app: App, deps: TeamsDependencies) -> None:
    c = deps.cfg
    org_client = deps.org_client
    s3_client = deps.s3_client
    sso_client = deps.sso_client
    identity_store_client = deps.identity_store_client
    schedule_client = deps.schedule_client

    async def this_teams_app() -> App:
        return app

    def _notifier() -> TeamsNotifier:
        return TeamsNotifier(c, this_teams_app)

    def _extract_task_data(ctx: ActivityContext[Any]) -> dict[str, Any]:
        v = ctx.activity.value
        if v is None:
            return {}
        raw = getattr(v, "data", None)
        if isinstance(raw, dict):
            return raw
        if raw is not None and hasattr(raw, "model_dump"):
            return cast(dict[str, Any], raw.model_dump())
        if hasattr(v, "model_dump"):
            all_d = v.model_dump()
            if isinstance(all_d, dict) and isinstance(all_d.get("data"), dict):
                return cast(dict[str, Any], all_d["data"])
        return {}

    async def _build_form_card(kind: str) -> dict:
        duration_options = (
            [str(timedelta(hours=h)) for h in range(1, c.max_permissions_duration_time + 1)]
            if not c.permission_duration_list_override
            else c.permission_duration_list_override
        )
        if kind == "account":
            accounts = organizations.get_accounts_from_config_with_cache(org_client=org_client, s3_client=s3_client, cfg=c)
            permission_sets = sso.get_permission_sets_from_config_with_cache(sso_client=sso_client, s3_client=s3_client, cfg=c)
            return teams_cards.build_account_access_form(accounts, permission_sets, duration_options)
        sso_instance = sso.describe_sso_instance(sso_client, c.sso_instance_arn)
        groups = sso.get_groups_from_config(sso_instance.identity_store_id, identity_store_client, c)
        return teams_cards.build_group_access_form(groups, duration_options)

    def _task_continue_from_card(
        title: str,
        card: dict,
    ) -> TaskModuleResponse:
        cti = CardTaskModuleTaskInfo(
            title=title,
            height="large",
            width="medium",
            card=Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card),
            completion_bot_id=c.teams_microsoft_app_id,
            fallback_url="",
        )
        return TaskModuleResponse(task=TaskModuleContinueResponse(type="continue", value=cti))

    @app.on_message
    async def on_message(ctx: ActivityContext[MessageActivity]) -> Any:
        text = (ctx.activity.text or "").strip()
        if "/request-access" not in text and "/request-group" not in text:
            return None
        if "/request-access" in text:
            if not c.statements:
                await ctx.send("Statements are not configured, please check the configuration.")
                return None
        elif "/request-group" in text and not c.group_statements:
            await ctx.send("Group statements are not configured, please check the configuration.")
            return None

        if "/request-access" in text:
            kind: str = "account"
        else:
            kind = "group"

        try:
            user = await teams_users.get_user_from_activity(ctx)
            sso_instance = sso.describe_sso_instance(sso_client, c.sso_instance_arn)
            sso.get_user_principal_id_by_email(
                identity_store_client=identity_store_client,
                identity_store_id=sso_instance.identity_store_id,
                email=user.email,
                cfg=c,
            )
        except SSOUserNotFound:
            from_prop = ctx.activity.from_
            name = (from_prop.name or "User") if from_prop else "User"
            await ctx.send(f"<at>{name}</at> Your request failed because SSO Elevator could not find your user in AWS SSO.")
            return None
        except Exception as e:
            log.exception("Error checking SSO user in on_message: %s", e)
            await ctx.send("An unexpected error occurred. Check the logs for details.")
            return None

        form = await _build_form_card(kind)
        title = "Request AWS Account Access" if kind == "account" else "Request AWS Group Access"
        return cast(
            Any,
            InvokeResponse(status=200, body=_task_continue_from_card(title, form)),
        )

    @app.on_dialog_open
    async def on_task_fetch(
        ctx: ActivityContext[TaskFetchInvokeActivity],
    ) -> TaskModuleResponse:
        data = _extract_task_data(ctx)
        kind = data.get("kind", "account")
        card = await _build_form_card(str(kind))
        title = "Request AWS Account Access" if kind == "account" else "Request AWS Group Access"
        return _task_continue_from_card(str(title), card)

    @app.on_dialog_submit
    async def on_task_submit(
        ctx: ActivityContext[TaskSubmitInvokeActivity],
    ) -> TaskModuleResponse:  # noqa: PLR0911, PLR0912, PLR0915
        data = _extract_task_data(ctx)
        is_group = "group_id" in data
        try:
            user = await teams_users.get_user_from_activity(ctx)
        except Exception as e:
            log.exception("Failed to get user in on_task_submit: %s", e)
            return TaskModuleResponse(
                task=TaskModuleMessageResponse(
                    type="message",
                    value="Failed to identify user. Please try again.",
                )
            )

        if is_group:
            gret = await group.handle_teams_group_task_submit(ctx, data, user, _notifier)
            t = (gret.get("task") or {}) if isinstance(gret, dict) else {}
            val = t.get("value", "Your request has been submitted.")
            if isinstance(val, str):
                return TaskModuleResponse(task=TaskModuleMessageResponse(type="message", value=val))
            return TaskModuleResponse(
                task=TaskModuleMessageResponse(
                    type="message",
                    value="Your request has been submitted.",
                )
            )
        return await _handle_account_task_submit(ctx, data, user)

    async def _handle_account_task_submit(  # noqa: PLR0911, PLR0912, PLR0915
        _ctx: ActivityContext[Any],
        data: dict,
        user: entities.teams.TeamsUser,
    ) -> TaskModuleResponse:
        duration_str = str(data.get("duration", "1:00:00"))
        permission_duration = teams_cards.parse_duration_choice(duration_str)
        reason = str(data.get("reason", ""))
        elevator_id = str(uuid.uuid4())
        slack_user = user.to_slack_user()
        account_id = str(data.get("account_id", ""))
        permission_set_name = str(data.get("permission_set", ""))

        decision = access_control.make_decision_on_access_request(
            c.statements,
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
        color_style = teams_cards.get_color_style(c.waiting_result_emoji)
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
        try:
            notifier = _notifier()
            act_id = await notifier.send_message(text="New access request", card=card)
            if act_id:
                request_store.update_teams_presentation(elevator_id, c.teams_approval_conversation_id, act_id)
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
                    time_to_wait=timedelta(minutes=c.approver_renotification_initial_wait_time),
                    elevator_request_id=elevator_id,
                )
        except Exception as e:
            log.exception("Failed to post approval card to Teams channel: %s", e)

        if decision.grant:
            try:
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
                request_store.update_request_status(elevator_id, ElevatorRequestStatus.completed)
            except Exception as e:
                log.exception("Failed to execute auto-approved decision: %s", e)

        return TaskModuleResponse(
            task=TaskModuleMessageResponse(
                type="message",
                value="Your request has been submitted.",
            )
        )

    @app.on_card_action
    async def on_adaptive_card_action(  # noqa: PLR0911, PLR0912, PLR0915
        ctx: ActivityContext[AdaptiveCardInvokeActivity],
    ) -> None:
        val = ctx.activity.value
        vdict: dict[str, Any] = val.model_dump(mode="json", by_alias=True)  # type: ignore[no-untyped-call]
        ad = (vdict.get("action") or {}) if isinstance(vdict.get("action"), dict) else {}
        if isinstance(ad, dict) and "data" in ad and isinstance(ad.get("data"), dict):
            action_data: dict = ad.get("data") or {}
        else:
            action_data = getattr(getattr(val, "action", None), "data", None) or {}
        if not isinstance(action_data, dict):
            action_data = {}
        value = {**vdict, **(action_data if action_data else {})}
        if isinstance(value.get("action"), dict) and (value.get("action") or {}).get("data"):
            inner = (value.get("action") or {}).get("data")
            if isinstance(inner, dict):
                action_data = inner
        if isinstance(action_data, dict) and action_data:
            elevator_request_id = action_data.get("elevator_request_id") or vdict.get("elevatorRequestId")
            action = action_data.get("action")
        else:
            elevator_request_id = vdict.get("elevatorRequestId")
            action = None
        if action is None and isinstance(vdict.get("action"), dict):
            adata = (vdict.get("action") or {}).get("data")
            if isinstance(adata, dict):
                elevator_request_id = adata.get("elevator_request_id", elevator_request_id)
                action = adata.get("action")

        if not elevator_request_id:
            log.warning("Card action missing elevator_request_id: %r", vdict)
            return

        rec = request_store.get_access_request(elevator_request_id)
        if rec is None:
            log.warning("Access request not found: %s", elevator_request_id)
            return

        if not request_store.try_begin_in_flight_approval(
            requester_slack_id=rec.requester_slack_id,
            account_id=rec.account_id,
            permission_set_name=rec.permission_set_name,
            group_id=rec.group_id,
        ):
            await teams_activity_helpers.teams_send_text_message(
                ctx,
                "This request is already being processed, please wait for the result.",
            )
            return

        try:
            approver = await teams_users.get_user_from_activity(ctx)
        except Exception as e:
            log.exception("Failed to get approver in on_adaptive_card_action: %s", e)
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )
            return

        if rec.kind == ElevatorRequestKind.group:
            await group.handle_teams_group_card_action(
                ctx,
                rec,
                approver,
                str(elevator_request_id),
                str(action or ""),
                _update_approval_card,
            )
            return

        approver_slack = approver.to_slack_user()
        requester_slack = entities.slack.User(
            id=rec.requester_slack_id,
            email="",
            real_name=rec.requester_slack_id,
        )
        permission_duration = timedelta(seconds=rec.permission_duration_seconds)
        str_action = str(action or "")
        approver_action = (
            entities.ApproverAction.Approve
            if str_action in ("approve", entities.ApproverAction.Approve.value)
            else entities.ApproverAction.Discard
        )

        if approver_action == entities.ApproverAction.Discard:
            request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.discarded)
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )
            await _update_approval_card(
                turn_context=ctx,
                elevator_request_id=elevator_request_id,
                decision_action="discarded",
                approver_name=approver.display_name,
                color_style=teams_cards.get_color_style(c.bad_result_emoji),
            )
            return

        decision = access_control.make_decision_on_approve_request(
            action=approver_action,
            statements=c.statements,
            account_id=rec.account_id,
            permission_set_name=rec.permission_set_name,
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
            await teams_activity_helpers.teams_send_text_message(
                ctx, f"{approver.display_name} you cannot approve this request."
            )
            return

        await _update_approval_card(
            turn_context=ctx,
            elevator_request_id=elevator_request_id,
            decision_action="approved",
            approver_name=approver.display_name,
            color_style=teams_cards.get_color_style(c.good_result_emoji),
        )

        try:
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
            request_store.update_request_status(elevator_request_id, ElevatorRequestStatus.completed)
        except Exception as e:
            log.exception("Failed to execute decision in on_adaptive_card_action: %s", e)
        finally:
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )

    async def _update_approval_card(
        turn_context: ActivityContext[Any],
        elevator_request_id: str,
        decision_action: str,
        approver_name: str,
        color_style: str,
    ) -> None:
        request_store._get_plain(elevator_request_id)  # noqa: SLF001
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
        act_id = turn_context.activity.reply_to_id or turn_context.activity.id
        if not act_id or not turn_context.activity.conversation:
            return
        try:
            conv_id = turn_context.activity.conversation.id
            up = MessageActivityInput().add_attachments(
                Attachment(content_type="application/vnd.microsoft.card.adaptive", content=updated_card)
            )
            up.id = str(act_id)
            await turn_context.api.conversations.activities(conv_id).update(str(act_id), up)
        except Exception as e:
            log.exception("Failed to update approval card: %s", e)
