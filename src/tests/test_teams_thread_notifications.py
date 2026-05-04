"""Thread parent selection and proactive in-thread send transport fallback (Bot Framework)."""

import asyncio
import json
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import config as config_module
import entities
import request_store
import schedule
from events import ApproverNotificationEvent
from requester.teams.teams_notifier import TeamsNotifier
from requester.teams.teams_threading import (
    parent_activity_id_for_bot_thread_reply,
    thread_follow_up_reply_parent_candidates,
    thread_root_activity_id_for_reply,
)
from revoker import _teams_reply_parent_activity_candidates
from revoker import handle_approvers_renotification_event
from tests.test_config import valid_config_dict


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.invalid/v3/conversations/x/activities")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError("err", request=req, response=resp)


def test_parent_activity_id_prefers_thread_root_when_differs_from_card() -> None:
    cid = "19:chan@thread.tacv2;messageid=root-msg"
    card_id = "launcher-or-card-id"
    assert parent_activity_id_for_bot_thread_reply(cid, card_id) == "root-msg"


def test_parent_activity_id_falls_back_to_card_when_no_root_in_conversation() -> None:
    cid = "19:chan@thread.tacv2"
    card_id = "card-only"
    assert parent_activity_id_for_bot_thread_reply(cid, card_id) == "card-only"


def test_teams_reply_parent_activity_candidates_unique_order() -> None:
    tc = "19:c@t;messageid=root1"
    ta = "card99"
    out = _teams_reply_parent_activity_candidates(tc, ta)
    assert out == ["card99", "root1"]
    assert thread_follow_up_reply_parent_candidates(tc, ta) == out


def test_thread_follow_up_candidates_card_only() -> None:
    assert thread_follow_up_reply_parent_candidates("19:c@thread.tacv2", "only-card") == ["only-card"]


def test_thread_follow_up_candidates_root_only_in_conversation() -> None:
    assert thread_follow_up_reply_parent_candidates("19:c@t;messageid=rootx", "") == ["rootx"]


def test_teams_reply_parent_activity_candidates_dedupes() -> None:
    tc = "19:c@t;messageid=same"
    ta = "same"
    out = _teams_reply_parent_activity_candidates(tc, ta)
    assert out == ["same"]


@pytest.fixture
def teams_notifier_config(monkeypatch: pytest.MonkeyPatch) -> config_module.Config:
    for k, v in valid_config_dict().items():
        monkeypatch.setenv(k, str(v))
    config_module._config = None
    return config_module.Config()  # type: ignore[call-arg]


def test_send_thread_text_transport_fallback_reply_on_404(teams_notifier_config: config_module.Config) -> None:
    cfg = teams_notifier_config
    get_app = AsyncMock(return_value=MagicMock())

    async def _run() -> None:
        notifier = TeamsNotifier(cfg, get_app)
        with (
            patch.object(
                notifier,
                "send_thread_text_as_activity_context_send",
                new_callable=AsyncMock,
                side_effect=_http_error(HTTPStatus.NOT_FOUND),
            ) as m_ac,
            patch.object(notifier, "send_thread_reply_with_entities", new_callable=AsyncMock) as m_reply,
        ):
            await notifier.send_thread_text_with_transport_fallback("parent1", "hello", None)
        m_ac.assert_awaited_once_with("parent1", "hello", None)
        m_reply.assert_awaited_once_with("parent1", "hello", None)

    asyncio.run(_run())


def test_send_thread_text_transport_fallback_propagates_non_404(teams_notifier_config: config_module.Config) -> None:
    cfg = teams_notifier_config
    get_app = AsyncMock(return_value=MagicMock())

    async def _run() -> None:
        notifier = TeamsNotifier(cfg, get_app)
        with (
            patch.object(
                notifier,
                "send_thread_text_as_activity_context_send",
                new_callable=AsyncMock,
                side_effect=_http_error(HTTPStatus.BAD_REQUEST),
            ),
            patch.object(notifier, "send_thread_reply_with_entities", new_callable=AsyncMock) as m_reply,
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await notifier.send_thread_text_with_transport_fallback("p", "t", None)
        m_reply.assert_not_called()

    asyncio.run(_run())


def test_send_thread_text_transport_fallback_requires_parent(teams_notifier_config: config_module.Config) -> None:
    cfg = teams_notifier_config
    get_app = AsyncMock(return_value=MagicMock())

    async def _run() -> None:
        notifier = TeamsNotifier(cfg, get_app)
        with pytest.raises(ValueError, match="parent_activity_id"):
            await notifier.send_thread_text_with_transport_fallback("", "x", None)

    asyncio.run(_run())


def test_schedule_approver_notification_event_serializes_teams_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in valid_config_dict().items():
        monkeypatch.setenv(k, str(v))
    config_module._config = None
    monkeypatch.setattr(schedule, "cfg", config_module.Config())  # type: ignore[call-arg]
    mock_client = MagicMock()
    schedule.schedule_approver_notification_event(
        mock_client,
        message_ts="",
        channel_id="",
        time_to_wait=timedelta(minutes=5),
        elevator_request_id="e1",
        teams_conversation_id="19:c@t;messageid=r",
        teams_activity_id="act1",
    )
    mock_client.create_schedule.assert_called_once()
    call_kw = mock_client.create_schedule.call_args.kwargs
    payload = json.loads(call_kw["Target"]["Input"])
    assert payload["elevator_request_id"] == "e1"
    assert payload["teams_conversation_id"] == "19:c@t;messageid=r"
    assert payload["teams_activity_id"] == "act1"


def test_teams_renotification_does_not_reschedule_after_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in valid_config_dict().items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("chat_platform", "teams")
    monkeypatch.setenv("teams_microsoft_app_id", "app-id")
    monkeypatch.setenv("teams_microsoft_app_password", "app-secret")
    monkeypatch.setenv("teams_azure_tenant_id", "tenant-id")
    monkeypatch.setenv("teams_approval_conversation_id", "19:approval@thread.tacv2")
    config_module._config = None

    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    request_store._memory.clear()  # noqa: SLF001

    eid = "e-finalized"
    request_store.put_access_request(
        entities.elevator_request.ElevatorRequestRecord(
            elevator_request_id=eid,
            kind=entities.elevator_request.ElevatorRequestKind.account,
            status=entities.elevator_request.ElevatorRequestStatus.completed,
            requester_slack_id="U1",
            requester_display_name="R",
            requester_email="r@example.com",
            reason="x",
            permission_duration_seconds=60,
            account_id="123456789012",
            permission_set_name="Admin",
            group_id=None,
            slack_channel_id="C",
            slack_message_ts="T",
        )
    )

    event = ApproverNotificationEvent(
        action="approvers_renotification",
        schedule_name="approvers-renotificationX",
        time_stamp="1",
        channel_id="C",
        time_to_wait_in_seconds=60.0,
        elevator_request_id=eid,
        teams_conversation_id="19:c@thread.tacv2;messageid=root",
        teams_activity_id="act1",
    )

    scheduler_client = MagicMock()
    monkeypatch.setattr(schedule, "delete_schedule", MagicMock())
    monkeypatch.setattr(schedule, "schedule_approver_notification_event", MagicMock())

    handle_approvers_renotification_event(
        event=event,
        slack_client=MagicMock(),
        scheduler_client=scheduler_client,
    )

    schedule.schedule_approver_notification_event.assert_not_called()


def test_thread_root_activity_id_for_reply_parses_suffix() -> None:
    assert thread_root_activity_id_for_reply("19:x@t;messageid=abc") == "abc"
    assert thread_root_activity_id_for_reply("19:x@t") == ""


def test_thread_methods_use_full_conversation_id_override_for_url(teams_notifier_config: config_module.Config) -> None:
    cfg = teams_notifier_config
    app_m = MagicMock()
    app_m.api.service_url = "https://smba.test.example/"
    app_m.activity_sender.send = AsyncMock()
    get_app = AsyncMock(return_value=app_m)

    async def _run() -> None:
        notifier = TeamsNotifier(
            cfg,
            get_app,
            conversation_id_override="19:ch@thread.tacv2;messageid=root1",
            service_url_override="https://smba.test.example/",
        )
        with patch("requester.teams.teams_notifier._send_in_conversation", new_callable=AsyncMock, return_value="aid") as m_send:
            await notifier.send_thread_reply("parent1", "hello")
            assert m_send.await_args.kwargs["conversation_id"] == "19:ch@thread.tacv2;messageid=root1"

    asyncio.run(_run())
