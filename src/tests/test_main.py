"""Tests for the CLI access-request path in main.py: lambda_handler's dispatch
fork and handle_cli_access_request's own branches. Everything past identity
verification and requester resolution is exercised already by
process_access_request's existing callers (see test_access_control.py /
test_group.py) — these tests only cover what's new here.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def main_module():
    """Import main with all module-level side effects (Bolt's App() token
    validation, the SSO instance lookup transitively triggered by importing
    group, and boto3 client construction) mocked out — same technique
    test_group.py's group_module fixture uses for the same underlying problem.

    cli_auth's own module-level iam client goes through this same patched
    boto3.Session, so its get_role() is stubbed here too, returning a role
    at IAM Identity Center's real reserved path — otherwise every "valid
    SSO session" test below would get rejected by cli_auth's own path check
    (a MagicMock().Path never equals the real path string). Likewise, the
    shared client's list_users paginator is stubbed to resolve every test
    ARN's session name ("req@example.com") to a matching Identity Store
    user, since cli_auth.extract_identity now looks the session name up
    rather than trusting it directly."""
    sys.modules.pop("main", None)
    sys.modules.pop("group", None)
    sys.modules.pop("cli_auth", None)

    with (
        patch.dict("sys.modules", {}),
        patch("boto3.Session") as mock_boto3_session,
        patch("boto3._get_default_session") as mock_default_session,
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
        patch("slack_bolt.App") as mock_app_cls,
    ):
        shared_client = MagicMock()
        shared_client.get_role.return_value = {"Role": {"Path": "/aws-reserved/sso.amazonaws.com/", "RoleName": "irrelevant-for-this-test"}}
        shared_client.get_paginator.return_value.paginate.return_value = [
            {"Users": [{"UserName": "req@example.com", "Emails": [{"Value": "req@example.com", "Primary": True}]}]}
        ]
        mock_boto3_session.return_value.client.return_value = shared_client
        mock_default_session.return_value.client.return_value = shared_client
        mock_app_cls.return_value = MagicMock()
        import main

        yield main

    sys.modules.pop("main", None)
    sys.modules.pop("group", None)


def _cli_request_event(body: dict | None = None, user_arn: str | None = None) -> dict:
    """An event shaped like what handle_cli_access_request itself consumes.
    routeKey/rawPath aren't relevant here, since these tests call
    handle_cli_access_request directly rather than going through
    lambda_handler's dispatch (that's covered separately, below)."""
    event = {
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {},
    }
    if user_arn is not None:
        event["requestContext"]["authorizer"] = {"iam": {"userArn": user_arn}}
    return event


# ---------------------------------------------------------------------------
# lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_routes_cli_route_key_to_cli_handler(main_module):
    event = {"routeKey": main_module.CLI_ACCESS_REQUEST_ROUTE_KEY, "rawPath": "/access-requester-cli"}
    with patch.object(main_module, "handle_cli_access_request", return_value={"statusCode": 200}) as mock_handle:
        result = main_module.lambda_handler(event, MagicMock())
    mock_handle.assert_called_once_with(event)
    assert result == {"statusCode": 200}


def test_lambda_handler_dispatch_is_robust_to_stage_name_prefix_in_rawpath(main_module):
    """Regression test for the actual bug this dispatch mechanism was chosen to
    avoid: whether rawPath includes a stage-name prefix depends on whether the
    stage is $default or a named stage, so dispatch must key off routeKey (which
    is always exactly "{METHOD} {path}" regardless of stage), not rawPath."""
    event = {
        "routeKey": main_module.CLI_ACCESS_REQUEST_ROUTE_KEY,
        "rawPath": "/some-stage-name/access-requester-cli",
        "requestContext": {"http": {"path": "/some-stage-name/access-requester-cli"}},
    }
    with patch.object(main_module, "handle_cli_access_request", return_value={"statusCode": 200}) as mock_handle:
        result = main_module.lambda_handler(event, MagicMock())
    mock_handle.assert_called_once_with(event)
    assert result == {"statusCode": 200}


def test_lambda_handler_routes_other_route_keys_to_bolt(main_module):
    event = {"routeKey": "POST /access-requester", "rawPath": "/access-requester"}
    context = MagicMock()
    with patch.object(main_module, "SlackRequestHandler") as mock_handler_cls:
        mock_handler_cls.return_value.handle.return_value = {"statusCode": 200}
        result = main_module.lambda_handler(event, context)
    mock_handler_cls.return_value.handle.assert_called_once_with(event, context)
    assert result == {"statusCode": 200}


# ---------------------------------------------------------------------------
# handle_cli_access_request
# ---------------------------------------------------------------------------


def test_handle_cli_access_request_rejects_missing_authorizer_context(main_module):
    event = _cli_request_event(body={"account": "111111111111"})
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_explicit_null_authorizer(main_module):
    """Regression test: a JSON key present with an explicit null value is
    not the same as a missing key -- `{}.get("authorizer", {})` only
    applies its default when the key is absent, so "authorizer": null
    used to reach .get("iam") on None and raise, turning this into a 500
    with a Slack post instead of the same clean 403 a missing key gets."""
    event = _cli_request_event(body={"account": "111111111111"})
    event["requestContext"]["authorizer"] = None
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_untrusted_arn(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "2"},
        user_arn="arn:aws:iam::111111111111:user/not-an-sso-session",
    )
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_invalid_json_body(main_module):
    event = _cli_request_event(user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    event["body"] = "{not json"
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_malformed_account_id(main_module):
    """Unlike the Slack modal (a populated select of real accounts), the
    CLI's JSON body has no format constraint on account -- a malformed
    value used to reach organizations.describe_account() well after the
    decision was made, unwinding to the generic 500 handler and posting to
    the approvals channel for what's just bad input."""
    event = _cli_request_event(
        body={"account": "not-an-account-id", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_account_outside_configured_statements(main_module):
    """With a real (non-wildcard) set of configured accounts, a
    well-formatted but unlisted account ID must still be rejected."""
    event = _cli_request_event(
        body={"account": "999999999999", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    restricted_cfg = main_module.cfg.model_copy(update={"accounts": frozenset(["111111111111"])})
    with patch.object(main_module, "cfg", restricted_cfg):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_accepts_wildcard_configured_account(main_module):
    """cfg.accounts can itself literally be {"*"} (a statement configured
    for any account) -- membership must treat that as "anything goes",
    matching access_control's own wildcard statement matching, not require
    an exact (impossible) match against the literal "*"."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    assert main_module.cfg.accounts == {"*"}  # sanity check on the fixture's own config
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request"),
    ):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 200


def test_handle_cli_access_request_rejects_permission_set_outside_configured_statements(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "NotConfigured", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    restricted_cfg = main_module.cfg.model_copy(update={"permission_sets": frozenset(["FullOrgAdmin"])})
    with patch.object(main_module, "cfg", restricted_cfg):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_non_positive_duration(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "0"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_duration_outside_the_configured_options(main_module):
    """Regression test for the actual bypass: a deployment can restrict
    everyone to an explicit short list of durations via
    permission_duration_list_override, in which case
    max_permissions_duration_time is documented as ignored. The CLI must
    honor that same list, not just its own looser upper bound."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "24"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    restricted_options = [
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="00:30"), value="00:30"),
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="01:00"), value="01:00"),
    ]
    with patch.object(main_module.slack_helpers, "get_max_duration_block", return_value=restricted_options):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 400


def test_handle_cli_access_request_accepts_duration_matching_a_configured_option(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    restricted_options = [
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="00:30"), value="00:30"),
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="01:00"), value="01:00"),
    ]
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module.slack_helpers, "get_max_duration_block", return_value=restricted_options),
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request"),
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 200


def test_handle_cli_access_request_success_calls_process_access_request(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester) as mock_get_user,
        patch.object(main_module, "process_access_request") as mock_process,
    ):
        result = main_module.handle_cli_access_request(event)

    mock_get_user.assert_called_once_with(main_module.app.client, "req@example.com")
    mock_process.assert_called_once()
    called_kwargs = mock_process.call_args.kwargs
    assert called_kwargs["request"].account_id == "111111111111"
    assert called_kwargs["request"].permission_set_name == "FullOrgAdmin"
    assert called_kwargs["request"].reason == "debugging"
    assert called_kwargs["request"].requester_slack_id == "U_REQ"
    assert called_kwargs["requester"] is fake_requester
    assert result["statusCode"] == 200


def test_handle_cli_access_request_rejects_verified_identity_with_no_slack_account(main_module):
    """A verified SSO identity with no matching Slack user is an expected
    outcome (someone in Identity Center but not in this Slack workspace),
    not a server bug -- it must not page the approvals channel, and it
    must return the exact same response as an unverified identity, so a
    caller can't use the difference to enumerate which emails have a
    Slack account here."""
    import slack_sdk.errors

    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", side_effect=slack_sdk.errors.SlackApiError("users_not_found", None)),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result == main_module.cli_auth.GENERIC_REJECTION
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_reports_unexpected_errors(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with patch.object(main_module.slack_helpers, "get_user_by_email", side_effect=RuntimeError("boom")):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 500


def test_handle_cli_access_request_returns_503_on_transient_iam_error(main_module):
    """A throttled/unavailable iam:GetRole says nothing about whether the
    caller's identity is valid -- it must not be reported as
    GENERIC_REJECTION's "your credentials are invalid" (403), nor page the
    approvals channel as an unexpected error (500). A distinguishable 503
    lets the CLI tell "retry this" apart from both of those."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(main_module.cli_auth, "extract_identity", side_effect=main_module.cli_auth.TransientIAMError),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 503
    mock_post_message.assert_not_called()
