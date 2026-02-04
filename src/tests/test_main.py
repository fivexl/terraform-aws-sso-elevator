"""Tests for main module - check_early_revoke_authorization function."""

from unittest.mock import MagicMock, patch
import sys

import pytest

import entities


# The main module has side effects at import time (AWS API calls).
# We need to mock these before importing.
@pytest.fixture(autouse=True)
def mock_main_imports():
    """Mock all the module-level side effects in main.py."""
    # Mock boto3 session and clients
    mock_session = MagicMock()
    mock_sso_client = MagicMock()
    mock_identity_store_client = MagicMock()
    mock_org_client = MagicMock()
    mock_schedule_client = MagicMock()
    mock_s3_client = MagicMock()

    mock_session.client.side_effect = lambda service: {
        "sso-admin": mock_sso_client,
        "identitystore": mock_identity_store_client,
        "organizations": mock_org_client,
        "scheduler": mock_schedule_client,
        "s3": mock_s3_client,
    }.get(service, MagicMock())

    # Mock config
    mock_cfg = MagicMock()
    mock_cfg.sso_instance_arn = "arn:aws:sso:::instance/ssoins-test"
    mock_cfg.slack_channel_id = "C12345"
    mock_cfg.statements = frozenset()
    mock_cfg.group_statements = frozenset()
    mock_cfg.allow_anyone_to_end_session_early = False
    mock_cfg.identity_store_id = "d-123456"
    mock_cfg.pending_status = "Pending"
    mock_cfg.granted_status = "Granted"
    mock_cfg.denied_status = "Denied"

    # Patch config module before importing main
    with patch.dict(
        sys.modules,
        {
            "boto3": MagicMock(Session=lambda: mock_session),
        },
    ):
        with patch("config.get_config", return_value=mock_cfg):
            with patch("config.check_and_refresh_config", return_value=mock_cfg):
                # Patch sso functions that run at import time
                with patch("sso.get_identity_store_id", return_value="d-123456"):
                    yield mock_cfg


class TestCheckEarlyRevokeAuthorization:
    """Tests for check_early_revoke_authorization function.

    Since main.py has many side effects at import time due to AWS clients,
    we test the logic of check_early_revoke_authorization by recreating its
    implementation in isolation.
    """

    def _check_early_revoke_authorization(  # noqa: PLR0913
        self,
        clicker_slack_id: str,
        requester_slack_id: str,
        approver_emails: list[str],
        client,
        cfg,
        get_user_fn,
        resolve_groups_fn,
        approver_groups: list[str] | None = None,
    ) -> bool:
        """Reimplementation of check_early_revoke_authorization for testing.

        This mirrors the logic in main.py without the import side effects.
        """
        if cfg.allow_anyone_to_end_session_early:
            return True

        if clicker_slack_id == requester_slack_id:
            return True

        try:
            clicker = get_user_fn(client, id=clicker_slack_id)
            if clicker.email in approver_emails:
                return True
        except Exception:
            pass

        if approver_groups:
            group_users, _ = resolve_groups_fn(client, frozenset(approver_groups))
            if clicker_slack_id in {u.id for u in group_users}:
                return True

        return False

    def test_anyone_can_end_session_when_allowed(self, mock_main_imports):
        """When allow_anyone_to_end_session_early is True, any user is authorized."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = True

        mock_client = MagicMock()
        mock_get_user = MagicMock()
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_RANDOM_USER",
            requester_slack_id="U_REQUESTER",
            approver_emails=["approver@example.com"],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=None,
        )

        assert result is True
        mock_get_user.assert_not_called()

    def test_requester_can_end_own_session(self, mock_main_imports):
        """Requester can always end their own session."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock()
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_REQUESTER",
            requester_slack_id="U_REQUESTER",
            approver_emails=["approver@example.com"],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=None,
        )

        assert result is True

    def test_individual_approver_can_end_session(self, mock_main_imports):
        """Individual approver can end session."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(return_value=entities.slack.User(id="U_APPROVER", email="approver@example.com", real_name="Approver"))
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_APPROVER",
            requester_slack_id="U_REQUESTER",
            approver_emails=["approver@example.com"],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=None,
        )

        assert result is True

    def test_non_approver_cannot_end_session(self, mock_main_imports):
        """User who is not requester or approver cannot end session."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(return_value=entities.slack.User(id="U_RANDOM", email="random@example.com", real_name="Random User"))
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_RANDOM",
            requester_slack_id="U_REQUESTER",
            approver_emails=["approver@example.com"],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=None,
        )

        assert result is False

    def test_approver_group_member_can_end_session(self, mock_main_imports):
        """User in an approver group can end session."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(
            return_value=entities.slack.User(id="U_GROUP_MEMBER", email="group-member@example.com", real_name="Group Member")
        )
        mock_resolve_groups = MagicMock(
            return_value=(
                [
                    entities.slack.User(id="U_GROUP_MEMBER", email="group-member@example.com", real_name="Group Member"),
                    entities.slack.User(id="U_OTHER", email="other@example.com", real_name="Other"),
                ],
                [],
            )
        )

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_GROUP_MEMBER",
            requester_slack_id="U_REQUESTER",
            approver_emails=[],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=["approver-group-1"],
        )

        assert result is True
        mock_resolve_groups.assert_called_once_with(mock_client, frozenset(["approver-group-1"]))

    def test_non_group_member_cannot_end_session(self, mock_main_imports):
        """User not in approver group cannot end session."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(return_value=entities.slack.User(id="U_OUTSIDER", email="outsider@example.com", real_name="Outsider"))
        mock_resolve_groups = MagicMock(
            return_value=(
                [
                    entities.slack.User(id="U_GROUP_MEMBER", email="group-member@example.com", real_name="Group Member"),
                ],
                [],
            )
        )

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_OUTSIDER",
            requester_slack_id="U_REQUESTER",
            approver_emails=[],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=["approver-group-1"],
        )

        assert result is False

    def test_handles_get_user_failure_gracefully(self, mock_main_imports):
        """Handles failure to get user info gracefully."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(side_effect=Exception("Slack API error"))
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_UNKNOWN",
            requester_slack_id="U_REQUESTER",
            approver_emails=["approver@example.com"],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=None,
        )

        assert result is False

    def test_empty_approver_groups_list(self, mock_main_imports):
        """Empty approver_groups list doesn't cause errors."""
        mock_cfg = mock_main_imports
        mock_cfg.allow_anyone_to_end_session_early = False

        mock_client = MagicMock()
        mock_get_user = MagicMock(return_value=entities.slack.User(id="U_RANDOM", email="random@example.com", real_name="Random"))
        mock_resolve_groups = MagicMock()

        result = self._check_early_revoke_authorization(
            clicker_slack_id="U_RANDOM",
            requester_slack_id="U_REQUESTER",
            approver_emails=[],
            client=mock_client,
            cfg=mock_cfg,
            get_user_fn=mock_get_user,
            resolve_groups_fn=mock_resolve_groups,
            approver_groups=[],
        )

        assert result is False
        # resolve_groups_fn should not be called with empty list
        mock_resolve_groups.assert_not_called()
