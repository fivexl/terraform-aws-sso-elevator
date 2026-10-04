"""Revoker tests: per-invocation Slack token read, loop paths surviving Slack failures,
and how revocations and expiry show on the request message."""

import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import config
import entities
import events
import sso


@pytest.fixture
def revoker():
    sys.modules.pop("revoker", None)
    with patch("boto3.client", return_value=MagicMock()):
        import revoker

        yield revoker
    sys.modules.pop("revoker", None)


def _discard_event() -> dict:
    return {"action": "discard_buttons_event", "schedule_name": "s", "time_stamp": "1", "channel_id": "C1"}


def test_lambda_handler_reads_slack_bot_token_per_invocation_in_degrade_mode(revoker):
    with (
        patch.object(revoker.config, "get_slack_secret", side_effect=["xoxb-first", "xoxb-rotated"]) as mock_get_secret,
        patch.object(revoker.slack_sdk, "WebClient") as mock_web_client,
        patch.object(revoker, "handle_discard_buttons_event") as mock_handle,
    ):
        revoker.lambda_handler(_discard_event(), None)
        revoker.lambda_handler(_discard_event(), None)

    assert mock_get_secret.call_count == 2
    mock_get_secret.assert_called_with(revoker.ssm_client, config.SLACK_BOT_TOKEN_PARAMETER_ENV, degrade_on_failure=True)
    assert [c.kwargs["token"] for c in mock_web_client.call_args_list] == ["xoxb-first", "xoxb-rotated"]
    assert mock_handle.call_args.kwargs["slack_client"] is mock_web_client.return_value


def test_account_revocation_loop_continues_after_slack_failure(revoker):
    assignments = [
        sso.AccountAssignment(account_id=account_id, permission_set_arn="ps", principal_id="u", principal_type="USER")
        for account_id in ("111111111111", "222222222222")
    ]
    slack_client = MagicMock()
    slack_client.chat_postMessage.side_effect = [RuntimeError("invalid_auth"), MagicMock()]
    with (
        patch.object(revoker.sso, "get_account_assignment_information", return_value=assignments),
        patch.object(revoker.schedule, "get_scheduled_events", return_value=[]),
        patch.object(revoker.sso, "describe_sso_instance", return_value=MagicMock(arn="arn:instance")),
        patch.object(revoker.sso, "delete_account_assignment_and_wait_for_result") as mock_delete,
        patch.object(revoker.sso, "describe_permission_set"),
        patch.object(revoker.s3, "log_operation"),
        patch.object(revoker.organizations, "describe_account"),
        patch.object(revoker.slack_helpers, "create_slack_mention_by_principal_id", return_value="@u"),
    ):
        revoker.handle_sso_elevator_scheduled_revocation(
            sso_client=MagicMock(),
            cfg=MagicMock(post_update_to_slack=True),
            scheduler_client=MagicMock(),
            org_client=MagicMock(),
            slack_client=slack_client,
            identitystore_client=MagicMock(),
        )

    assert [c.args[1].account_id for c in mock_delete.call_args_list] == ["111111111111", "222222222222"]
    assert slack_client.chat_postMessage.call_count == 2


def test_group_revocation_loop_continues_after_slack_failure(revoker):
    assignments = [
        sso.GroupAssignment(group_name="g", group_id="g-1", user_principal_id="u", membership_id=m, identity_store_id="d-1")
        for m in ("m-1", "m-2")
    ]
    slack_client = MagicMock()
    slack_client.chat_postMessage.side_effect = [RuntimeError("invalid_auth"), MagicMock()]
    with (
        patch.object(revoker.sso, "describe_sso_instance", return_value=MagicMock(identity_store_id="d-1")),
        patch.object(revoker.schedule, "get_scheduled_events", return_value=[]),
        patch.object(revoker.sso, "get_group_assignments", return_value=assignments),
        patch.object(revoker.sso, "remove_user_from_group") as mock_remove,
        patch.object(revoker.s3, "log_operation"),
        patch.object(revoker.slack_helpers, "create_slack_mention_by_principal_id", return_value="@u"),
    ):
        revoker.handle_sso_elevator_group_scheduled_revocation(
            identity_store_client=MagicMock(),
            sso_client=MagicMock(),
            scheduler_client=MagicMock(),
            cfg=MagicMock(post_update_to_slack=True),
            slack_client=slack_client,
        )

    assert [c.args[1] for c in mock_remove.call_args_list] == ["m-1", "m-2"]
    assert slack_client.chat_postMessage.call_count == 2


def _users():  # noqa: ANN202
    return {
        "approver": entities.slack.User(id="U_APP", email="a@x.com", real_name="A"),
        "requester": entities.slack.User(id="U_REQ", email="r@x.com", real_name="R"),
    }


def _revoke_event(**overrides) -> events.RevokeEvent:  # noqa: ANN003
    fields = {
        "schedule_name": "s",
        "user_account_assignment": sso.UserAccountAssignment(
            instance_arn="i", account_id="222222222222", permission_set_arn="ps", user_principal_id="u"
        ),
        "permission_duration": timedelta(minutes=30),
        "channel_id": "C1",
        "message_ts": "100.1",
    }
    return events.RevokeEvent(**(_users() | fields | overrides))


def _group_revoke_event(**overrides) -> events.GroupRevokeEvent:  # noqa: ANN003
    fields = {
        "schedule_name": "s",
        "group_assignment": sso.GroupAssignment(
            group_name="admins", group_id="g-1", user_principal_id="u", membership_id="m-1", identity_store_id="d-1"
        ),
        "permission_duration": timedelta(hours=1),
        "channel_id": "C1",
        "message_ts": "100.1",
    }
    return events.GroupRevokeEvent(**(_users() | fields | overrides))


APPROVED_MESSAGE = {
    "ts": "100.1",
    "blocks": [
        {"block_id": "title", "type": "section", "text": {"type": "mrkdwn", "text": "old title"}},
        {"block_id": "reason", "type": "section", "text": {"type": "mrkdwn", "text": ">testing revoker"}},
        {"block_id": "status", "type": "context", "elements": [{"type": "mrkdwn", "text": "old status"}]},
        {"block_id": "source", "type": "context", "elements": [{"type": "mrkdwn", "text": "Requested via Slack"}]},
    ],
}


def _revoke_account(revoker, slack_client, revoke_event, post_update_to_slack=True):  # noqa: ANN001, ANN202
    with (
        patch.object(revoker.sso, "delete_account_assignment_and_wait_for_result", return_value=MagicMock(request_id="r")),
        patch.object(revoker.sso, "describe_permission_set", return_value=MagicMock()) as mock_ps,
        patch.object(revoker.s3, "log_operation"),
        patch.object(revoker.schedule, "delete_schedule"),
        patch.object(revoker.organizations, "describe_account", return_value=entities.aws.Account(id="222222222222", name="aft")),
    ):
        mock_ps.return_value.name = "ReadOnly"
        revoker.handle_scheduled_account_assignment_deletion(
            revoke_event=revoke_event,
            sso_client=MagicMock(),
            cfg=MagicMock(post_update_to_slack=post_update_to_slack, slack_channel_id="C1"),
            scheduler_client=MagicMock(),
            org_client=MagicMock(),
            slack_client=slack_client,
        )


def test_scheduled_revocation_ends_its_request_message_and_replies_in_thread(revoker):
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = {"messages": [APPROVED_MESSAGE]}

    _revoke_account(revoker, slack_client, _revoke_event(), post_update_to_slack=False)

    update = slack_client.chat_update.call_args.kwargs
    assert update["ts"] == "100.1"
    assert update["text"] == ":lock: *Ended · ReadOnly → aft #222222222222 for* <@U_REQ>"
    assert [b["block_id"] for b in update["blocks"]] == ["title", "reason", "status", "source"]
    status = update["blocks"][2]["elements"][0]["text"]
    assert status.startswith("Approved by <@U_APP> · access ended at <!date^")
    slack_client.chat_postMessage.assert_called_once_with(
        channel="C1", thread_ts="100.1", text="Access ended: ReadOnly removed from aft #222222222222"
    )


@pytest.mark.parametrize(
    ("revoke_event", "history"),
    [
        (_revoke_event(channel_id=None, message_ts=None), {"messages": []}),  # scheduled before events carried the request
        (_revoke_event(), {"messages": []}),  # request message deleted
    ],
)
def test_scheduled_revocation_falls_back_to_a_standalone_notice(revoker, revoke_event, history):
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = history

    _revoke_account(revoker, slack_client, revoke_event)

    slack_client.chat_update.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel="C1", text=":broom: Revoked untracked access · ReadOnly → aft #222222222222 for <@U_REQ>"
    )


def test_scheduled_revocation_never_fails_on_slack_errors(revoker):
    slack_client = MagicMock()
    slack_client.conversations_history.side_effect = RuntimeError("invalid_auth")
    slack_client.chat_postMessage.side_effect = RuntimeError("invalid_auth")

    _revoke_account(revoker, slack_client, _revoke_event())  # must not raise


def test_scheduled_group_revocation_ends_its_request_message(revoker):
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = {"messages": [APPROVED_MESSAGE]}
    with (
        patch.object(revoker.sso, "remove_user_from_group"),
        patch.object(revoker.s3, "log_operation"),
        patch.object(revoker.schedule, "delete_schedule"),
    ):
        revoker.handle_scheduled_group_assignment_deletion(
            group_revoke_event=_group_revoke_event(),
            cfg=MagicMock(post_update_to_slack=True, slack_channel_id="C1"),
            scheduler_client=MagicMock(),
            slack_client=slack_client,
            identitystore_client=MagicMock(),
        )

    assert slack_client.chat_update.call_args.kwargs["text"] == ":lock: *Ended · group admins for* <@U_REQ>"
    slack_client.chat_postMessage.assert_called_once_with(channel="C1", thread_ts="100.1", text="Access ended: removed from group admins")


def test_discard_buttons_event_expires_a_pending_request(revoker):
    import slack_helpers

    request = slack_helpers.RequestForAccess(
        permission_set_name="ReadOnly",
        account_id="222222222222",
        account_name="aft",
        reason="testing",
        requester_slack_id="U_REQ",
        permission_duration=timedelta(minutes=30),
    )
    _, blocks = slack_helpers.build_request_message(slack_helpers.RequestCard.for_request(request), slack_helpers.RequestState.pending())
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = {"messages": [{"ts": "1", "blocks": blocks}]}

    with patch.object(revoker.schedule, "delete_schedule"), patch.object(revoker.s3, "log_operation"):
        revoker.handle_discard_buttons_event(
            event=events.DiscardButtonsEvent(**_discard_event()), slack_client=slack_client, scheduler_client=MagicMock()
        )

    update = slack_client.chat_update.call_args.kwargs
    assert update["text"] == ":hourglass: *Expired · ReadOnly → aft #222222222222 for* <@U_REQ>"
    assert [b["block_id"] for b in update["blocks"]] == ["title", "reason", "status", "source"]
    assert update["blocks"][2]["elements"][0]["text"] == "No decision within 8 hours"


def test_discard_buttons_event_strips_a_pre_upgrade_request(revoker):
    old_blocks = [{"block_id": "content", "fields": []}, {"block_id": "buttons", "elements": [{"value": "approve"}]}]
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = {"messages": [{"ts": "1", "text": "old", "blocks": old_blocks}]}

    with patch.object(revoker.schedule, "delete_schedule"):
        revoker.handle_discard_buttons_event(
            event=events.DiscardButtonsEvent(**_discard_event()), slack_client=slack_client, scheduler_client=MagicMock()
        )

    assert [b["block_id"] for b in slack_client.chat_update.call_args.kwargs["blocks"]] == ["content"]


def test_approvers_renotification_skips_a_message_without_blocks(revoker):
    slack_client = MagicMock()
    slack_client.conversations_history.return_value = {"messages": [{"ts": "1", "text": "plain"}]}
    event = events.ApproverNotificationEvent(
        action="approvers_renotification", schedule_name="s", time_stamp="1", channel_id="C1", time_to_wait_in_seconds=60
    )

    with (
        patch.object(revoker.schedule, "delete_schedule"),
        patch.object(revoker.schedule, "schedule_approver_notification_event") as mock_next,
    ):
        revoker.handle_approvers_renotification_event(event=event, slack_client=slack_client, scheduler_client=MagicMock())

    slack_client.chat_postMessage.assert_not_called()
    mock_next.assert_not_called()


def _pending_request_message(revoker, group: bool) -> dict:  # noqa: ANN001
    sh = revoker.slack_helpers
    if group:
        request = sh.RequestForGroupAccess(
            group_id="g-1234", group_name="Admins", reason="need access", requester_slack_id="U_REQ", permission_duration=timedelta(hours=2)
        )
    else:
        request = sh.RequestForAccess(
            permission_set_name="Admin",
            account_id="111111111111",
            account_name="Prod",
            reason="need access",
            requester_slack_id="U_REQ",
            permission_duration=timedelta(hours=2),
            request_source="cli",
            verified_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req",
        )
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    return {"ts": "1", "blocks": blocks}


def _expire(revoker, message: dict, slack_client: MagicMock) -> None:  # noqa: ANN001
    event = revoker.DiscardButtonsEvent.model_validate(_discard_event())
    with (
        patch.object(revoker.slack_helpers, "get_message_from_timestamp", return_value=message),
        patch.object(revoker.schedule, "delete_schedule"),
    ):
        revoker.handle_discard_buttons_event(event=event, slack_client=slack_client, scheduler_client=MagicMock())


@pytest.mark.parametrize("group", [False, True])
def test_expired_request_writes_one_declined_expired_entry(revoker, group):
    slack_client = MagicMock()
    slack_client.users_info.return_value = MagicMock(
        data={"user": {"id": "U_REQ", "real_name": "Req", "profile": {"email": "req@example.com"}}}
    )
    with patch.object(revoker.s3, "log_operation") as mock_log_operation:
        _expire(revoker, _pending_request_message(revoker, group), slack_client)

    slack_client.chat_update.assert_called_once()
    mock_log_operation.assert_called_once()
    entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert (entry.operation_type, entry.decision_reason) == ("declined", "Expired")
    assert (entry.approver_slack_id, entry.approver_email) == ("NA", "NA")
    assert (entry.requester_slack_id, entry.requester_email) == ("U_REQ", "req@example.com")
    assert entry.permission_duration == timedelta(hours=2)
    if group:
        assert (entry.audit_entry_type, entry.group_id, entry.group_name) == ("group", "g-1234", "Admins")
    else:
        assert (entry.audit_entry_type, entry.account_id, entry.role_name) == ("account", "111111111111", "Admin")
        assert entry.request_source == "cli"
        assert entry.verified_arn == "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req"


def test_expired_request_audit_uses_na_email_when_requester_lookup_fails(revoker):
    slack_client = MagicMock()
    slack_client.users_info.side_effect = RuntimeError("user_not_found")
    with patch.object(revoker.s3, "log_operation") as mock_log_operation:
        _expire(revoker, _pending_request_message(revoker, group=False), slack_client)

    assert mock_log_operation.call_args.kwargs["audit_entry"].requester_email == "NA"


def test_expired_request_audit_failure_does_not_block_button_removal(revoker):
    slack_client = MagicMock()
    with patch.object(revoker.s3, "log_operation", side_effect=RuntimeError("s3 down")):
        _expire(revoker, _pending_request_message(revoker, group=False), slack_client)

    slack_client.chat_update.assert_called_once()


def test_already_decided_request_writes_no_expired_entry(revoker):
    message = _pending_request_message(revoker, group=False)
    message["blocks"] = [b for b in message["blocks"] if b["block_id"] != "buttons"]
    slack_client = MagicMock()
    with patch.object(revoker.s3, "log_operation") as mock_log_operation:
        _expire(revoker, message, slack_client)

    slack_client.chat_update.assert_not_called()
    mock_log_operation.assert_not_called()


def test_pre_upgrade_request_writes_no_expired_entry(revoker):
    """Its buttons carry no request to audit; they are only stripped."""
    message = {"ts": "1", "text": "old", "blocks": [{"block_id": "buttons", "elements": [{"value": "approve"}]}]}
    slack_client = MagicMock()
    with patch.object(revoker.s3, "log_operation") as mock_log_operation:
        _expire(revoker, message, slack_client)

    slack_client.chat_update.assert_called_once()
    mock_log_operation.assert_not_called()
