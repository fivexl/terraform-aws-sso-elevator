"""Tests for the CLI access-request path in main.py: lambda_handler's dispatch
fork and handle_cli_access_request's own branches, plus process_access_request
itself (shared by both the CLI and Slack modal paths -- test_access_control.py
and test_group.py test execute_decision/execute_decision_on_group_request
directly, not process_access_request, so its own message-ordering/return-value
behavior needed covering here).
"""

import json
import secrets
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import botocore.exceptions
import pytest

from .proof_helpers import SESSION_TOKEN, SIGNATURE, make_body, proof_query

# STS identity per proof nonce: _cli_request_event registers it, the fixture's fake STS returns it.
_ARN_BY_NONCE: dict[str, str] = {}
# The unpatched cli_proof.fetch_caller_identity, saved by main_module for tests that run the real one.
_REAL_FETCH_CALLER_IDENTITY: list = []
CLI_API_ID = "test-api-id"  # conftest's cli_expected_api_id
CLI_REGION = "eu-central-1"

# Stand-ins for the deployment's real (Organizations/Identity Center) account
# and permission-set catalog, used to fake organizations.get_accounts_from_config_with_cache
# / sso.get_permission_sets_from_config_with_cache below without hitting AWS.
# Every account/permission-set name the tests below expect to be *accepted*
# must be listed here.
_REAL_ACCOUNTS = [SimpleNamespace(id="111111111111", name="acct-111111111111")]
_REAL_PERMISSION_SETS = [
    SimpleNamespace(name="Foo", arn="arn:aws:sso:::permissionSet/ssoins-1/ps-foo"),
    SimpleNamespace(name="FullOrgAdmin", arn="arn:aws:sso:::permissionSet/ssoins-1/ps-fullorgadmin"),
]


def _fake_accounts_from_config(_org_client, _s3_client, cfg):
    """Mirrors organizations.get_accounts_from_config_with_cache's own
    wildcard-vs-filter contract (see organizations.py), against the fixed
    catalog above instead of a real Organizations account list."""
    if "*" in cfg.accounts:
        return _REAL_ACCOUNTS
    return [a for a in _REAL_ACCOUNTS if a.id in cfg.accounts]


def _fake_permission_sets_from_config(_sso_client, _s3_client, cfg):
    """Mirrors sso.get_permission_sets_from_config_with_cache's own
    wildcard-vs-filter contract, against the fixed catalog above."""
    if "*" in cfg.permission_sets:
        return _REAL_PERMISSION_SETS
    return [p for p in _REAL_PERMISSION_SETS if p.name in cfg.permission_sets]


@pytest.fixture
def main_module():
    """Import main with module-level side effects mocked (Bolt token check, SSO instance lookup,
    boto3 clients). list_users resolves every test session name to user "u-req"
    (req@example.com); the account/permission-set catalogs come from the fakes above."""
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

        def _paginator_for(operation_name, **_kwargs):
            paginator = MagicMock()
            if operation_name == "list_users":
                paginator.paginate.return_value = [
                    {
                        "Users": [
                            {"UserId": "u-req", "UserName": "req@example.com", "Emails": [{"Value": "req@example.com", "Primary": True}]}
                        ]
                    }
                ]
            else:
                paginator.paginate.return_value = []
            return paginator

        shared_client.get_paginator.side_effect = _paginator_for
        mock_boto3_session.return_value.client.return_value = shared_client
        mock_default_session.return_value.client.return_value = shared_client
        mock_app_cls.return_value = MagicMock()
        import main

        _REAL_FETCH_CALLER_IDENTITY[:] = [main.cli_proof.fetch_caller_identity]
        with (
            patch.dict("os.environ", {"AWS_REGION": CLI_REGION}),
            patch.object(main.cli_proof, "fetch_caller_identity", side_effect=_fake_fetch_caller_identity),
            patch.object(main.organizations, "get_accounts_from_config_with_cache", side_effect=_fake_accounts_from_config),
            patch.object(main.sso, "get_permission_sets_from_config_with_cache", side_effect=_fake_permission_sets_from_config),
            patch.object(main.organizations, "describe_account", side_effect=lambda _client, account_id: _account(main, account_id)),
        ):
            yield main

    sys.modules.pop("main", None)
    sys.modules.pop("group", None)


def _account(main, account_id: str):  # noqa: ANN001, ANN202
    return main.entities.aws.Account(id=account_id, name="aft")


def _fake_fetch_caller_identity(envelope):  # noqa: ANN001, ANN202
    """Stands in for STS: the identity registered for this proof's nonce, else STS refuses it."""
    arn = _ARN_BY_NONCE.get(envelope.headers["x-elevator-nonce"])
    if arn is None:
        raise sys.modules["main"].cli_proof.ProofRejected("STS refused the proof: 403 SignatureDoesNotMatch")
    return sys.modules["main"].cli_proof.CallerIdentity(arn=arn, account=arn.split(":")[4])


def _cli_request_event(body: dict | None = None, user_arn: str | None = None, api_id: str = CLI_API_ID) -> dict:
    """A REST API proxy event for the CLI route carrying a fresh v1 proof envelope for body, addressed
    to api_id. user_arn is the identity the fake STS returns for the proof; None means STS refuses it.
    body=None sends no body at all, as an old CLI with nothing to send would."""
    nonce = secrets.token_hex(16)
    if user_arn is not None:
        _ARN_BY_NONCE[nonce] = user_arn
    return {
        "resource": sys.modules["main"].CLI_ACCESS_REQUEST_PATH,
        "path": sys.modules["main"].CLI_ACCESS_REQUEST_PATH,
        "httpMethod": "POST",
        "headers": {"content-type": "application/json"},
        "requestContext": {"apiId": "forgeable", "httpMethod": "POST", "stage": "default", "identity": {"userArn": "forgeable"}},
        "body": None
        if body is None
        else make_body(body, api_id=api_id, nonce=nonce, query=proof_query(region=CLI_REGION, signed_at=datetime.now(UTC))),
        "isBase64Encoded": False,
    }


def test_slack_app_gets_its_secrets_from_ssm(main_module):
    get_parameter_calls = main_module.ssm_client.get_parameter.call_args_list
    assert call(Name="/test/slack-bot-token", WithDecryption=True) in get_parameter_calls
    assert call(Name="/test/slack-signing-secret", WithDecryption=True) in get_parameter_calls


@pytest.mark.parametrize(
    ("get_parameter_kwargs", "message"),
    [
        ({"side_effect": RuntimeError("AccessDeniedException")}, "Failed to read Slack secret parameter /test/slack-bot-token"),
        ({"return_value": {"Parameter": {"Value": "REPLACE_ME"}}}, "/test/slack-bot-token still holds the placeholder"),
    ],
)
def test_main_refuses_to_start_without_usable_slack_secrets(get_parameter_kwargs, message):
    sys.modules.pop("main", None)
    sys.modules.pop("group", None)
    sys.modules.pop("cli_auth", None)
    shared_client = MagicMock()
    shared_client.get_parameter = MagicMock(**get_parameter_kwargs)
    with (
        patch.dict("sys.modules", {}),
        patch("boto3.Session") as mock_boto3_session,
        patch("boto3._get_default_session") as mock_default_session,
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
        patch("slack_bolt.App") as mock_app_cls,
    ):
        mock_boto3_session.return_value.client.return_value = shared_client
        mock_default_session.return_value.client.return_value = shared_client
        with pytest.raises(RuntimeError, match=message):
            import main  # noqa: F401

    mock_app_cls.assert_not_called()
    sys.modules.pop("main", None)
    sys.modules.pop("group", None)
    sys.modules.pop("cli_auth", None)


# ---------------------------------------------------------------------------
# lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_routes_cli_event_to_cli_handler(main_module):
    """The CLI's REST API proxy event (rest_api.tf) has no routeKey at all --
    it's identified by httpMethod/resource instead."""
    event = {"httpMethod": "POST", "resource": main_module.CLI_ACCESS_REQUEST_PATH, "path": "/default/access-requester-cli"}
    with patch.object(main_module, "handle_cli_access_request", return_value={"statusCode": 200}) as mock_handle:
        result = main_module.lambda_handler(event, MagicMock())
    mock_handle.assert_called_once_with(event)
    assert result == {"statusCode": 200}


def test_lambda_handler_does_not_treat_a_get_on_the_cli_resource_as_a_cli_request(main_module):
    """_is_cli_event checks the method, not just the resource path."""
    event = {"httpMethod": "GET", "resource": main_module.CLI_ACCESS_REQUEST_PATH}
    context = MagicMock()
    with patch.object(main_module, "SlackRequestHandler") as mock_handler_cls:
        mock_handler_cls.return_value.handle.return_value = {"statusCode": 200}
        result = main_module.lambda_handler(event, context)
    mock_handler_cls.return_value.handle.assert_called_once_with(event, context)
    assert result == {"statusCode": 200}


def test_lambda_handler_routes_slack_events_to_bolt(main_module):
    """A Slack event from the same REST API (its own resource) goes to Bolt."""
    event = {
        "resource": "/access-requester",
        "path": "/access-requester",
        "httpMethod": "POST",
        "requestContext": {"httpMethod": "POST", "resourcePath": "/access-requester", "stage": "default"},
        "body": "payload=x",
        "isBase64Encoded": False,
    }
    context = MagicMock()
    with patch.object(main_module, "SlackRequestHandler") as mock_handler_cls:
        mock_handler_cls.return_value.handle.return_value = {"statusCode": 200}
        result = main_module.lambda_handler(event, context)
    mock_handler_cls.return_value.handle.assert_called_once_with(event, context)
    assert result == {"statusCode": 200}


# ---------------------------------------------------------------------------
# handle_cli_access_request
# ---------------------------------------------------------------------------


def test_handle_cli_access_request_rejects_a_proof_for_another_api(main_module):
    """The proof's audience must be this deployment's API id (conftest's cli_expected_api_id)."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
        api_id="some-other-api-id",
    )
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION
    main_module.cli_proof.fetch_caller_identity.assert_not_called()


def test_handle_cli_access_request_rejects_every_event_when_cli_is_disabled(main_module):
    """With the CLI route disabled, cli_expected_api_id is "". A proof addressed to "" must still be
    rejected, before any STS or identity lookup."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
        api_id="",
    )
    disabled_cfg = main_module.cfg.model_copy(update={"cli_expected_api_id": ""})
    with (
        patch.object(main_module, "cfg", disabled_cfg),
        patch.object(main_module.cli_auth, "extract_identity") as mock_extract_identity,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result == main_module.cli_auth.GENERIC_REJECTION
    main_module.cli_proof.fetch_caller_identity.assert_not_called()
    mock_extract_identity.assert_not_called()


def test_handle_cli_access_request_ignores_the_forgeable_request_context(main_module):
    """A direct invoke can forge requestContext.identity.userArn. Only the STS identity counts:
    here STS returns a non-SSO principal, so the forged SSO ARN in requestContext must not help."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/SomeOtherRole/req@example.com",
    )
    event["requestContext"]["identity"]["userArn"] = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com"
    event["requestContext"]["apiId"] = CLI_API_ID
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


@pytest.mark.parametrize(
    "body",
    [
        None,
        json.dumps({"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"}),
        "{not json",
        "[]",
        json.dumps({"version": 2, "payload": "{}", "proof": {}}),
    ],
)
def test_handle_cli_access_request_tells_an_old_cli_to_upgrade(main_module, body):
    event = _cli_request_event(user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    event["body"] = body
    with patch.object(main_module.cli_auth, "extract_identity") as mock_extract_identity:
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400
    assert "Upgrade elevator" in json.loads(result["body"])["message"]
    mock_extract_identity.assert_not_called()


def test_handle_cli_access_request_rejects_a_base64_encoded_body(main_module):
    event = _cli_request_event(body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"})
    event["isBase64Encoded"] = True
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_a_caller_outside_the_organization(main_module):
    """STS vouches for any AWS account; DescribeAccount decides whether it is this organization's."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::999999999999:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    not_found = botocore.exceptions.ClientError({"Error": {"Code": "AccountNotFoundException", "Message": "x"}}, "DescribeAccount")
    with (
        patch.object(main_module.org_client, "describe_account", side_effect=not_found) as mock_describe,
        patch.object(main_module.cli_auth, "extract_identity") as mock_extract_identity,
    ):
        result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION
    mock_describe.assert_called_once_with(AccountId="999999999999")
    mock_extract_identity.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        botocore.exceptions.ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "DescribeAccount"
        ),
        botocore.exceptions.EndpointConnectionError(endpoint_url="https://organizations.us-east-1.amazonaws.com"),
    ],
)
def test_handle_cli_access_request_returns_503_when_organizations_is_unavailable(main_module, error):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module.org_client, "describe_account", side_effect=error):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 503


def test_handle_cli_access_request_returns_503_when_sts_is_unavailable(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    main_module.cli_proof.fetch_caller_identity.side_effect = main_module.cli_proof.ProofUnavailable("STS answered 503 unknown")
    with patch.object(main_module.app.client, "chat_postMessage") as mock_post_message:
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 503
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_accepts_caller_from_a_different_account(main_module):
    """Any account in the organization may request access to another, so a well-formed request
    from a different member account succeeds end to end."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::222222222222:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request", return_value=(MagicMock(), True)) as mock_process,
    ):
        result = main_module.handle_cli_access_request(event)
    mock_process.assert_called_once()
    assert result["statusCode"] == 200  # noqa: PLR2004
    assert json.loads(result["body"])["ok"] is True


def test_handle_cli_access_request_rejects_a_proof_sts_refuses(main_module):
    # A valid body, so the identity check is what rejects it.
    event = _cli_request_event(body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"})
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_untrusted_arn(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "2"},
        user_arn="arn:aws:iam::111111111111:user/not-an-sso-session",
    )
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_succeeds_with_no_prior_account_assignment(main_module):
    """Regression test for issue #193: a genuine elevation request -- the
    requester currently has *no* SSO account assignment at all for the
    account/permission-set being requested -- must reach process_access_request,
    not be rejected. The now-removed has_account_assignment defense-in-depth
    check required the requester to already hold the exact assignment being
    requested, which rejected every real elevation request by construction
    (the entire point of this tool is granting access the caller does not
    currently have) and only ever passed for a redundant re-request of a
    still-live grant SSO Elevator had itself just issued. Reproduced live
    against a real 4.4.0 deployment before this fix: a genuinely
    unprivileged account/permission-set pair came back 403 GENERIC_REJECTION
    every time."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "incident response", "duration": "15"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    fake_decision = SimpleNamespace(reason=main_module.access_control.DecisionReason.RequiresApproval)
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request", return_value=(fake_decision, True)) as mock_process,
    ):
        result = main_module.handle_cli_access_request(event)

    mock_process.assert_called_once()
    assert result != main_module.cli_auth.GENERIC_REJECTION
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["ok"] is True


def test_handle_cli_access_request_returns_503_on_transient_account_catalog_error(main_module):
    """Regression test: get_accounts_from_config_with_cache and
    get_permission_sets_from_config_with_cache are cache-backed, but that
    only shields a *warm* cache -- with caching disabled or a cold cache, a
    throttle propagates the raw botocore error. This must come back as a
    retryable 503, not the blanket handler's 500 plus a Slack post."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    throttled = botocore.exceptions.ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        operation_name="ListAccounts",
    )
    with (
        patch.object(main_module.organizations, "get_accounts_from_config_with_cache", side_effect=throttled),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 503
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_checks_duration_before_the_account_catalog(main_module):
    """Regression test: the account/permission-set catalog lookups
    (organizations:ListAccounts, sso:ListPermissionSets +
    sso:DescribePermissionSet per entry) are the most expensive calls on this
    path, behind a per-route throttle any SSO principal in the org can drive
    at 1 rps sustained -- an invalid duration is the cheapest thing to reject
    and must do so before paying for them, not after."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "not-a-number"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module.organizations, "get_accounts_from_config_with_cache") as mock_get_accounts:
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400
    mock_get_accounts.assert_not_called()


def test_handle_cli_access_request_rejects_email_that_does_not_round_trip_to_the_verified_identity(main_module):
    """Regression test for the email round-trip / UserId threading gap:
    identity_user_id is the UserId this specific, IAM-authenticated session
    was actually verified against (matched by session_name). requester.email
    (Slack's own profile email) is what execute_decision independently
    re-resolves to a UserId later on -- if that second, independent lookup
    ever disagreed with the one actually verified, the grant would go to
    whoever it found instead of the verified caller. Simulated here by
    pointing get_user_by_email at a Slack user whose email isn't in the
    Identity Store at all, so the cross-check's second lookup resolves to
    None while identity_user_id (from the verified session) is "u-req" --
    a clear mismatch, not just an edge case in matching."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="someone-else@example.com")
    with patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester):
        result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_when_requesters_email_collides_in_the_identity_store(main_module):
    """Regression test: find_user_principal_id_by_email_strict raises
    AmbiguousSSOUser (a sibling of SSOUserNotFound, not a subclass -- see
    test_sso.py) on an email collision instead of returning None -- the
    round-trip cross-check above must catch that specific type and treat it
    the same as an outright mismatch (a clean, quiet GENERIC_REJECTION), not
    let it propagate to the blanket exception handler as a 500 plus a Slack
    post the way every other unexpected exception in this function does."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    colliding_list_of_users = {
        "Users": [
            {"UserId": "u-req", "UserName": "req@example.com", "Emails": [{"Value": "collide@example.com", "Primary": True}]},
            {"UserId": "u-other", "UserName": "someone-else", "Emails": [{"Value": "collide@example.com", "Primary": True}]},
        ]
    }
    fake_requester = MagicMock(id="U_REQ", email="collide@example.com")
    with (
        patch.object(main_module.sso, "list_users", return_value=colliding_list_of_users),
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
    ):
        result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"account": "1", "account": "2"}'])
def test_handle_cli_access_request_rejects_a_malformed_payload_without_resolving_identity(main_module, payload):
    """A malformed payload is a 400, rejected before the STS call and the Identity Store scan."""
    event = _cli_request_event(user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    event["body"] = make_body(payload, api_id=CLI_API_ID, query=proof_query(region=CLI_REGION, signed_at=datetime.now(UTC)))
    with patch.object(main_module.cli_auth, "extract_identity") as mock_extract_identity:
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400
    main_module.cli_proof.fetch_caller_identity.assert_not_called()
    mock_extract_identity.assert_not_called()


def test_handle_cli_access_request_rejects_an_empty_payload(main_module):
    event = _cli_request_event(body={}, user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400
    main_module.cli_proof.fetch_caller_identity.assert_not_called()


def test_handle_cli_access_request_rejects_a_reason_over_the_limit(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x" * 1001, "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400  # noqa: PLR2004
    assert json.loads(result["body"]) == {"message": "Reason must be 1000 characters or fewer."}


def test_handle_cli_access_request_rejects_a_request_too_large_for_the_button(main_module):
    """Within the reason cap, but JSON escaping doubles every quote: the button value would pass 2000 characters."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": '"' * 1000, "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=MagicMock(id="U_REQ", email="req@example.com")),
        patch.object(main_module, "process_access_request") as mock_process,
    ):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400  # noqa: PLR2004
    assert json.loads(result["body"]) == {"message": "Request is too large for Slack; shorten the reason."}
    mock_process.assert_not_called()


def test_handle_cli_access_request_rejects_wildcard_account_not_in_the_real_organization(main_module):
    """Regression test for the actual bypass: with cfg.accounts == {"*"},
    checking membership by literal set intersection ({"*", account_id} &
    cfg.accounts) passed for *any* syntactically valid 12-digit ID, even one
    that isn't a real account -- reaching organizations.describe_account()
    downstream and crashing with an unhandled AccountNotFoundException. A
    well-formatted but nonexistent account must be rejected here instead,
    the same way the Slack modal's dropdown could never have offered it."""
    event = _cli_request_event(
        body={"account": "000000000000", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    assert main_module.cfg.accounts == {"*"}  # sanity check on the fixture's own config
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_wildcard_permission_set_not_in_the_real_catalog(main_module):
    """Mirrors test_handle_cli_access_request_rejects_wildcard_account_not_in_the_real_organization
    above, for the permission-set half of the same wildcard fix. That
    account-side test would fail on a revert to the old {"*", value} &
    cfg.<...> intersection check; the permission-set side had no equivalent
    -- reverting main.py's real_permission_sets membership check back to a
    literal set-intersection check left the entire suite green, since every
    other test uses a restricted cfg.permission_sets the old check also
    rejected correctly. A syntactically plausible but non-configured
    permission set name (the modal's dropdown could never have offered it)
    must be rejected here, not reach organizations.describe_account()/
    access_control and surface as an approved-then-failed grant."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "AdministratorAccess-typo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_AdministratorAccess-typo/req@example.com",
    )
    assert main_module.cfg.permission_sets == {"*"}  # sanity check on the fixture's own config
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_gives_identical_responses_for_bad_account_vs_bad_permission_set(main_module):
    """Regression test (#194 B8): the account-not-configured and
    permission_set-not-configured checks used to return distinct message
    text, letting any authenticated SSO caller (this route's AWS_IAM
    authorizer only proves signing capability, not that the signer is one
    this deployment's policy actually intends to allow) walk the account ID
    and permission-set name spaces separately -- confirming each real value
    one field at a time via which of the two messages came back, rather than
    needing a whole matching pair before learning anything. Both failure
    modes must now be indistinguishable."""
    event_bad_account = _cli_request_event(
        body={"account": "000000000000", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    event_bad_permission_set = _cli_request_event(
        body={"account": "111111111111", "permission_set": "made-up-permission-set", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result_bad_account = main_module.handle_cli_access_request(event_bad_account)
    result_bad_permission_set = main_module.handle_cli_access_request(event_bad_permission_set)

    assert result_bad_account["statusCode"] == 400  # noqa: PLR2004
    assert result_bad_permission_set["statusCode"] == 400  # noqa: PLR2004
    assert result_bad_account["body"] == result_bad_permission_set["body"]


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
        patch.object(main_module, "process_access_request", return_value=(MagicMock(), True)),
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


def test_handle_cli_access_request_rejects_float_duration(main_module):
    """Regression test: int(2.7) silently truncates to 2 instead of being
    rejected -- a JSON number (not the string the CLI always sends) should
    be treated as invalid input, not quietly reinterpreted."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": 2.7},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_absurdly_long_duration_string(main_module):
    """Regression test: a several-thousand-digit duration string used to
    pass the old \\d+ regex, then crash int() at CPython's ~4300-digit
    conversion limit -- an uncaught ValueError that unwound to the generic
    exception handler as a 500 plus a Slack post, instead of this clean 400.
    The {1,7} length cap (up to 9,999,999 minutes, ~19 years) rejects it
    before int() ever runs."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "9" * 5000},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_underscore_separated_duration(main_module):
    """Regression test: Python's int("2_4") == 24 -- a digit string with an
    underscore separator should be rejected outright, not silently
    reinterpreted as a larger duration."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "2_4"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_non_ascii_digits(main_module):
    """Regression test: Python's re module matches \\d against every Unicode
    decimal digit, not just ASCII, and int() accepts them too
    (int("１０") == 10) -- a duration of fullwidth or Arabic-Indic
    digit characters used to be silently reinterpreted as the equivalent
    ASCII number instead of rejected, on a field the surrounding comment
    already says must not be quietly reinterpreted."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "１０"},  # fullwidth "10"
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_non_string_account(main_module):
    """Regression test: account_id used to flow straight into
    re.fullmatch(...), which raises TypeError (not a clean 400) for
    anything that isn't already a string."""
    event = _cli_request_event(
        body={"account": 111111111111, "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_duration_above_the_configured_maximum(main_module):
    """Regression test for the original bypass: a deployment can restrict
    everyone via permission_duration_list_override, in which case
    max_permissions_duration_time is documented as ignored. The CLI must be
    bounded by that same maximum (here, 60 minutes -- the "01:00" entry),
    not just its own looser upper bound."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "61"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    restricted_options = [
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="00:30"), value="00:30"),
        main_module.slack_helpers.Option(text=main_module.slack_helpers.PlainTextObject(text="01:00"), value="01:00"),
    ]
    with patch.object(main_module.slack_helpers, "get_max_duration_block", return_value=restricted_options):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 400


def test_handle_cli_access_request_respects_the_computed_max_duration_when_no_override_is_set(main_module):
    """Regression test: conftest.py's mock_env always sets
    permission_duration_list_override, so every other test here only ever
    exercises get_max_duration_block's `if` branch. This forces the `else`
    branch (max_permissions_duration_time-derived, 30-minute increments,
    clamped at 99 entries) that _max_allowed_minutes also depends on --
    conftest's max_permissions_duration_time=24 (hours) means the last
    computed option is "24:00", i.e. a 1440-minute maximum."""
    no_override_cfg = main_module.cfg.model_copy(update={"permission_duration_list_override": []})

    over_max_event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1441"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module, "cfg", no_override_cfg):
        result = main_module.handle_cli_access_request(over_max_event)
    assert result["statusCode"] == 400

    at_max_event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1440"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module, "cfg", no_override_cfg),
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request", return_value=(MagicMock(), True)),
    ):
        result = main_module.handle_cli_access_request(at_max_event)
    assert result["statusCode"] == 200


def test_handle_cli_access_request_rejects_cleanly_when_max_duration_is_misconfigured_to_zero(main_module):
    """Regression test: with no override and max_permissions_duration_time
    == 0, get_max_duration_block's computed range is empty (range(1, 1)) --
    _max_allowed_minutes used to call max() over that empty sequence with no
    default, raising ValueError. That reached the blanket exception handler
    as a 500 plus a Slack post on every single request, not just an
    over-max one. A misconfigured deployment should get a clean 400
    (duration must be no greater than 0), not a crash -- the real fix
    (Terraform validation on max_permissions_duration_time) is what should
    stop 0 from reaching here at all in practice."""
    zero_max_cfg = main_module.cfg.model_copy(update={"permission_duration_list_override": [], "max_permissions_duration_time": 0})
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module, "cfg", zero_max_cfg):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_accepts_a_duration_not_exactly_matching_any_configured_option(main_module):
    """Unlike the Slack dropdown, the CLI isn't limited to the *specific*
    entries a deployment's duration options list -- per an explicit design
    decision (the 30-minute increments are a Slack UI constraint, not a real
    one), any whole number of minutes up to the configured maximum is valid.
    45 minutes is accepted here even though neither "00:30" nor "01:00" is
    exactly 45 minutes, since both are within the 60-minute maximum."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "45"},
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
        patch.object(main_module, "process_access_request", return_value=(MagicMock(), True)),
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 200


def test_handle_cli_access_request_success_calls_process_access_request(main_module):
    # 47 is deliberately not a "round" number (not a multiple of 30, not an
    # hour) -- this is the actual proof that the granted duration is exactly
    # what was requested, not rounded/approximated to the nearest option a
    # human would pick from the Slack dropdown.
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "47"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    fake_decision = SimpleNamespace(reason=main_module.access_control.DecisionReason.ApprovalNotRequired)
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester) as mock_get_user,
        patch.object(main_module, "process_access_request", return_value=(fake_decision, True)) as mock_process,
    ):
        result = main_module.handle_cli_access_request(event)

    mock_get_user.assert_called_once_with(main_module.app.client, "req@example.com")
    mock_process.assert_called_once()
    called_kwargs = mock_process.call_args.kwargs
    assert called_kwargs["request"].account_id == "111111111111"
    assert called_kwargs["request"].permission_set_name == "FullOrgAdmin"
    assert called_kwargs["request"].reason == "debugging"
    assert called_kwargs["request"].requester_slack_id == "U_REQ"
    assert called_kwargs["request"].permission_duration == main_module.timedelta(minutes=47)
    # request_source/verified_arn are what let the approval message (and the
    # audit trail) distinguish a CLI-submitted request from a Slack one, and
    # what execute_decision's provenance check verifies against -- previously
    # only asserted downstream of this point, never at the point this
    # handler actually builds the RequestForAccess.
    assert called_kwargs["request"].request_source == "cli"
    assert called_kwargs["request"].verified_arn == "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com"
    assert called_kwargs["requester"] is fake_requester
    assert result["statusCode"] == 200


def test_handle_cli_access_request_reports_denied_decisions_as_not_ok(main_module):
    """Regression test: process_access_request's return value used to be
    discarded entirely, so a request refused for a real policy reason
    (RequesterNotAllowed/NoStatements/NoApprovers) still got the exact same
    {"ok": true, "message": "...posted for approval..."} a genuinely
    accepted request gets -- a CLI caller who isn't permitted to make the
    request saw a false success."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    fake_decision = SimpleNamespace(reason=main_module.access_control.DecisionReason.RequesterNotAllowed)
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request", return_value=(fake_decision, False)),
    ):
        result = main_module.handle_cli_access_request(event)

    body = json.loads(result["body"])
    assert body["ok"] is False
    assert result["statusCode"] == 200  # a 2xx with ok:false -- the Go client checks the body, not the status, for this


def test_handle_cli_access_request_reports_ok_false_when_requires_approval_finds_no_approvers(main_module):
    """Regression test: process_access_request's RequiresApproval branch can
    determine, only after calling find_approvers_in_slack, that none of the
    configured approvers exist in Slack -- it keeps decision.reason ==
    RequiresApproval (that's still the *policy* reason) but the request was
    never actually posted for approval. Deriving ok/not-ok from decision.reason
    alone (the old DENIED_DECISION_REASONS frozenset) missed this case
    entirely and reported ok:true for a request nobody could ever approve;
    the `succeeded` flag process_access_request now returns is what actually
    caught it, so this asserts against that flag rather than the reason."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    fake_requester = MagicMock(id="U_REQ", email="req@example.com")
    fake_decision = SimpleNamespace(reason=main_module.access_control.DecisionReason.RequiresApproval)
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=fake_requester),
        patch.object(main_module, "process_access_request", return_value=(fake_decision, False)),
    ):
        result = main_module.handle_cli_access_request(event)

    body = json.loads(result["body"])
    assert body["ok"] is False
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
        patch.object(
            main_module.slack_helpers,
            "get_user_by_email",
            side_effect=slack_sdk.errors.SlackApiError("users_not_found", {"ok": False, "error": "users_not_found"}),
        ),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result == main_module.cli_auth.GENERIC_REJECTION
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_rejects_duplicate_username_generically(main_module):
    """Two Identity Store users sharing the session's UserName get the generic rejection, not a 500."""
    duplicate_users = {
        "Users": [
            {"UserId": "u-1", "UserName": "req@example.com", "Emails": [{"Value": "a@example.com", "Primary": True}]},
            {"UserId": "u-2", "UserName": "req@example.com", "Emails": [{"Value": "b@example.com", "Primary": True}]},
        ]
    }
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(main_module.cli_auth.sso, "list_users_with_cache", return_value=duplicate_users),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result == main_module.cli_auth.GENERIC_REJECTION
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_reports_broken_slack_integration_instead_of_generic_rejection(main_module):
    """Regression test (#194 B5): a Slack error other than "users_not_found"
    -- invalid_auth/missing_scope from a rotated bot token or a dropped
    OAuth scope, or an internal_error/outage -- says nothing about whether
    the caller's AWS credentials are valid, so it must not be folded into
    GENERIC_REJECTION's "credentials not associated with SSO session"
    response. It must instead be treated like any other unexpected error:
    logged loudly, posted to the approvals channel, and reported to the
    caller as a 500, not a false claim their credentials are bad."""
    import slack_sdk.errors

    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(
            main_module.slack_helpers,
            "get_user_by_email",
            side_effect=slack_sdk.errors.SlackApiError("invalid_auth", {"ok": False, "error": "invalid_auth"}),
        ),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result != main_module.cli_auth.GENERIC_REJECTION
    assert result["statusCode"] == 500  # noqa: PLR2004
    mock_post_message.assert_called_once()


def test_handle_cli_access_request_still_returns_500_when_the_error_notification_itself_fails(main_module):
    """Regression test (#194 B5 residual, found live by Andrey Devyatkin):
    the blanket handler's own chat_postMessage call -- posting the
    "unexpected error" notification -- was unguarded, so a second Slack
    failure there (e.g. invalid_auth on both calls) propagated straight past
    this except block instead of reaching the documented 500 body below it.
    The caller got API Gateway's opaque "Internal Server Error" instead of
    {"message": "An unexpected error occurred..."}."""
    import slack_sdk.errors

    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(
            main_module.slack_helpers,
            "get_user_by_email",
            side_effect=slack_sdk.errors.SlackApiError("invalid_auth", {"ok": False, "error": "invalid_auth"}),
        ),
        patch.object(
            main_module.app.client,
            "chat_postMessage",
            side_effect=slack_sdk.errors.SlackApiError("invalid_auth", {"ok": False, "error": "invalid_auth"}),
        ),
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 500  # noqa: PLR2004
    assert json.loads(result["body"]) == {"message": "An unexpected error occurred while processing the request."}


def test_handle_cli_access_request_reports_unexpected_errors(main_module):
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with patch.object(main_module.slack_helpers, "get_user_by_email", side_effect=RuntimeError("boom")):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 500


def test_handle_cli_access_request_returns_503_on_transient_identity_store_error(main_module):
    """A throttled/unavailable Identity Store lookup says nothing about
    whether the caller's identity is valid -- it must not be reported as
    GENERIC_REJECTION's "your credentials are invalid" (403), nor page the
    approvals channel as an unexpected error (500). A distinguishable 503
    lets the CLI tell "retry this" apart from both of those."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(main_module.cli_auth, "extract_identity", side_effect=main_module.cli_auth.TransientAWSError),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 503
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_logs_the_sts_verified_arn_when_identity_cannot_be_resolved(main_module):
    """Regression test (#194 B10): an unresolvable identity must be logged with the caller's ARN --
    the one STS returned, not the forgeable requestContext value."""
    sts_arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com"
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn=sts_arn,
    )
    with (
        patch.object(main_module.cli_auth, "extract_identity", return_value=None),
        patch.object(main_module, "logger") as mock_logger,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result == main_module.cli_auth.GENERIC_REJECTION
    info_calls = mock_logger.info.call_args_list
    assert any(c.kwargs.get("extra", {}).get("user_arn") == sts_arn for c in info_calls)


def _sts_connection(status: int = 200, body: bytes = b"", failure: Exception | None = None) -> MagicMock:
    connection = MagicMock()
    connection.sock = MagicMock()
    response = connection.getresponse.return_value
    response.status = status
    response.read.side_effect = lambda amt: body[:amt]
    if failure:
        connection.getresponse.side_effect = failure
    return connection


@pytest.mark.parametrize(
    ("case", "status_code"),
    [
        ("wrong_audience", 403),
        ("tampered_payload", 403),
        ("stale", 403),
        ("sts_refuses", 403),
        ("sts_throttles", 503),
        ("sts_times_out", 503),
        ("outsider_account", 403),
        ("handler_crash", 500),
    ],
)
def test_proof_never_reaches_the_logs(main_module, caplog, case, status_code):
    """Runs the real verifier through each failure path at DEBUG and checks that no proof material
    (signature, session token, the presigned URL) appears in any captured log record."""
    from . import proof_helpers

    caplog.set_level("DEBUG")
    payload = {"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"}
    now = datetime.now(UTC)
    if case == "stale":
        now = now.replace(year=now.year - 1)
    body = make_body(
        payload, api_id="wrong" if case == "wrong_audience" else CLI_API_ID, query=proof_query(region=CLI_REGION, signed_at=now)
    )
    if case == "tampered_payload":
        doc = json.loads(body)
        doc["payload"] = json.dumps(payload | {"account": "222222222222"})
        body = json.dumps(doc)
    event = _cli_request_event()
    event["body"] = body

    arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com"
    connection = {
        "sts_refuses": _sts_connection(403, proof_helpers.sts_error_xml("SignatureDoesNotMatch")),
        "sts_throttles": _sts_connection(400, proof_helpers.sts_error_xml("Throttling")),
        "sts_times_out": _sts_connection(failure=TimeoutError(f"timed out {SIGNATURE} {SESSION_TOKEN}")),
    }.get(case, _sts_connection(200, proof_helpers.sts_identity_xml(arn)))
    not_found = botocore.exceptions.ClientError({"Error": {"Code": "AccountNotFoundException", "Message": "x"}}, "DescribeAccount")
    org_side_effect = not_found if case == "outsider_account" else None
    crash = RuntimeError("boom") if case == "handler_crash" else None

    with (
        patch.object(main_module.cli_proof, "fetch_caller_identity", _REAL_FETCH_CALLER_IDENTITY[0]),
        patch.object(main_module.cli_proof.http.client, "HTTPSConnection", return_value=connection),
        patch.object(main_module.org_client, "describe_account", side_effect=org_side_effect),
        patch.object(main_module.cli_auth, "extract_identity", side_effect=crash),
        patch.object(main_module.app.client, "chat_postMessage"),
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == status_code
    assert caplog.records, "expected the CLI path to log something"
    logged = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    for marker in ("X-Amz-Signature", "X-Amz-Credential", SIGNATURE, SESSION_TOKEN, "sts.eu-central-1.amazonaws.com/?"):
        assert marker not in logged


# ---------------------------------------------------------------------------
# process_access_request
# ---------------------------------------------------------------------------


def test_handle_cli_access_request_does_not_repost_a_failure_the_request_already_shows(main_module):
    """A grant failure is already on the request message and in its thread;
    the CLI handler must not add a second, top-level error post."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "debugging", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_x/req@example.com",
    )
    with (
        patch.object(main_module.slack_helpers, "get_user_by_email", return_value=MagicMock(id="U_REQ", email="req@example.com")),
        patch.object(main_module, "process_access_request", side_effect=main_module.ShownOnRequest("Granting access failed: boom")),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post,
    ):
        result = main_module.handle_cli_access_request(event)

    assert result["statusCode"] == 500  # noqa: PLR2004
    assert "Slack thread" in json.loads(result["body"])["message"]
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# process_access_request
# ---------------------------------------------------------------------------


def _request(main_module, **overrides):  # noqa: ANN001, ANN202, ANN003
    fields = {
        "account_id": "111111111111",
        "permission_set_name": "FullOrgAdmin",
        "reason": "testing",
        "requester_slack_id": "U_REQ",
        "permission_duration": main_module.timedelta(hours=1),
    }
    return main_module.slack_helpers.RequestForAccess(**(fields | overrides))


def _slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456"}
    client.conversations_members.return_value = MagicMock(data={"members": []})
    return client


def _texts(method: MagicMock) -> list[str]:
    return [c.kwargs.get("text") or "" for c in method.call_args_list]


def _thread_replies(client: MagicMock) -> list[str]:
    return [c.kwargs["text"] for c in client.chat_postMessage.call_args_list if c.kwargs.get("thread_ts")]


def _decision(main_module, reason, grant=False, approvers=frozenset()):  # noqa: ANN001, ANN202
    return main_module.access_control.AccessRequestDecision(
        grant=grant, reason=reason, based_on_statements=frozenset(), approvers=approvers
    )


def _process(main_module, client, decision, execute=None, approvers=None):  # noqa: ANN001, ANN202
    """Runs process_access_request with AWS mocked; execute is execute_decision's side_effect."""
    with (
        patch.object(main_module.access_control, "make_decision_on_access_request", return_value=decision),
        patch.object(main_module.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(main_module.slack_helpers, "find_approvers_in_slack", return_value=approvers or ([], [])),
        patch.object(main_module.schedule, "schedule_discard_buttons_event") as mock_discard,
        patch.object(main_module.schedule, "schedule_approver_notification_event"),
        patch.object(main_module.access_control, "execute_decision", side_effect=execute or (lambda **_: [])) as mock_execute,
    ):
        requester = MagicMock(id="U_REQ", email="email@domen.com", real_name="Test User")
        request = _request(main_module, account_name="aft")
        result = main_module.process_access_request(request=request, requester=requester, client=client)
    return result, mock_execute, mock_discard


def test_process_access_request_reflects_a_grant_failure_instead_of_claiming_success(main_module):
    """The request is posted as Processing, granted, and only then shown as an
    outcome -- a failed grant ends Failed with the detail in the thread, and
    nothing ever claims success."""
    client = _slack_client()
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    def _fail(**_kwargs):  # noqa: ANN202, ANN003
        raise RuntimeError("boom: permission set not found")

    with pytest.raises(main_module.ShownOnRequest):
        _process(main_module, client, decision, execute=_fail)

    assert client.chat_postMessage.call_args_list[0].kwargs["text"].startswith(":hourglass_flowing_sand: *Processing")
    assert client.chat_update.call_args.kwargs["text"].startswith(":x: *Failed")
    assert "Granting access failed: boom" in _thread_replies(client)[0]
    assert not any("access granted" in t for t in _texts(client.chat_postMessage) + _texts(client.chat_update))


def test_process_access_request_shows_the_outcome_only_after_the_grant(main_module):
    """#194 ordering: nothing reads as granted before execute_decision runs."""
    client = _slack_client()
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    def _grant(**kwargs):  # noqa: ANN202, ANN003
        assert client.chat_update.call_count == 0
        assert not any("access granted" in t for t in _texts(client.chat_postMessage))
        assert kwargs["channel_id"] == "x"
        assert kwargs["message_ts"] == "123.456"
        return []

    (result, succeeded), _, _ = _process(main_module, client, decision, execute=_grant)

    assert result is decision
    assert succeeded is True
    final = client.chat_update.call_args.kwargs
    assert final["text"] == ":white_check_mark: *Auto-approved · FullOrgAdmin → aft #111111111111 for* <@U_REQ>"
    status = next(b for b in final["blocks"] if b["block_id"] == "status")["elements"][0]["text"]
    assert status.startswith("Self-approval allowed · access ends at <!date^")
    assert _thread_replies(client)[0].startswith("<@U_REQ> access granted, ends at <!date^")
    assert not any("will be approved automatically" in t for t in _texts(client.chat_postMessage))


def test_process_access_request_pings_the_requester_even_when_the_final_update_fails(main_module):
    client = _slack_client()
    client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")
    decision = _decision(main_module, main_module.access_control.DecisionReason.ApprovalNotRequired, grant=True)

    (_, succeeded), _, _ = _process(main_module, client, decision)

    assert succeeded is True
    assert any(t.startswith("<@U_REQ> access granted") for t in _thread_replies(client))


def test_process_access_request_still_notifies_when_channel_membership_check_fails(main_module):
    """A failed membership check must not suppress any notification (#194 A4);
    it counts as outside the channel, so the requester gets a DM too."""
    client = _slack_client()
    client.conversations_members.side_effect = RuntimeError("Slack is briefly unavailable")
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    (_, succeeded), _, _ = _process(main_module, client, decision)

    assert succeeded is True
    assert client.chat_update.call_args_list
    assert any("access granted" in t for t in _thread_replies(client))
    assert any(c.kwargs["channel"] == "U_REQ" for c in client.chat_postMessage.call_args_list)


def test_process_access_request_shows_a_post_grant_failure_as_granted_not_scheduled(main_module):
    """The assignment exists, so this is not Failed: the request says access is live."""
    client = _slack_client()
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    def _post_grant_failure(**_kwargs):  # noqa: ANN202, ANN003
        raise main_module.access_control.PostGrantError("throttled")

    (_, succeeded), _, _ = _process(main_module, client, decision, execute=_post_grant_failure)

    assert succeeded is True
    assert client.chat_update.call_args.kwargs["text"].startswith(":warning: *Granted")
    assert "could not be scheduled: throttled" in _thread_replies(client)[0]


def _old_revoke_event(main_module):  # noqa: ANN001, ANN202
    return main_module.schedule.RevokeEvent(
        schedule_name="s",
        approver=main_module.entities.slack.User(id="U_OLD_APPROVER", email="a@x.com", real_name="A"),
        requester=main_module.entities.slack.User(id="U_REQ", email="r@x.com", real_name="R"),
        user_account_assignment=main_module.sso.UserAccountAssignment(
            instance_arn="i", account_id="111111111111", permission_set_arn="ps", user_principal_id="u"
        ),
        permission_duration=main_module.timedelta(hours=1),
        channel_id="C_OLD",
        message_ts="100.1",
    )


def _client_with_old_request() -> MagicMock:
    client = _slack_client()
    client.chat_getPermalink.return_value = {"permalink": "https://x.slack.com/archives/C/p123456"}
    old_message = {"ts": "100.1", "blocks": [{"block_id": "reason", "type": "section", "text": {"type": "mrkdwn", "text": ">old"}}]}
    client.conversations_history.return_value = {"messages": [old_message]}
    return client


def test_process_access_request_marks_a_replaced_request_as_extended(main_module):
    client = _client_with_old_request()
    old_event = _old_revoke_event(main_module)
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    _process(main_module, client, decision, execute=lambda **_: [old_event])

    extended = next(c.kwargs for c in client.chat_update.call_args_list if c.kwargs["ts"] == "100.1")
    assert extended["channel"] == "C_OLD"
    assert extended["text"].startswith(":repeat: *Extended · FullOrgAdmin → aft #111111111111 for* <@U_REQ>")
    status = next(b for b in extended["blocks"] if b["block_id"] == "status")["elements"][0]["text"]
    assert status == "Approved by <@U_OLD_APPROVER> · extended by <https://x.slack.com/archives/C/p123456|newer request>"


def test_process_access_request_marks_replaced_requests_extended_when_the_new_schedule_fails(main_module):
    """The older schedules were deleted before the new one failed, so the older
    request no longer ends when it says: it must still show Extended."""
    client = _client_with_old_request()
    old_event = _old_revoke_event(main_module)
    decision = _decision(main_module, main_module.access_control.DecisionReason.SelfApproval, grant=True)

    def _schedule_failed(**_kwargs):  # noqa: ANN202, ANN003
        raise main_module.access_control.PostGrantError("throttled", [old_event])

    _process(main_module, client, decision, execute=_schedule_failed)

    assert client.chat_update.call_args_list[0].kwargs["text"].startswith(":warning: *Granted")
    extended = next(c.kwargs for c in client.chat_update.call_args_list if c.kwargs["ts"] == "100.1")
    assert extended["text"].startswith(":repeat: *Extended")


def test_process_access_request_posts_pending_with_buttons_and_pings_approvers(main_module):
    client = _slack_client()
    approver = main_module.entities.slack.User(id="U_APP", email="approver@example.com", real_name="A")
    decision = _decision(
        main_module, main_module.access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver@example.com"])
    )

    (_, succeeded), mock_execute, mock_discard = _process(main_module, client, decision, approvers=([approver], []))

    assert succeeded is True
    posted = client.chat_postMessage.call_args_list[0].kwargs
    assert posted["text"].endswith("*for 1 hour*")
    assert any(b["block_id"] == "buttons" for b in posted["blocks"])
    assert _thread_replies(client) == ["<@U_APP>: waiting for your approval"]
    mock_discard.assert_called_once()
    # Called for every decision so a refusal can be audited; this one grants nothing.
    assert mock_execute.call_args.kwargs["decision"] is decision


def test_process_access_request_reports_not_succeeded_when_requires_approval_finds_no_approvers_in_slack(main_module):
    """RequiresApproval keeps its reason even when no approver exists in Slack,
    but nothing is queued: the request is posted Failed, without buttons."""
    client = _slack_client()
    decision = _decision(
        main_module, main_module.access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver@example.com"])
    )

    (result, succeeded), _, mock_discard = _process(main_module, client, decision, approvers=([], ["approver@example.com"]))

    assert result.reason == main_module.access_control.DecisionReason.RequiresApproval
    assert succeeded is False
    posted = client.chat_postMessage.call_args_list[0].kwargs
    assert posted["text"].startswith(":x: *Failed")
    assert not any(b["block_id"] == "buttons" for b in posted["blocks"])
    assert "None of the approvers" in _thread_replies(client)[0]
    mock_discard.assert_not_called()


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("NoApprovers", "Nobody can approve this request · details in thread"),
        ("NoStatements", "No statement covers this request · details in thread"),
        ("RequesterNotAllowed", "Requester is not allowed to request this · details in thread"),
    ],
)
def test_process_access_request_posts_refusals_as_failed(main_module, reason, status):
    client = _slack_client()
    decision = _decision(main_module, main_module.access_control.DecisionReason[reason])

    (_, succeeded), mock_execute, _ = _process(main_module, client, decision)

    assert succeeded is False
    posted = client.chat_postMessage.call_args_list[0].kwargs
    assert next(b for b in posted["blocks"] if b["block_id"] == "status")["elements"][0]["text"] == status
    assert len(_thread_replies(client)) == 1
    assert mock_execute.call_args.kwargs["decision"] is decision


def _process_auditing(main_module, client, decision, approvers=None, log_error=None):  # noqa: ANN001, ANN202
    """Runs process_access_request through the real execute_decision; returns the S3 audit mock."""
    with (
        patch.object(main_module.access_control, "make_decision_on_access_request", return_value=decision),
        patch.object(main_module.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(main_module.slack_helpers, "find_approvers_in_slack", return_value=approvers or ([], [])),
        patch.object(main_module.schedule, "schedule_discard_buttons_event"),
        patch.object(main_module.schedule, "schedule_approver_notification_event"),
        patch.object(main_module.s3, "log_operation", side_effect=log_error) as mock_log_operation,
    ):
        requester = MagicMock(id="U_REQ", email="email@domen.com", real_name="Test User")
        main_module.process_access_request(request=_request(main_module, account_name="aft"), requester=requester, client=client)
    return mock_log_operation


def test_process_access_request_audits_a_refusal_as_declined(main_module):
    decision = _decision(main_module, main_module.access_control.DecisionReason.NoApprovers)

    mock_log_operation = _process_auditing(main_module, _slack_client(), decision)

    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert (audit_entry.operation_type, audit_entry.decision_reason) == ("declined", "NoApprovers")
    assert (audit_entry.approver_slack_id, audit_entry.approver_email) == ("NA", "NA")


def test_process_access_request_writes_one_declined_entry_when_requires_approval_finds_no_approvers_in_slack(main_module):
    decision = _decision(
        main_module, main_module.access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver@example.com"])
    )

    mock_log_operation = _process_auditing(main_module, _slack_client(), decision, approvers=([], ["approver@example.com"]))

    mock_log_operation.assert_called_once()
    entry = mock_log_operation.call_args.kwargs["audit_entry"]
    request = _request(main_module, account_name="aft")
    assert (entry.operation_type, entry.decision_reason) == ("declined", "NoApproversFoundInSlack")
    assert (entry.approver_slack_id, entry.approver_email) == ("NA", "NA")
    assert (entry.audit_entry_type, entry.account_id, entry.role_name) == ("account", request.account_id, request.permission_set_name)
    assert (entry.request_source, entry.verified_arn) == (request.request_source, request.verified_arn)
    assert (entry.requester_slack_id, entry.requester_email) == ("U_REQ", "email@domen.com")


def test_process_access_request_no_approvers_in_slack_audit_failure_keeps_the_slack_outcome(main_module):
    client = _slack_client()
    decision = _decision(
        main_module, main_module.access_control.DecisionReason.RequiresApproval, approvers=frozenset(["approver@example.com"])
    )

    _process_auditing(main_module, client, decision, approvers=([], ["approver@example.com"]), log_error=RuntimeError("s3 down"))

    assert client.chat_postMessage.call_args_list[0].kwargs["text"].startswith(":x: *Failed")
    assert "None of the approvers" in _thread_replies(client)[0]


# ---------------------------------------------------------------------------
# handle_button_click
# ---------------------------------------------------------------------------


def _button_click_body(main_module, action: str = "approve", **request_overrides) -> dict:  # noqa: ANN001, ANN003
    """A click on a pending message the real builder produced."""
    sh = main_module.slack_helpers
    request = _request(main_module, account_name="aft", **request_overrides)
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    value = next(b for b in blocks if b["block_id"] == "buttons")["elements"][0]["value"]
    return {
        "actions": [{"action_id": action, "value": value}],
        "user": {"id": "U_APPROVER"},
        "message": {"ts": "12345.6789", "blocks": blocks},
        "channel": {"id": "C123"},
    }


APPROVER = SimpleNamespace(id="U_APPROVER", email="approver@example.com")
REQUESTER = SimpleNamespace(id="U_REQ", email="req@example.com")


def _click(main_module, body, client, execute=None, decision=None):  # noqa: ANN001, ANN202
    decision = decision or main_module.access_control.ApproveRequestDecision(grant=True, permit=True, based_on_statements=frozenset())
    with (
        patch.object(main_module.slack_helpers, "get_user", side_effect=[APPROVER, REQUESTER]),
        patch.object(main_module.access_control, "get_requester_group_ids_if_needed", return_value=frozenset()) as mock_group_ids,
        patch.object(main_module.access_control, "make_decision_on_approve_request", return_value=decision) as mock_decide,
        patch.object(main_module.access_control, "execute_decision", side_effect=execute or (lambda **_: [])) as mock_execute,
    ):
        result = main_module.handle_button_click.__wrapped__(body=body, client=client, context={})
    return result, mock_execute, mock_decide, mock_group_ids


def test_handle_button_click_reflects_a_grant_failure_instead_of_claiming_success(main_module):
    client = _slack_client()

    def _fail(**_kwargs):  # noqa: ANN202, ANN003
        raise RuntimeError("boom: permission set not found")

    with pytest.raises(main_module.ShownOnRequest):
        _click(main_module, _button_click_body(main_module), client, execute=_fail)

    assert client.chat_update.call_args.kwargs["text"].startswith(":x: *Failed · FullOrgAdmin → aft")
    assert _thread_replies(client) == ["Granting access failed: boom: permission set not found"]
    assert main_module.cache_for_dublicate_requests == {}


def test_handle_button_click_shows_approved_and_pings_the_requester(main_module):
    client = _slack_client()

    _, mock_execute, _, _ = _click(main_module, _button_click_body(main_module), client)

    assert mock_execute.call_args.kwargs["channel_id"] == "C123"
    assert mock_execute.call_args.kwargs["message_ts"] == "12345.6789"
    assert client.chat_update.call_args.kwargs["text"] == ":white_check_mark: *Approved · FullOrgAdmin → aft #111111111111 for* <@U_REQ>"
    assert _thread_replies(client)[0].startswith("<@U_REQ> access granted, ends at")
    assert len(_thread_replies(client)) == 1
    assert main_module.cache_for_dublicate_requests == {}


def test_handle_button_click_clears_the_dedup_cache_when_make_decision_on_approve_request_raises(main_module):
    """#194 A3 residual: an exception here must not leave the request stuck on "already in progress"."""
    client = _slack_client()
    with (
        patch.object(main_module.slack_helpers, "get_user", side_effect=[APPROVER, REQUESTER]),
        patch.object(main_module.access_control, "make_decision_on_approve_request", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        main_module.handle_button_click.__wrapped__(body=_button_click_body(main_module), client=client, context={})

    assert main_module.cache_for_dublicate_requests == {}


def test_handle_button_click_strips_buttons_before_execute_decision_runs(main_module):
    """#194 A2: the buttons go before the grant runs, so a second approver's
    click in another Lambda container has nothing left to press."""
    client = _slack_client()

    def _execute(**_kwargs):  # noqa: ANN202, ANN003
        assert client.chat_update.call_count == 1
        early = client.chat_update.call_args.kwargs
        assert not any(b["block_id"] == "buttons" for b in early["blocks"])
        assert early["text"].startswith(":hourglass_flowing_sand: *Processing")
        return []

    _click(main_module, _button_click_body(main_module), client, execute=_execute)

    assert client.chat_update.call_count == 2  # noqa: PLR2004


def test_handle_button_click_evaluates_eligibility_against_pinned_verified_email_for_cli_requests(main_module):
    """#194 B4: a CLI request is evaluated against the identity verified at
    submission, carried in the button value, not the requester's current Slack email."""
    client = _slack_client()
    body = _button_click_body(
        main_module,
        request_source="cli",
        verified_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/pinned.user",
        verified_user_id="pinned-user-id",
        verified_email="pinned@example.com",
    )

    _, mock_execute, mock_decide, mock_group_ids = _click(main_module, body, client)

    assert mock_group_ids.call_args.args[1:] == ("pinned@example.com", "pinned-user-id")
    assert mock_decide.call_args.kwargs["requester_email"] == "pinned@example.com"
    assert mock_execute.call_args.kwargs["verified_user_id"] == "pinned-user-id"


def test_handle_button_click_notification_failure_after_a_successful_grant_is_not_reported_as_failure(main_module):
    """Once the grant succeeded, a Slack failure while showing it must not
    propagate -- and the requester is still pinged in the thread."""
    client = _slack_client()
    client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")

    result, _, _, _ = _click(main_module, _button_click_body(main_module), client)

    assert result is None
    assert _thread_replies(client)[0].startswith("<@U_REQ> access granted")


def test_handle_button_click_discard_shows_discarded_without_a_thread_reply(main_module):
    client = _slack_client()

    _, mock_execute, _, _ = _click(main_module, _button_click_body(main_module, action="discard"), client)

    assert client.chat_update.call_args.kwargs["text"].startswith(":wastebasket: *Discarded")
    assert _thread_replies(client) == []
    assert any(
        c.kwargs["channel"] == "U_REQ" and "discarded by <@U_APPROVER>" in c.kwargs["text"] for c in client.chat_postMessage.call_args_list
    )
    mock_execute.assert_not_called()


def test_handle_button_click_discard_that_fails_to_show_tells_the_approver_not_the_requester(main_module):
    """The buttons are still live, so the requester must not hear it was discarded."""
    client = _slack_client()
    client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")

    _click(main_module, _button_click_body(main_module, action="discard"), client)

    assert _thread_replies(client) == ["<@U_APPROVER> the discard did not go through, please try again."]
    assert not any(c.kwargs["channel"] == "U_REQ" for c in client.chat_postMessage.call_args_list)


def test_handle_button_click_discard_logs_a_declined_audit_entry_after_removing_buttons(main_module):
    client = _slack_client()
    buttons_removed_before_audit = []
    with patch.object(
        main_module.s3, "log_operation", side_effect=lambda **_: buttons_removed_before_audit.append(client.chat_update.called)
    ) as mock_log:
        _click(main_module, _button_click_body(main_module, action="discard"), client)

    assert buttons_removed_before_audit == [True]
    audit_entry = mock_log.call_args.kwargs["audit_entry"]
    assert (audit_entry.operation_type, audit_entry.decision_reason) == ("declined", "Discarded")
    assert audit_entry.approver_slack_id == "U_APPROVER"
    assert (audit_entry.account_id, audit_entry.role_name) == ("111111111111", "FullOrgAdmin")


def test_handle_button_click_discard_audit_failure_still_shows_discarded(main_module):
    client = _slack_client()
    with patch.object(main_module.s3, "log_operation", side_effect=RuntimeError("s3 down")):
        _click(main_module, _button_click_body(main_module, action="discard"), client)

    assert client.chat_update.call_args.kwargs["text"].startswith(":wastebasket: *Discarded")
    assert any(c.kwargs["channel"] == "U_REQ" for c in client.chat_postMessage.call_args_list)


def test_handle_button_click_discard_writes_no_audit_entry_if_buttons_not_removed(main_module):
    client = _slack_client()
    client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")
    with patch.object(main_module.s3, "log_operation") as mock_log:
        _click(main_module, _button_click_body(main_module, action="discard"), client)

    mock_log.assert_not_called()


def test_handle_button_click_not_permitted_writes_no_audit_entry(main_module):
    """A non-approver's click leaves the request live; only Slack's reply records it."""
    client = _slack_client()
    decision = main_module.access_control.ApproveRequestDecision(grant=False, permit=False, based_on_statements=frozenset())
    with patch.object(main_module.s3, "log_operation") as mock_log_operation:
        _click(main_module, _button_click_body(main_module), client, decision=decision)

    assert any("can not approve" in (c.kwargs.get("text") or "") for c in client.chat_postMessage.call_args_list)
    mock_log_operation.assert_not_called()


def test_handle_button_click_on_a_pre_upgrade_message_asks_to_request_again(main_module):
    client = _slack_client()
    body = {
        "actions": [{"action_id": "approve", "value": "approve"}],
        "user": {"id": "U_APPROVER"},
        "message": {
            "ts": "12345.6789",
            "text": "old",
            "blocks": [{"block_id": "content", "fields": [{"text": "Requester: <@U_REQ>"}]}, {"block_id": "buttons", "elements": []}],
        },
        "channel": {"id": "C123"},
    }
    with patch.object(main_module.access_control, "execute_decision") as mock_execute:
        main_module.handle_button_click.__wrapped__(body=body, client=client, context={})

    assert _thread_replies(client) == ["This request was made before an Elevator upgrade — please request again"]
    stripped = client.chat_update.call_args.kwargs
    assert stripped["ts"] == "12345.6789"
    assert [b["block_id"] for b in stripped["blocks"]] == ["content"]
    assert stripped["text"] == "This request was made before an Elevator upgrade — please request again"
    mock_execute.assert_not_called()


def test_handle_button_click_routes_group_requests_by_kind(main_module):
    sh = main_module.slack_helpers
    request = sh.RequestForGroupAccess(
        group_id="g-1", group_name="admins", reason="r", requester_slack_id="U_REQ", permission_duration=main_module.timedelta(hours=1)
    )
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    value = next(b for b in blocks if b["block_id"] == "buttons")["elements"][0]["value"]
    body = {
        "actions": [{"action_id": "approve", "value": value}],
        "user": {"id": "U_A"},
        "message": {"ts": "1.2", "blocks": blocks},
        "channel": {"id": "C1"},
    }

    with patch.object(main_module.group, "handle_group_button_click") as mock_group_click:
        main_module.handle_button_click.__wrapped__(body=body, client=MagicMock(), context={})

    assert mock_group_click.call_args.kwargs["payload"].request == request


# ---------------------------------------------------------------------------
# handle_request_for_access_submittion
# ---------------------------------------------------------------------------


def _submit_modal(main_module, request):  # noqa: ANN001, ANN202
    client = MagicMock()
    requester = MagicMock(id="U_REQ", email="req@example.com")
    with (
        patch.object(main_module.slack_helpers.RequestForAccessView, "parse", return_value=request),
        patch.object(main_module.slack_helpers, "get_user", return_value=requester),
        patch.object(main_module, "process_access_request") as mock_process,
    ):
        result = main_module.handle_request_for_access_submittion.__wrapped__(body={}, ack=MagicMock(), client=client, context={})
    assert result is None
    return client, requester, mock_process


@pytest.mark.parametrize(
    ("overrides", "rejection"),
    [
        ({"reason": "x" * 1001}, "Reason must be 1000 characters or fewer"),
        ({"reason": "x" * 1000, "permission_set_name": "p" * 1000}, "Request is too large for Slack; shorten the reason"),
    ],
)
def test_handle_request_for_access_submittion_rejects_by_dm(main_module, overrides, rejection):
    """The modal is already closed when this runs, so the requester is told by DM."""
    client, _, mock_process = _submit_modal(main_module, _request(main_module, **overrides))

    mock_process.assert_not_called()
    client.chat_postMessage.assert_called_once_with(channel="U_REQ", text=f"Your access request wasn't submitted: {rejection}.")


def test_handle_request_for_access_submittion_submits_a_normal_request_with_its_account_name(main_module):
    client, requester, mock_process = _submit_modal(main_module, _request(main_module, reason="debugging prod issue"))

    named = _request(main_module, reason="debugging prod issue", account_name="aft")
    mock_process.assert_called_once_with(request=named, requester=requester, client=client)
    client.chat_postMessage.assert_not_called()
