"""SnapStart: a snapshot holds no approval config or Slack secrets, and the after-restore hook loads
them into the objects every module already holds, failing closed when it can't."""

import hashlib
import hmac
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
from slack_bolt import BoltRequest

import access_control
import config
import entities

# Every module that binds `cfg = config.get_config()` at import, main first.
CFG_HOLDERS = ("main", "group", "slack_helpers", "errors", "schedule", "access_control")
GROUP_ID = "11111111-2222-3333-4444-555555555555"  # conftest's group statement, approver email@domen.com
AUTH_TEST = {"ok": True, "team_id": "T1", "user_id": "UBOT", "bot_id": "B1", "team": "t", "user": "bot", "url": "https://x.slack.com/"}
RESTORED_S3_CONFIG = {
    "statements": [],
    "group_statements": [{"Resource": [GROUP_ID], "Approvers": ["other@domen.com"]}],
}


@pytest.fixture
def snap_start(monkeypatch):
    """Import main as SnapStart's snapshot init does, with every S3, SSM and Slack read failing the test."""
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "snap-start")
    monkeypatch.setenv("CONFIG_S3_KEY", "config/approval-config.json")
    monkeypatch.setattr(config, "_config", None)
    aws_client = MagicMock()
    aws_client.get_parameter.side_effect = AssertionError("SSM read during snapshot init")
    hooks = []
    with (
        patch.dict("sys.modules"),
        patch("boto3.Session") as mock_session,
        patch("boto3._get_default_session") as mock_default_session,
        patch("boto3.client", return_value=aws_client),
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
        patch.object(config, "load_approval_config_from_s3", side_effect=AssertionError("S3 read during snapshot init")) as load_s3,
        patch("slack_sdk.WebClient.auth_test", side_effect=AssertionError("Slack call during snapshot init")) as auth_test,
        patch("snapshot_restore_py.register_after_restore", side_effect=hooks.append),
    ):
        for name in CFG_HOLDERS:
            sys.modules.pop(name, None)
        mock_session.return_value.client.return_value = aws_client
        mock_default_session.return_value.client.return_value = aws_client
        import main

        yield SimpleNamespace(main=main, ssm=aws_client, load_s3=load_s3, auth_test=auth_test, hooks=hooks)


def _arm(env: SimpleNamespace) -> None:
    """Let S3, SSM and Slack answer, as they do once the snapshot is restored."""
    values = {"/test/slack-bot-token": "xoxb-restored", "/test/slack-signing-secret": "secret-restored"}
    env.ssm.get_parameter.side_effect = lambda Name, WithDecryption: {"Parameter": {"Value": values[Name]}}  # noqa: ARG005, N803
    env.load_s3.side_effect = None
    env.load_s3.return_value = RESTORED_S3_CONFIG
    env.auth_test.side_effect = None
    env.auth_test.return_value = MagicMock(data=AUTH_TEST)


def _restore(env: SimpleNamespace) -> None:
    """Run the registered after-restore hook as Lambda would."""
    _arm(env)
    (hook,) = env.hooks
    hook()


def _group_approval_permitted(group_module) -> bool:
    return access_control.make_decision_on_approve_request(
        action=entities.ApproverAction.Approve,
        statements=group_module.cfg.group_statements,
        group_id=GROUP_ID,
        approver_email="email@domen.com",
        requester_email="requester@domen.com",
    ).permit


def _signed_shortcut(signing_secret: str) -> BoltRequest:
    body = urlencode(
        {"payload": json.dumps({"type": "shortcut", "callback_id": "test_shortcut", "team": {"id": "T1"}, "user": {"id": "U1"}})}
    )
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(signing_secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    headers = {
        "content-type": ["application/x-www-form-urlencoded"],
        "x-slack-request-timestamp": [timestamp],
        "x-slack-signature": [signature],
    }
    return BoltRequest(body=body, headers=headers)


def test_snapshot_init_reads_no_s3_ssm_or_slack(snap_start):
    main = snap_start.main
    snap_start.load_s3.assert_not_called()
    snap_start.ssm.get_parameter.assert_not_called()
    snap_start.auth_test.assert_not_called()
    assert snap_start.hooks == [main.restore_after_snapshot]
    assert main.slack_auth == main.SlackAuth()
    assert main.cfg.config_s3_key == ""


def test_restore_hook_loads_config_secrets_and_bot_identity_into_shared_objects(snap_start):
    main = snap_start.main
    snapshot_cfg = main.cfg
    _restore(snap_start)

    assert main.cfg is snapshot_cfg is config.get_config()
    for name in CFG_HOLDERS:
        assert sys.modules[name].cfg is main.cfg, name
    assert main.cfg.config_s3_key == "config/approval-config.json"
    assert main.slack_auth == main.SlackAuth(bot_token="xoxb-restored", signing_secret="secret-restored", auth_test=AUTH_TEST)
    assert main.app.client.token == "xoxb-restored"
    result = main.authorize_with_current_token(enterprise_id=None, team_id="T1", logger=None)
    assert (result.bot_token, result.bot_user_id) == ("xoxb-restored", "UBOT")


def test_group_approval_permitted_at_snapshot_time_is_denied_after_rule_change(snap_start):
    group = sys.modules["group"]
    assert _group_approval_permitted(group)  # the rules the snapshot was taken with
    _restore(snap_start)  # the S3 config now names a different approver
    assert not _group_approval_permitted(group)


@pytest.mark.parametrize(
    ("failing", "error"),
    [("load_s3", RuntimeError("NoSuchKey")), ("ssm", RuntimeError("AccessDenied")), ("auth_test", RuntimeError("invalid_auth"))],
)
def test_restore_hook_fails_closed(snap_start, failing, error):
    main = snap_start.main
    target = snap_start.ssm.get_parameter if failing == "ssm" else getattr(snap_start, failing)
    _arm(snap_start)
    target.side_effect = error

    with pytest.raises(RuntimeError):
        snap_start.hooks[0]()

    # Lambda fails the restore on the exception; nothing usable is left behind either way.
    assert main.authorize_with_current_token(enterprise_id=None, team_id="T1", logger=None) is None
    assert main.app.dispatch(_signed_shortcut("secret-restored")).status == 401


def test_bolt_uses_secrets_loaded_after_restore(snap_start):
    main = snap_start.main
    seen = []

    @main.app.shortcut("test_shortcut")
    def _shortcut(ack, client, context):
        seen.append((client.token, context.bot_user_id))
        ack()

    # Before the hook: the snapshot holds no signing secret, so nothing passes verification.
    assert main.app.dispatch(_signed_shortcut("")).status == 401

    _restore(snap_start)
    assert main.app.dispatch(_signed_shortcut("secret-stale")).status == 401
    assert main.app.dispatch(_signed_shortcut("secret-restored")).status == 200
    assert seen == [("xoxb-restored", "UBOT")]


def test_ssl_check_needs_no_signature(snap_start):
    request = BoltRequest(body=urlencode({"ssl_check": "1", "token": "x"}), headers={"content-type": ["application/x-www-form-urlencoded"]})
    assert snap_start.main.app.dispatch(request).status == 200
