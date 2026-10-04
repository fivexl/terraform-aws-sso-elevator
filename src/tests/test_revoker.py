"""Revoker tests: per-invocation Slack token read, and loop paths surviving Slack failures."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import config
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
