"""Tests for group.handle_request_for_group_access_submittion.

Focuses on the RequiresApproval branch and its three sub-paths:
1. All approvers found in Slack
2. Some approvers missing from Slack
3. No approvers found in Slack
"""

import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import access_control
import entities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(id: str, email: str, name: str = "Test User") -> entities.slack.User:
    return entities.slack.User(id=id, email=email, real_name=name)


REQUESTER = _make_user("U_REQ", "requester@example.com", "Requester")
APPROVER_1 = _make_user("U_APP1", "approver1@example.com", "Approver One")
APPROVER_2 = _make_user("U_APP2", "approver2@example.com", "Approver Two")

GROUP = entities.aws.SSOGroup(
    name="TestGroup",
    id="g-1234",
    description="test",
    identity_store_id="d-1234",
)

FAKE_BLOCKS = [{"type": "section", "block_id": "header", "text": {"type": "mrkdwn", "text": "header"}}]


def _decision(reason: access_control.DecisionReason, grant: bool = False, approvers=frozenset()):
    return access_control.AccessRequestDecision(
        grant=grant,
        reason=reason,
        based_on_statements=frozenset(),
        approvers=approvers,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage.return_value = {
        "ts": "1234567890.123456",
        "message": {"blocks": list(FAKE_BLOCKS)},
    }
    client.chat_update.return_value = {"ok": True}
    return client


@pytest.fixture
def group_module():
    """Import group module with all module-level side effects mocked out."""
    # Remove cached module so we can re-import with mocks
    sys.modules.pop("group", None)

    mock_sso = MagicMock()
    mock_sso.describe_sso_instance.return_value = MagicMock(identity_store_id="d-1234")

    with (
        patch.dict("sys.modules", {}),
        patch("boto3._get_default_session") as mock_session,
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
    ):
        mock_session.return_value.client.return_value = MagicMock()
        import group

        yield group

    # Cleanup
    sys.modules.pop("group", None)


# ---------------------------------------------------------------------------
# Tests for RequiresApproval — all approvers found
# ---------------------------------------------------------------------------


def test_requires_approval_all_approvers_found(group_module, slack_client):
    decision = _decision(
        access_control.DecisionReason.RequiresApproval,
        grant=False,
        approvers=frozenset(["approver1@example.com", "approver2@example.com"]),
    )

    with (
        patch.object(group_module, "slack_helpers") as mock_sh,
        patch.object(group_module, "access_control") as mock_ac,
        patch.object(group_module, "sso") as mock_sso,
        patch.object(group_module, "schedule"),
        patch.object(group_module, "cfg") as mock_cfg,
    ):
        mock_cfg.slack_channel_id = "C_CHAN"
        mock_cfg.good_result_emoji = ":white_check_mark:"
        mock_cfg.bad_result_emoji = ":x:"
        mock_cfg.waiting_result_emoji = ":hourglass:"
        mock_cfg.send_dm_if_user_not_in_channel = False
        mock_cfg.approver_renotification_initial_wait_time = 15
        mock_cfg.group_statements = frozenset()

        mock_sh.RequestForGroupAccessView.parse.return_value = MagicMock(
            requester_slack_id="U_REQ",
            group_id="g-1234",
            reason="need access",
            permission_duration=timedelta(hours=1),
        )
        mock_sh.get_user.return_value = REQUESTER
        mock_sh.find_approvers_in_slack.return_value = ([APPROVER_1, APPROVER_2], [])
        mock_sh.build_approval_request_message_blocks.return_value = FAKE_BLOCKS
        mock_sh.HeaderSectionBlock.set_color_coding.return_value = FAKE_BLOCKS
        mock_sh.check_if_user_is_in_channel.return_value = True

        mock_sso.describe_group.return_value = GROUP

        mock_ac.make_decision_on_access_request.return_value = decision
        mock_ac.DecisionReason = access_control.DecisionReason

        group_module.handle_request_for_group_access_submittion(
            body={},
            ack=MagicMock(),
            client=slack_client,
            context=MagicMock(),
        )

        # find_approvers_in_slack was called instead of get_user_by_email
        mock_sh.find_approvers_in_slack.assert_called_once_with(slack_client, decision.approvers)

        # Channel thread message mentions approvers and says "waiting for the approval"
        channel_calls = slack_client.chat_postMessage.call_args_list
        thread_msg = next(c for c in channel_calls if c.kwargs.get("thread_ts"))
        assert "waiting for the approval" in thread_msg.kwargs["text"]
        assert "<@U_APP1>" in thread_msg.kwargs["text"] or "<@U_APP2>" in thread_msg.kwargs["text"]

        # Color coding uses waiting emoji
        color_arg = mock_sh.HeaderSectionBlock.set_color_coding.call_args
        assert color_arg.kwargs["color_coding_emoji"] == ":hourglass:"


# ---------------------------------------------------------------------------
# Tests for RequiresApproval — some approvers missing
# ---------------------------------------------------------------------------


def test_requires_approval_some_approvers_missing(group_module, slack_client):
    decision = _decision(
        access_control.DecisionReason.RequiresApproval,
        grant=False,
        approvers=frozenset(["approver1@example.com", "gone@example.com"]),
    )

    with (
        patch.object(group_module, "slack_helpers") as mock_sh,
        patch.object(group_module, "access_control") as mock_ac,
        patch.object(group_module, "sso") as mock_sso,
        patch.object(group_module, "schedule"),
        patch.object(group_module, "cfg") as mock_cfg,
    ):
        mock_cfg.slack_channel_id = "C_CHAN"
        mock_cfg.good_result_emoji = ":white_check_mark:"
        mock_cfg.bad_result_emoji = ":x:"
        mock_cfg.waiting_result_emoji = ":hourglass:"
        mock_cfg.send_dm_if_user_not_in_channel = False
        mock_cfg.approver_renotification_initial_wait_time = 15
        mock_cfg.group_statements = frozenset()

        mock_sh.RequestForGroupAccessView.parse.return_value = MagicMock(
            requester_slack_id="U_REQ",
            group_id="g-1234",
            reason="need access",
            permission_duration=timedelta(hours=1),
        )
        mock_sh.get_user.return_value = REQUESTER
        mock_sh.find_approvers_in_slack.return_value = ([APPROVER_1], ["gone@example.com"])
        mock_sh.build_approval_request_message_blocks.return_value = FAKE_BLOCKS
        mock_sh.HeaderSectionBlock.set_color_coding.return_value = FAKE_BLOCKS
        mock_sh.check_if_user_is_in_channel.return_value = True

        mock_sso.describe_group.return_value = GROUP

        mock_ac.make_decision_on_access_request.return_value = decision
        mock_ac.DecisionReason = access_control.DecisionReason

        group_module.handle_request_for_group_access_submittion(
            body={},
            ack=MagicMock(),
            client=slack_client,
            context=MagicMock(),
        )

        # Thread message mentions found approver AND notes the missing one
        channel_calls = slack_client.chat_postMessage.call_args_list
        thread_msg = next(c for c in channel_calls if c.kwargs.get("thread_ts"))
        assert "<@U_APP1>" in thread_msg.kwargs["text"]
        assert "gone@example.com" in thread_msg.kwargs["text"]
        assert "could not be found in Slack" in thread_msg.kwargs["text"]

        # Still uses waiting emoji since some approvers were found
        color_arg = mock_sh.HeaderSectionBlock.set_color_coding.call_args
        assert color_arg.kwargs["color_coding_emoji"] == ":hourglass:"


# ---------------------------------------------------------------------------
# Tests for RequiresApproval — no approvers found
# ---------------------------------------------------------------------------


def test_requires_approval_no_approvers_found(group_module, slack_client):
    decision = _decision(
        access_control.DecisionReason.RequiresApproval,
        grant=False,
        approvers=frozenset(["gone@example.com"]),
    )

    with (
        patch.object(group_module, "slack_helpers") as mock_sh,
        patch.object(group_module, "access_control") as mock_ac,
        patch.object(group_module, "sso") as mock_sso,
        patch.object(group_module, "schedule"),
        patch.object(group_module, "cfg") as mock_cfg,
    ):
        mock_cfg.slack_channel_id = "C_CHAN"
        mock_cfg.good_result_emoji = ":white_check_mark:"
        mock_cfg.bad_result_emoji = ":x:"
        mock_cfg.waiting_result_emoji = ":hourglass:"
        mock_cfg.send_dm_if_user_not_in_channel = False
        mock_cfg.approver_renotification_initial_wait_time = 15
        mock_cfg.group_statements = frozenset()

        mock_sh.RequestForGroupAccessView.parse.return_value = MagicMock(
            requester_slack_id="U_REQ",
            group_id="g-1234",
            reason="need access",
            permission_duration=timedelta(hours=1),
        )
        mock_sh.get_user.return_value = REQUESTER
        mock_sh.find_approvers_in_slack.return_value = ([], ["gone@example.com"])
        mock_sh.build_approval_request_message_blocks.return_value = FAKE_BLOCKS
        mock_sh.HeaderSectionBlock.set_color_coding.return_value = FAKE_BLOCKS
        mock_sh.check_if_user_is_in_channel.return_value = True

        mock_sso.describe_group.return_value = GROUP

        mock_ac.make_decision_on_access_request.return_value = decision
        mock_ac.DecisionReason = access_control.DecisionReason

        group_module.handle_request_for_group_access_submittion(
            body={},
            ack=MagicMock(),
            client=slack_client,
            context=MagicMock(),
        )

        # Thread message says none of the approvers could be found
        channel_calls = slack_client.chat_postMessage.call_args_list
        thread_msg = next(c for c in channel_calls if c.kwargs.get("thread_ts"))
        assert "None of the approvers" in thread_msg.kwargs["text"]

        # Uses bad_result_emoji
        color_arg = mock_sh.HeaderSectionBlock.set_color_coding.call_args
        assert color_arg.kwargs["color_coding_emoji"] == ":x:"
