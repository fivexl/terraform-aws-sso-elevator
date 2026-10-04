"""Tests for group requests: submission (group.handle_request_for_group_access_submittion)
and the Approve/Discard click main.handle_button_click routes here by the request's kind."""

import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import access_control
import entities

REQUESTER = entities.slack.User(id="U_REQ", email="requester@example.com", real_name="Requester")
APPROVER_1 = entities.slack.User(id="U_APP1", email="approver1@example.com", real_name="Approver One")
APPROVER_2 = entities.slack.User(id="U_APP2", email="approver2@example.com", real_name="Approver Two")
GROUP = entities.aws.SSOGroup(name="TestGroup", id="g-1234", description="test", identity_store_id="d-1234")


def _decision(reason: access_control.DecisionReason, grant: bool = False, approvers=frozenset()):  # noqa: ANN001, ANN202
    return access_control.AccessRequestDecision(grant=grant, reason=reason, based_on_statements=frozenset(), approvers=approvers)


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "1234567890.123456"}
    client.conversations_members.return_value = MagicMock(data={"members": [REQUESTER.id]})
    return client


@pytest.fixture
def group_module():
    sys.modules.pop("group", None)
    with (
        patch.dict("sys.modules", {}),
        patch("boto3._get_default_session") as mock_session,
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
    ):
        mock_session.return_value.client.return_value = MagicMock()
        import group

        group.cache_for_dublicate_requests.clear()
        yield group
    sys.modules.pop("group", None)


def _thread_replies(client: MagicMock) -> list[str]:
    return [c.kwargs["text"] for c in client.chat_postMessage.call_args_list if c.kwargs.get("thread_ts")]


def _submit(group_module, client, decision, approvers=([], []), execute=None, reason="need access", group=GROUP):  # noqa: ANN001, ANN202, PLR0913
    sh = group_module.slack_helpers
    request = sh.RequestForGroupAccess(
        group_id=GROUP.id, reason=reason, requester_slack_id=REQUESTER.id, permission_duration=timedelta(hours=1)
    )
    with (
        patch.object(sh.RequestForGroupAccessView, "parse", return_value=request),
        patch.object(sh, "get_user", return_value=REQUESTER),
        patch.object(sh, "find_approvers_in_slack", return_value=approvers) as mock_find,
        patch.object(group_module.sso, "describe_group", return_value=group) as mock_describe_group,
        patch.object(group_module.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(group_module.access_control, "make_decision_on_access_request", return_value=decision) as mock_decide,
        patch.object(group_module.access_control, "get_requester_group_ids_if_needed", return_value=frozenset()),
        patch.object(
            group_module.access_control, "execute_decision_on_group_request", side_effect=execute or (lambda **_: [])
        ) as mock_exec,
        patch.object(group_module, "schedule") as mock_schedule,
    ):
        group_module.handle_request_for_group_access_submittion(body={}, ack=MagicMock(), client=client, context=MagicMock())
    return MagicMock(find=mock_find, describe_group=mock_describe_group, decide=mock_decide, execute=mock_exec, schedule=mock_schedule)


def test_requires_approval_all_approvers_found(group_module, slack_client):
    decision = _decision(
        access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver1@example.com", "approver2@example.com"])
    )

    mocks = _submit(group_module, slack_client, decision, approvers=([APPROVER_1, APPROVER_2], []))

    mocks.find.assert_called_once_with(slack_client, decision.approvers)
    posted = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert posted["text"] == ":closed_lock_with_key: *Pending · group TestGroup for* <@U_REQ> *for 1 hour*"
    assert any(b["block_id"] == "buttons" for b in posted["blocks"])
    assert _thread_replies(slack_client) == ["<@U_APP1> <@U_APP2>: waiting for your approval"]
    mocks.schedule.schedule_discard_buttons_event.assert_called_once()


def test_requires_approval_some_approvers_missing(group_module, slack_client):
    decision = _decision(access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver1@example.com", "gone@example.com"]))

    _submit(group_module, slack_client, decision, approvers=([APPROVER_1], ["gone@example.com"]))

    reply = _thread_replies(slack_client)[0]
    assert reply.startswith("<@U_APP1>: waiting for your approval")
    assert "gone@example.com" in reply
    assert "could not be found in Slack" in reply


def test_requires_approval_no_approvers_found(group_module, slack_client):
    decision = _decision(access_control.DecisionReason.RequiresApproval, approvers=frozenset(["gone@example.com"]))

    mocks = _submit(group_module, slack_client, decision, approvers=([], ["gone@example.com"]))

    posted = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert posted["text"].startswith(":x: *Failed · group TestGroup")
    assert not any(b["block_id"] == "buttons" for b in posted["blocks"])
    assert "None of the approvers" in _thread_replies(slack_client)[0]
    mocks.schedule.schedule_discard_buttons_event.assert_not_called()


def test_group_self_approval_is_shown_as_auto_approved_after_the_grant(group_module, slack_client):
    decision = _decision(access_control.DecisionReason.SelfApproval, grant=True)

    def _grant(**kwargs):  # noqa: ANN202, ANN003
        assert slack_client.chat_update.call_count == 0
        assert kwargs["message_ts"] == "1234567890.123456"
        return []

    _submit(group_module, slack_client, decision, execute=_grant)

    assert slack_client.chat_postMessage.call_args_list[0].kwargs["text"].startswith(":hourglass_flowing_sand: *Processing")
    assert slack_client.chat_update.call_args.kwargs["text"] == ":white_check_mark: *Auto-approved · group TestGroup for* <@U_REQ>"
    assert _thread_replies(slack_client)[0].startswith("<@U_REQ> access granted, ends at")


def test_group_submission_reflects_a_grant_failure_and_does_not_claim_success(group_module, slack_client):
    """A failed auto-grant ends Failed with the detail in the thread; the generic
    handler does not post it again at the top level."""
    decision = _decision(access_control.DecisionReason.SelfApproval, grant=True)

    def _fail(**_kwargs):  # noqa: ANN202, ANN003
        raise RuntimeError("boom: group not found")

    _submit(group_module, slack_client, decision, execute=_fail)

    assert slack_client.chat_update.call_args.kwargs["text"].startswith(":x: *Failed · group TestGroup")
    assert _thread_replies(slack_client) == ["Granting access failed: boom: group not found"]
    top_level = [c for c in slack_client.chat_postMessage.call_args_list[1:] if not c.kwargs.get("thread_ts")]
    assert top_level == []


def test_group_submission_rejects_a_reason_over_the_limit(group_module, slack_client):
    """The modal is already closed when this runs, so a DM is all that is left."""
    mocks = _submit(group_module, slack_client, decision=None, reason="x" * 1001)

    mocks.decide.assert_not_called()
    slack_client.chat_postMessage.assert_called_once()
    assert slack_client.chat_postMessage.call_args.kwargs["channel"] == REQUESTER.id
    assert "Reason must be 1000 characters or fewer" in slack_client.chat_postMessage.call_args.kwargs["text"]


def test_group_submission_rejects_a_request_too_large_for_the_button(group_module, slack_client):
    """The group name rides in the button value too, so it counts toward Slack's 2000-character cap."""
    long_named = GROUP.model_copy(update={"name": "n" * 1000})
    mocks = _submit(group_module, slack_client, decision=None, reason="x" * 1000, group=long_named)

    mocks.decide.assert_not_called()
    slack_client.chat_postMessage.assert_called_once()
    assert slack_client.chat_postMessage.call_args.kwargs == {
        "channel": REQUESTER.id,
        "text": "Your access request wasn't submitted: Request is too large for Slack; shorten the reason.",
    }


# ---------------------------------------------------------------------------
# Approve / Discard
# ---------------------------------------------------------------------------


def _payload(group_module, action: entities.ApproverAction = entities.ApproverAction.Approve):  # noqa: ANN001, ANN202
    sh = group_module.slack_helpers
    request = sh.RequestForGroupAccess(
        group_id=GROUP.id, group_name=GROUP.name, reason="r", requester_slack_id=REQUESTER.id, permission_duration=timedelta(hours=1)
    )
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    return sh.ButtonClickedPayload.model_validate(
        {
            "actions": [{"action_id": action.value, "value": request.model_dump_json()}],
            "user": {"id": APPROVER_1.id},
            "message": {"ts": "1234567890.123456", "blocks": blocks},
            "channel": {"id": "C_CHAN"},
        }
    )


def _click(group_module, client, payload, decide=None, execute=None, group_ids=None):  # noqa: ANN001, ANN202, PLR0913
    approve = access_control.ApproveRequestDecision(grant=True, permit=True, based_on_statements=frozenset())
    with (
        patch.object(group_module.slack_helpers, "get_user", side_effect=[APPROVER_1, REQUESTER]),
        patch.object(group_module.sso, "describe_group", return_value=GROUP),
        patch.object(group_module.access_control, "get_requester_group_ids_if_needed", side_effect=group_ids or (lambda *_: frozenset())),
        patch.object(group_module.access_control, "make_decision_on_approve_request", side_effect=decide or (lambda **_: approve)),
        patch.object(
            group_module.access_control, "execute_decision_on_group_request", side_effect=execute or (lambda **_: [])
        ) as mock_exec,
    ):
        group_module.handle_group_button_click(payload=payload, client=client, context={"user_id": APPROVER_1.id})
    return mock_exec


def test_group_approval_lookup_error_is_retryable(group_module, slack_client):
    def _unavailable(*_args):  # noqa: ANN202, ANN002
        raise RuntimeError("SSO unavailable")

    mock_exec = _click(group_module, slack_client, _payload(group_module), group_ids=_unavailable)

    mock_exec.assert_not_called()
    slack_client.chat_update.assert_not_called()
    assert group_module.cache_for_dublicate_requests == {}
    assert "unexpected error" in slack_client.chat_postMessage.call_args.kwargs["text"]


def test_group_button_click_clears_the_dedup_cache_when_make_decision_on_approve_request_raises(group_module, slack_client):
    """#194 A3 residual: an exception here must not leave the request stuck on "already in progress"."""

    def _boom(**_kwargs):  # noqa: ANN202, ANN003
        raise RuntimeError("boom")

    mock_exec = _click(group_module, slack_client, _payload(group_module), decide=_boom)

    assert group_module.cache_for_dublicate_requests == {}
    mock_exec.assert_not_called()


def test_group_button_click_strips_buttons_then_shows_approved(group_module, slack_client):
    def _grant(**kwargs):  # noqa: ANN202, ANN003
        early = slack_client.chat_update.call_args.kwargs
        assert not any(b["block_id"] == "buttons" for b in early["blocks"])
        assert kwargs["channel_id"] == "C_CHAN"
        return []

    _click(group_module, slack_client, _payload(group_module), execute=_grant)

    assert slack_client.chat_update.call_count == 2  # noqa: PLR2004
    assert slack_client.chat_update.call_args.kwargs["text"] == ":white_check_mark: *Approved · group TestGroup for* <@U_REQ>"
    assert _thread_replies(slack_client)[0].startswith("<@U_REQ> access granted")


def test_group_button_click_reflects_a_grant_failure_and_clears_the_dedup_cache(group_module, slack_client):
    """The failure is shown on the request itself, so @handle_errors logs it without a second, top-level post."""

    def _fail(**_kwargs):  # noqa: ANN202, ANN003
        raise RuntimeError("boom: group not found")

    _click(group_module, slack_client, _payload(group_module), execute=_fail)

    assert group_module.cache_for_dublicate_requests == {}
    final_update_text = slack_client.chat_update.call_args.kwargs["text"]
    assert final_update_text.startswith(":x: *Failed · group TestGroup")
    assert _thread_replies(slack_client) == ["Granting access failed: boom: group not found"]
    assert not any("unexpected error" in (c.kwargs.get("text") or "") for c in slack_client.chat_postMessage.call_args_list)


def test_group_button_click_discard(group_module, slack_client):
    mock_exec = _click(group_module, slack_client, _payload(group_module, entities.ApproverAction.Discard))

    assert slack_client.chat_update.call_args.kwargs["text"].startswith(":wastebasket: *Discarded · group TestGroup")
    assert _thread_replies(slack_client) == []
    mock_exec.assert_not_called()


def test_group_button_click_discard_that_fails_to_show_tells_the_approver_not_the_requester(group_module, slack_client):
    """If the message still shows its buttons, the request is not discarded."""
    slack_client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")
    slack_client.conversations_members.return_value = MagicMock(data={"members": []})

    _click(group_module, slack_client, _payload(group_module, entities.ApproverAction.Discard))

    assert _thread_replies(slack_client) == ["<@U_APP1> the discard did not go through, please try again."]
    assert not any(c.kwargs["channel"] == REQUESTER.id for c in slack_client.chat_postMessage.call_args_list)
