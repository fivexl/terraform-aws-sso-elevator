"""Register Microsoft Teams App routes (microsoft-teams-apps) for SSO Elevator."""

from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta
from typing import Any, cast

import access_control
import config
import entities
import group
import organizations
import request_store
import sso
from entities.elevator_request import ElevatorRequestKind, ElevatorRequestRecord, ElevatorRequestStatus
from errors import SSOUserNotFound
from microsoft_teams.api import (
    Attachment,
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

from . import teams_activity_helpers, teams_approval_deferred, teams_cards, teams_users
from .teams_card_action_parse import (
    parse_adaptive_card_invoke_value,
    value_from_message_activity_for_adaptive_submit,
)
from .teams_deps import TeamsDependencies
from .teams_notifier import TeamsNotifier, _teams_channel_id_and_thread_root_activity_id
from .teams_threading import message_activity_id_for_channel_card_invoke

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

    def _teams_conversation_id_from_activity(ctx: ActivityContext[Any]) -> str:
        conv = getattr(ctx.activity, "conversation", None)
        if conv is None:
            return ""
        cid = getattr(conv, "id", None)
        if cid is None:
            return ""
        return str(cid).strip()

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

    def _message_command_kind(raw: str) -> str | None:
        """Map message text to account vs group. /slash, hyphen titles (manifest), or space (from menu)."""
        text = (raw or "").strip()
        text = re.sub(r"^<at>[^<]*</at>\s*", "", text, count=1)
        low = text.lower()
        if "/request-access" in low or low in ("request access", "request-access"):
            return "account"
        if "/request-group" in low or low in ("request group", "request-group"):
            return "group"
        return None

    def _build_card_for_approval_update(eid: str, rec: ElevatorRequestRecord) -> dict[str, Any]:
        """Match the posted approval card (FactSet, etc.); buttons stripped in update_card_after_decision."""
        waiting_style = teams_cards.get_color_style(c.waiting_result_emoji)
        requester_name = (rec.requester_display_name or rec.requester_slack_id or "Requester").strip() or "Requester"
        duration_str = str(timedelta(seconds=rec.permission_duration_seconds))
        if rec.kind == ElevatorRequestKind.group:
            sso_instance = sso.describe_sso_instance(sso_client, c.sso_instance_arn)
            try:
                sso_group = sso.describe_group(sso_instance.identity_store_id, str(rec.group_id or ""), identity_store_client)
            except Exception:
                sso_group = entities.aws.SSOGroup(
                    id=rec.group_id or "",
                    name=rec.group_id or "",
                    description=None,
                    identity_store_id="",
                )
            request_data: dict[str, Any] = {
                "group_id": rec.group_id or "",
                "duration": duration_str,
                "reason": rec.reason,
                "requester_id": rec.requester_slack_id,
            }
            return teams_cards.build_approval_card(
                requester_name=requester_name,
                account=None,
                group=sso_group,
                role_name=None,
                reason=rec.reason,
                permission_duration=duration_str,
                show_buttons=True,
                color_style=waiting_style,
                request_data=request_data,
                elevator_request_id=eid,
            )
        try:
            account = organizations.describe_account(org_client, rec.account_id or "")
        except Exception:
            account = entities.aws.Account(id=rec.account_id or "", name=rec.account_id or "")
        request_data_acc: dict[str, Any] = {
            "account_id": rec.account_id or "",
            "permission_set": rec.permission_set_name or "",
            "duration": duration_str,
            "reason": rec.reason,
            "requester_id": rec.requester_slack_id,
        }
        return teams_cards.build_approval_card(
            requester_name=requester_name,
            account=account,
            group=None,
            role_name=rec.permission_set_name or "",
            reason=rec.reason,
            permission_duration=duration_str,
            show_buttons=True,
            color_style=waiting_style,
            request_data=request_data_acc,
            elevator_request_id=eid,
        )

    async def _update_approval_card(
        turn_context: ActivityContext[Any],
        elevator_request_id: str,
        decision_action: str,
        color_style: str,
    ) -> None:
        rec = request_store.get_access_request(elevator_request_id)
        if rec is not None:
            try:
                orig = _build_card_for_approval_update(elevator_request_id, rec)
            except Exception as e:
                log.exception("Failed to rebuild approval card for in-place update %s: %s", elevator_request_id, e)
                orig = None
        else:
            orig = None
        if orig is not None:
            updated_card = teams_cards.update_card_after_decision(orig, decision_action, color_style)
        else:
            log.warning("Using minimal approval card update (missing record or rebuild error) for %s", elevator_request_id)
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
                        "text": f"Request {decision_action}",
                        "wrap": True,
                        "weight": "bolder",
                    },
                ],
            }
        act_id = message_activity_id_for_channel_card_invoke(turn_context.activity) or str(
            getattr(turn_context.activity, "id", None) or "",
        )
        tpid = request_store.get_teams_presentation_ids(elevator_request_id)
        if (not (act_id or "").strip()) and tpid is not None:
            act_id = tpid[1]
        raw_cid = ""
        if turn_context.activity.conversation:
            raw_cid = str(turn_context.activity.conversation.id)
        if (not (raw_cid or "").strip()) and tpid is not None:
            raw_cid = tpid[0]
        if not (act_id or "").strip() or not (raw_cid or "").strip():
            log.warning("Cannot update approval card: no activity or conversation for %s", elevator_request_id)
            return
        try:
            base_cid, _thr = _teams_channel_id_and_thread_root_activity_id(raw_cid)
            conv_id = base_cid if base_cid else raw_cid
            up = MessageActivityInput().add_attachments(
                Attachment(content_type="application/vnd.microsoft.card.adaptive", content=updated_card)
            )
            up.id = str(act_id)
            await turn_context.api.conversations.activities(conv_id).update(str(act_id), up)
        except Exception as e:
            log.exception("Failed to update approval card: %s", e)

    async def _handle_approval_card_submission(  # noqa: PLR0911, PLR0912, PLR0915
        ctx: ActivityContext[Any],
        elevator_request_id: str,
        action: str | None,
    ) -> None:
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
            try:
                await teams_activity_helpers.teams_send_text_message(
                    ctx,
                    "Could not identify you as a Teams user. Please try again or contact an admin.",
                )
            except Exception as e2:
                log.exception("Failed to send user-visible error after get_user failure: %s", e2)
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
        str_action = (str(action) if action is not None else "").strip().lower()
        if not str_action:
            str_action = "discard"
        approver_action = entities.ApproverAction.Approve if str_action == "approve" else entities.ApproverAction.Discard

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
                color_style=teams_cards.get_color_style(c.bad_result_emoji),
            )
            await teams_activity_helpers.teams_send_text_with_user_mention(
                ctx,
                text_before_mention="Request was discarded by ",
                text_after_mention=".",
                user_id=approver.id,
                display_name=approver.display_name,
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
            await teams_activity_helpers.teams_send_text_with_user_mention(
                ctx,
                text_before_mention="",
                text_after_mention=", you cannot approve this request.",
                user_id=approver.id,
                display_name=approver.display_name,
            )
            return

        await _update_approval_card(
            turn_context=ctx,
            elevator_request_id=elevator_request_id,
            decision_action="approved",
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
            await teams_activity_helpers.teams_send_text_with_user_mention(
                ctx,
                text_before_mention="Permissions have been granted by ",
                text_after_mention=".",
                user_id=approver.id,
                display_name=approver.display_name,
            )
        except Exception as e:
            log.exception("Failed to execute decision in on_adaptive_card_action: %s", e)
        finally:
            request_store.end_in_flight_approval(
                requester_slack_id=rec.requester_slack_id,
                account_id=rec.account_id,
                permission_set_name=rec.permission_set_name,
                group_id=rec.group_id,
            )

    @app.on_message
    async def on_message(ctx: ActivityContext[MessageActivity]) -> Any:  # noqa: PLR0911
        text = (ctx.activity.text or "").strip()
        kind0 = _message_command_kind(text)
        if kind0 is None:
            v = value_from_message_activity_for_adaptive_submit(ctx.activity)
            eid, act = parse_adaptive_card_invoke_value(v)
            if v is not None and eid:
                if act is not None and str(act).strip().lower() not in ("approve", "discard"):
                    return None
                log.info(
                    "Teams approval from message activity (not invoke) eid=%r act=%r",
                    eid,
                    act,
                )
                await _handle_approval_card_submission(ctx, eid, act)
            return None
        if kind0 == "account" and not c.statements:
            await ctx.send("Statements are not configured, please check the configuration.")
            return None
        if kind0 == "group" and not c.group_statements:
            await ctx.send("Group statements are not configured, please check the configuration.")
            return None

        kind = kind0

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

        launcher = teams_cards.build_request_access_launcher_card(kind)
        await ctx.send(
            teams_activity_helpers.teams_message_with_adaptive_card(
                "SSO Elevator — use the button on the card to open the request form.",
                launcher,
            )
        )
        return None

    @app.on_dialog_open
    async def on_task_fetch(
        ctx: ActivityContext[TaskFetchInvokeActivity],
    ) -> TaskModuleResponse:
        data = _extract_task_data(ctx)
        kind_raw = data.get("kind", "account")
        kind = str(kind_raw) if kind_raw in ("account", "group") else "account"
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
            return TaskModuleResponse(
                task=TaskModuleMessageResponse(
                    type="message",
                    value="SSO Elevator could not find your user in AWS IAM Identity Center. Ask an admin to sync your directory account.",
                )
            )
        except Exception as e:
            log.exception("Error checking SSO user in on_task_fetch: %s", e)
            return TaskModuleResponse(
                task=TaskModuleMessageResponse(
                    type="message",
                    value="An unexpected error occurred. Check the logs for details.",
                )
            )

        card = await _build_form_card(kind)
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
        teams_conversation_id = _teams_conversation_id_from_activity(_ctx)
        su = getattr(_ctx.activity, "service_url", None)
        teams_service_url = str(su).strip() if su else ""
        rpar = getattr(_ctx.activity, "reply_to_id", None)
        teams_parent_activity_id = str(rpar).strip() if rpar else ""
        launcher_activity_id = teams_activity_helpers.launcher_activity_id_for_task_submit(_ctx)
        log.info(
            "Account access task submit: conversation_id=%r service_url=%s reply_to_id=%r launcher_id=%r",
            teams_conversation_id,
            bool(teams_service_url),
            teams_parent_activity_id or None,
            launcher_activity_id or None,
        )

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
                requester_display_name=(user.display_name or "").strip() or None,
                reason=reason,
                permission_duration_seconds=int(permission_duration.total_seconds()),
                account_id=account_id,
                permission_set_name=permission_set_name,
            )
        )

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

        # Posting to the approval channel is deferred (async self-invoke) so task/submit returns under Teams'
        # client timeout (~15s). Local/tests without AWS_LAMBDA_FUNCTION_NAME run the post in-process.
        deps = TeamsDependencies(
            cfg=c,
            org_client=org_client,
            s3_client=s3_client,
            sso_client=sso_client,
            identity_store_client=identity_store_client,
            schedule_client=schedule_client,
        )
        if not teams_conversation_id:
            log.warning(
                "Account task submit: no conversation id on activity; skip posting approval card to Teams",
            )
        elif os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            try:
                tthr = teams_approval_deferred.AccountApprovalTeamsThread(
                    conversation_id=teams_conversation_id,
                    service_url=teams_service_url,
                    parent_activity_id=teams_parent_activity_id,
                    launcher_activity_id=launcher_activity_id,
                )
                teams_approval_deferred.invoke_account_approval_post_async(
                    elevator_id,
                    user.display_name,
                    user.email,
                    tthr,
                )
            except Exception as e:
                log.exception("Failed to schedule Teams approval post: %s", e)
        else:
            try:
                tthr = teams_approval_deferred.AccountApprovalTeamsThread(
                    conversation_id=teams_conversation_id,
                    service_url=teams_service_url,
                    parent_activity_id=teams_parent_activity_id,
                    launcher_activity_id=launcher_activity_id,
                )
                await teams_approval_deferred.post_account_approval_to_teams_channel(
                    deps,
                    elevator_id,
                    user.display_name,
                    user.email,
                    tthr,
                )
            except Exception as e:
                log.exception("Failed to post approval card to Teams channel: %s", e)

        # Submitted state hides the form button; deferred in-place update then replaces with the approval card.
        await teams_activity_helpers.update_teams_launcher_message_after_task_submit(_ctx, "account")

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
        act = ctx.activity
        log.info(
            "Teams on_card_action activity_type=%s invoke=%s",
            getattr(act, "type", None),
            getattr(act, "name", None),
        )
        val = ctx.activity.value
        elevator_request_id, action = parse_adaptive_card_invoke_value(val)
        if not elevator_request_id:
            raw_dump: dict[str, Any] = (
                val.model_dump(mode="json", by_alias=True)  # type: ignore[no-untyped-call]
                if val is not None and hasattr(val, "model_dump")
                else {}
            )
            log.warning("Card action missing elevator_request_id; value keys sample: %r", list(raw_dump)[:20])
            return
        await _handle_approval_card_submission(ctx, elevator_request_id, action)
