"""Slack-platform branches in revoker (discard expiry, approver renotification)."""

from unittest.mock import MagicMock, patch

import pytest
import slack_sdk

import config as config_module
import entities
import request_store
from entities.elevator_request import ElevatorRequestKind, ElevatorRequestRecord, ElevatorRequestStatus
from events import ApproverNotificationEvent, DiscardButtonsEvent
from revoker import handle_approvers_renotification_event, handle_discard_buttons_event
from tests.test_config import valid_config_dict


def _slack_revoker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in valid_config_dict().items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("chat_platform", "slack")
    monkeypatch.setenv("ELEVATOR_REQUESTS_TABLE_NAME", "memory")
    config_module._config = None


def _base_record(*, eid: str, status: ElevatorRequestStatus) -> ElevatorRequestRecord:
    return ElevatorRequestRecord(
        elevator_request_id=eid,
        kind=ElevatorRequestKind.account,
        status=status,
        requester_slack_id="U1",
        reason="r",
        permission_duration_seconds=3600,
        account_id="123456789012",
        permission_set_name="Admin",
        group_id=None,
        slack_channel_id="C1",
        slack_message_ts="1.2",
    )


def test_slack_discard_skips_when_request_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    _slack_revoker_env(monkeypatch)
    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    request_store._memory.clear()  # noqa: SLF001
    eid = "e-done"
    request_store.put_access_request(_base_record(eid=eid, status=ElevatorRequestStatus.completed))

    slack_client = slack_sdk.WebClient(token="x-test-token")
    scheduler = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "delete_schedule", MagicMock())
    gm = MagicMock()
    monkeypatch.setattr(revoker_module.slack_helpers, "get_message_from_timestamp", gm)

    with patch.object(slack_client, "chat_update", MagicMock()) as chat_update:
        handle_discard_buttons_event(
            DiscardButtonsEvent(
                action="discard_buttons_event",
                schedule_name="sched-x",
                time_stamp="1.2",
                channel_id="C1",
                elevator_request_id=eid,
            ),
            slack_client=slack_client,
            scheduler_client=scheduler,
        )
    gm.assert_not_called()
    chat_update.assert_not_called()


def test_slack_discard_skips_when_not_awaiting_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    _slack_revoker_env(monkeypatch)
    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    request_store._memory.clear()  # noqa: SLF001
    eid = "e-exp"
    request_store.put_access_request(_base_record(eid=eid, status=ElevatorRequestStatus.expired))

    slack_client = slack_sdk.WebClient(token="x-test-token")
    scheduler = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "delete_schedule", MagicMock())
    gm = MagicMock()
    monkeypatch.setattr(revoker_module.slack_helpers, "get_message_from_timestamp", gm)

    with patch.object(slack_client, "chat_update", MagicMock()) as chat_update:
        handle_discard_buttons_event(
            DiscardButtonsEvent(
                action="discard_buttons_event",
                schedule_name="sched-y",
                time_stamp="1.2",
                channel_id="C1",
                elevator_request_id=eid,
            ),
            slack_client=slack_client,
            scheduler_client=scheduler,
        )
    gm.assert_not_called()
    chat_update.assert_not_called()


def test_slack_discard_updates_message_when_awaiting(monkeypatch: pytest.MonkeyPatch) -> None:
    _slack_revoker_env(monkeypatch)
    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    request_store._memory.clear()  # noqa: SLF001
    eid = "e-wait"
    request_store.put_access_request(_base_record(eid=eid, status=ElevatorRequestStatus.awaiting_approval))

    msg = {
        "ts": "9.9",
        "blocks": [
            {"type": "section", "block_id": "content", "text": {"type": "mrkdwn", "text": "x"}},
            {"type": "actions", "block_id": "buttons", "elements": []},
        ],
    }
    slack_client = slack_sdk.WebClient(token="x-test-token")
    scheduler = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "delete_schedule", MagicMock())
    monkeypatch.setattr(
        revoker_module.slack_helpers,
        "get_message_from_timestamp",
        MagicMock(return_value=msg),
    )

    with (
        patch.object(slack_client, "chat_update", MagicMock()) as chat_update,
        patch.object(slack_client, "chat_postMessage", MagicMock()) as chat_post,
    ):
        handle_discard_buttons_event(
            DiscardButtonsEvent(
                action="discard_buttons_event",
                schedule_name="sched-z",
                time_stamp="9.9",
                channel_id="C1",
                elevator_request_id=eid,
            ),
            slack_client=slack_client,
            scheduler_client=scheduler,
        )
    chat_update.assert_called_once()
    chat_post.assert_called_once()


def test_slack_renotification_posts_when_buttons_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _slack_revoker_env(monkeypatch)
    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    msg = {
        "ts": "3.3",
        "blocks": [{"type": "actions", "block_id": "buttons", "elements": []}],
    }
    slack_client = slack_sdk.WebClient(token="x-test-token")
    scheduler = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "delete_schedule", MagicMock())
    sched_next = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "schedule_approver_notification_event", sched_next)
    monkeypatch.setattr(
        revoker_module.slack_helpers,
        "get_message_from_timestamp",
        MagicMock(return_value=msg),
    )

    with patch.object(slack_client, "chat_postMessage", MagicMock()) as chat_post:
        handle_approvers_renotification_event(
            ApproverNotificationEvent(
                action="approvers_renotification",
                schedule_name="ren1",
                time_stamp="1.0",
                channel_id="C9",
                time_to_wait_in_seconds=120.0,
                elevator_request_id=None,
            ),
            slack_client=slack_client,
            scheduler_client=scheduler,
        )
    chat_post.assert_called_once()
    sched_next.assert_called_once()


def test_slack_renotification_no_schedule_when_no_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    _slack_revoker_env(monkeypatch)
    import revoker as revoker_module

    revoker_module.cfg = config_module.Config()  # type: ignore[call-arg]
    msg = {"ts": "4.4", "blocks": [{"type": "section", "block_id": "content", "text": {"type": "mrkdwn", "text": "x"}}]}
    slack_client = slack_sdk.WebClient(token="x-test-token")
    scheduler = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "delete_schedule", MagicMock())
    sched_next = MagicMock()
    monkeypatch.setattr(revoker_module.schedule, "schedule_approver_notification_event", sched_next)
    monkeypatch.setattr(
        revoker_module.slack_helpers,
        "get_message_from_timestamp",
        MagicMock(return_value=msg),
    )

    with patch.object(slack_client, "chat_postMessage", MagicMock()) as chat_post:
        handle_approvers_renotification_event(
            ApproverNotificationEvent(
                action="approvers_renotification",
                schedule_name="ren2",
                time_stamp="1.0",
                channel_id="C9",
                time_to_wait_in_seconds=120.0,
                elevator_request_id=None,
            ),
            slack_client=slack_client,
            scheduler_client=scheduler,
        )
    chat_post.assert_not_called()
    sched_next.assert_not_called()
