"""Tests for the CLI access-request path in main.py: lambda_handler's dispatch
fork and handle_cli_access_request's own branches, plus process_access_request
itself (shared by both the CLI and Slack modal paths -- test_access_control.py
and test_group.py test execute_decision/execute_decision_on_group_request
directly, not process_access_request, so its own message-ordering/return-value
behavior needed covering here).
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

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
    rather than trusting it directly. That same stubbed user's UserId
    ("u-req") is what both find_email_by_username (inside extract_identity)
    and handle_cli_access_request's own email-round-trip cross-check (a
    second, independent lookup by requester.email against the same stub)
    resolve to -- they agree by construction here, since every test below
    uses the same "req@example.com" identity throughout; the mismatch case
    is exercised on its own, separately.

    get_paginator dispatches by operation name (list_users vs.
    list_account_assignments) since the shared client backs both calls --
    a single flat paginate() stub can't serve both shapes at once.
    list_account_assignments is stubbed to report one real assignment (for
    the "u-req" identity every test below verifies), since
    handle_cli_access_request now requires at least one before proceeding
    -- the empty/no-assignment case is exercised on its own, separately.

    organizations.get_accounts_from_config_with_cache and
    sso.get_permission_sets_from_config_with_cache are patched to the fake
    catalog-lookup functions above, rather than left to hit the (mocked)
    AWS clients directly -- handle_cli_access_request now validates account
    and permission_set against their real return values, not a literal
    membership check against cfg.accounts/cfg.permission_sets."""
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
            elif operation_name == "list_account_assignments":
                paginator.paginate.return_value = [
                    {"AccountAssignments": [{"AccountId": "111111111111", "PrincipalId": "u-req", "PrincipalType": "USER"}]}
                ]
            else:
                paginator.paginate.return_value = []
            return paginator

        shared_client.get_paginator.side_effect = _paginator_for
        mock_boto3_session.return_value.client.return_value = shared_client
        mock_default_session.return_value.client.return_value = shared_client
        mock_app_cls.return_value = MagicMock()
        import main

        with (
            patch.object(main.organizations, "get_accounts_from_config_with_cache", side_effect=_fake_accounts_from_config),
            patch.object(main.sso, "get_permission_sets_from_config_with_cache", side_effect=_fake_permission_sets_from_config),
        ):
            yield main

    sys.modules.pop("main", None)
    sys.modules.pop("group", None)


def _cli_request_event(body: dict | None = None, user_arn: str | None = None, api_id: str | None = "test-api-id") -> dict:
    """An event shaped like what handle_cli_access_request itself consumes.
    routeKey/rawPath aren't relevant here, since these tests call
    handle_cli_access_request directly rather than going through
    lambda_handler's dispatch (that's covered separately, below).

    api_id defaults to conftest.py's mock_env cli_expected_api_id value, so
    every test below passes the apiId check for free unless it's overridden
    -- that check is exercised on its own, separately."""
    event = {
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"apiId": api_id} if api_id is not None else {},
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


def test_handle_cli_access_request_rejects_mismatched_api_id(main_module):
    """Defense-in-depth check: an event whose requestContext.apiId doesn't
    match this deployment's own API Gateway (conftest.py's mock_env sets
    cli_expected_api_id to "test-api-id") must be rejected before identity
    verification even runs -- this is what catches a direct
    lambda:InvokeFunction call that didn't bother forging this field."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
        api_id="some-other-api-id",
    )
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_rejects_missing_api_id(main_module):
    """A direct lambda:InvokeFunction call that omits requestContext.apiId
    entirely (rather than forging a wrong one) must also be rejected --
    None must never accidentally equal cli_expected_api_id."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
        api_id=None,
    )
    result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


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


def test_handle_cli_access_request_rejects_identity_with_no_account_assignment(main_module):
    """Defense-in-depth against round-1 finding #6: iam:GetRole proves
    role_name is genuinely IAM Identity Center-provisioned, but says nothing
    about whether this specific user was ever actually assigned anything on
    this account -- a session under a reserved-path role that's still
    technically valid but was orphaned (e.g. after every permission set
    assignment for this user was revoked) must still be rejected."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module.sso, "has_account_assignment", return_value=False):
        result = main_module.handle_cli_access_request(event)
    assert result == main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_calls_has_account_assignment_with_the_resolved_account_and_permission_set_arn(main_module):
    """Regression test: has_account_assignment moved from
    list_account_assignments_for_principal (documented by AWS as callable
    only from the IAM Identity Center *management* account -- unusable in
    this module's own recommended delegated-admin deployment) to
    list_account_assignments (per account + permission set, no such
    restriction) -- which needs the real permission_set_arn, not just the
    name, so this pins that has_account_assignment is actually called with
    the account/permission-set pair the request resolved to, not called
    before that resolution happens."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "FullOrgAdmin", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with patch.object(main_module.sso, "has_account_assignment", return_value=True) as mock_check:
        result = main_module.handle_cli_access_request(event)
    mock_check.assert_called_once_with(
        main_module.sso_client,
        main_module.cfg.sso_instance_arn,
        "u-req",
        "111111111111",
        "arn:aws:sso:::permissionSet/ssoins-1/ps-fullorgadmin",
    )
    assert result != main_module.cli_auth.GENERIC_REJECTION


def test_handle_cli_access_request_returns_503_on_transient_account_assignment_error(main_module):
    """A throttled/unavailable list_account_assignments says nothing about
    whether the assignment exists -- it must not be reported as
    GENERIC_REJECTION's "your credentials are invalid" (403), nor page the
    approvals channel as an unexpected error (500). A distinguishable 503
    lets the CLI tell "retry this" apart from both of those."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    with (
        patch.object(main_module.sso, "has_account_assignment", side_effect=main_module.sso.TransientSSOError),
        patch.object(main_module.app.client, "chat_postMessage") as mock_post_message,
    ):
        result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 503
    mock_post_message.assert_not_called()


def test_handle_cli_access_request_returns_503_on_transient_account_catalog_error(main_module):
    """Regression test: get_accounts_from_config_with_cache and
    get_permission_sets_from_config_with_cache are cache-backed, but that
    only shields a *warm* cache -- with caching disabled or a cold cache, a
    throttle propagates the raw botocore error. Like the has_account_assignment
    transient case above, this must come back as a retryable 503, not the
    blanket handler's 500 plus a Slack post."""
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


def test_handle_cli_access_request_rejects_invalid_json_body(main_module):
    event = _cli_request_event(user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    event["body"] = "{not json"
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_non_object_json_body(main_module):
    """Regression test: a syntactically valid JSON document that isn't an
    object (e.g. a bare array) used to pass json.loads, then raise
    AttributeError at body.get(...) -- unwinding to the generic 500 handler
    and posting to the approvals channel for what's just bad input."""
    event = _cli_request_event(user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com")
    event["body"] = "[]"
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_oversized_reason(main_module):
    """Slack's section-block text fields cap at 2000 chars; an oversized
    reason used to reach chat_postMessage unbounded, get rejected with
    invalid_blocks, and unwind to the generic 500 handler."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x" * 2001, "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


def test_handle_cli_access_request_rejects_reason_that_only_overflows_once_wrapped(main_module):
    """Regression test: the cap used to be a plain 2000, not 2000 minus the
    length of the "Reason: " prefix slack_helpers.py wraps it in for that
    Slack field -- a 1993-2000 character reason passed the old check but
    still produced a field over Slack's real 2000-char limit once wrapped
    (e.g. 1996 chars -> "Reason: " + 1996 chars = 2004), reaching
    chat_postMessage and unwinding to the generic 500 handler."""
    event = _cli_request_event(
        body={"account": "111111111111", "permission_set": "Foo", "reason": "x" * 1993, "duration": "1"},
        user_arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo/req@example.com",
    )
    result = main_module.handle_cli_access_request(event)
    assert result["statusCode"] == 400


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


# ---------------------------------------------------------------------------
# process_access_request
# ---------------------------------------------------------------------------


def test_process_access_request_reflects_a_grant_failure_instead_of_claiming_success(main_module):
    """Regression test: chat_update (recoloring the message and setting its
    text) previously ran before execute_decision -- for an auto-grant
    decision (SelfApproval/ApprovalNotRequired), that meant the message was
    already updated to good_result_emoji/"will be approved automatically"
    before the actual grant was even attempted. A failure in execute_decision
    (a stale permission set name, an AWS API error, anything) then left that
    misleading "success" message in the channel permanently, with the real
    error surfacing as a disconnected message from whichever blanket handler
    the exception unwound to. execute_decision now runs first, so a failure
    can override the message before it's ever posted, and the "Permissions
    granted" follow-up must never fire."""
    request = main_module.slack_helpers.RequestForAccess(
        account_id="111111111111",
        permission_set_name="FullOrgAdmin",
        reason="testing",
        requester_slack_id="U_REQ",
        permission_duration=main_module.timedelta(hours=1),
    )
    requester = MagicMock(id="U_REQ", email="email@domen.com", real_name="Test User")
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"blocks": []}}
    client.conversations_members.return_value = MagicMock(data={"members": []})
    client.users_info.return_value = MagicMock(
        data={"user": {"id": "U_REQ", "profile": {"email": "email@domen.com"}, "real_name": "Test User"}}
    )

    fake_decision = main_module.access_control.AccessRequestDecision(
        grant=True,
        reason=main_module.access_control.DecisionReason.SelfApproval,
        based_on_statements=frozenset(),
        approvers=frozenset(),
    )
    fake_account = main_module.entities.aws.Account(id="111111111111", name="test-account")

    with (
        patch.object(main_module.access_control, "make_decision_on_access_request", return_value=fake_decision),
        patch.object(main_module.organizations, "describe_account", return_value=fake_account),
        patch.object(main_module.slack_helpers.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(main_module.access_control, "execute_decision", side_effect=RuntimeError("boom: permission set not found")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        main_module.process_access_request(request=request, requester=requester, client=client)

    update_call = client.chat_update.call_args
    assert update_call is not None, "chat_update should still have run, with the failure reflected in it"
    assert "error occurred" in update_call.kwargs["text"].lower()
    assert not any("permissions granted" in (c.kwargs.get("text") or "").lower() for c in client.chat_postMessage.call_args_list)
    # Regression coverage for a second bug in the same area: execute_decision
    # used to run *after* the thread reply and DM were already sent (only
    # the header's chat_update was moved earlier), so those two messages
    # could still read "will be approved automatically" even once the
    # header itself correctly showed the failure. Every chat_postMessage
    # call here -- the thread reply included -- must reflect the failure,
    # and none may still carry the stale pre-grant wording.
    assert any("error occurred" in (c.kwargs.get("text") or "").lower() for c in client.chat_postMessage.call_args_list)
    assert not any("will be approved automatically" in (c.kwargs.get("text") or "").lower() for c in client.chat_postMessage.call_args_list)


def test_process_access_request_posts_granted_message_on_success(main_module):
    """Companion to the failure-path test above: the reordering must not
    break the ordinary success path -- execute_decision succeeding still
    results in the "Permissions granted" follow-up being posted."""
    request = main_module.slack_helpers.RequestForAccess(
        account_id="111111111111",
        permission_set_name="FullOrgAdmin",
        reason="testing",
        requester_slack_id="U_REQ",
        permission_duration=main_module.timedelta(hours=1),
    )
    requester = MagicMock(id="U_REQ", email="email@domen.com", real_name="Test User")
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"blocks": []}}
    client.conversations_members.return_value = MagicMock(data={"members": []})
    client.users_info.return_value = MagicMock(
        data={"user": {"id": "U_REQ", "profile": {"email": "email@domen.com"}, "real_name": "Test User"}}
    )

    fake_decision = main_module.access_control.AccessRequestDecision(
        grant=True,
        reason=main_module.access_control.DecisionReason.SelfApproval,
        based_on_statements=frozenset(),
        approvers=frozenset(),
    )
    fake_account = main_module.entities.aws.Account(id="111111111111", name="test-account")

    with (
        patch.object(main_module.access_control, "make_decision_on_access_request", return_value=fake_decision),
        patch.object(main_module.organizations, "describe_account", return_value=fake_account),
        patch.object(main_module.slack_helpers.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(main_module.access_control, "execute_decision", return_value=True),
    ):
        result, succeeded = main_module.process_access_request(request=request, requester=requester, client=client)

    assert result is fake_decision
    assert succeeded is True
    assert any("permissions granted" in (c.kwargs.get("text") or "").lower() for c in client.chat_postMessage.call_args_list)


def test_process_access_request_reports_not_succeeded_when_requires_approval_finds_no_approvers_in_slack(main_module):
    """Regression test: a RequiresApproval decision whose approver emails
    don't resolve to any real Slack user (find_approvers_in_slack returns an
    empty list) leaves decision.reason == RequiresApproval -- that's still
    the correct *policy* reason -- even though the request was never
    actually posted for anyone to approve. `succeeded` must reflect that
    (derived from color_coding_emoji, same as every other branch), not just
    mirror decision.reason, or a caller like the CLI has no way to tell this
    case apart from a genuinely pending approval."""
    request = main_module.slack_helpers.RequestForAccess(
        account_id="111111111111",
        permission_set_name="FullOrgAdmin",
        reason="testing",
        requester_slack_id="U_REQ",
        permission_duration=main_module.timedelta(hours=1),
    )
    requester = MagicMock(id="U_REQ", email="email@domen.com", real_name="Test User")
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "123.456", "message": {"blocks": []}}
    client.conversations_members.return_value = MagicMock(data={"members": []})
    client.users_info.return_value = MagicMock(
        data={"user": {"id": "U_REQ", "profile": {"email": "email@domen.com"}, "real_name": "Test User"}}
    )

    fake_decision = main_module.access_control.AccessRequestDecision(
        grant=False,
        reason=main_module.access_control.DecisionReason.RequiresApproval,
        based_on_statements=frozenset(),
        approvers=frozenset(["approver@example.com"]),
    )
    fake_account = main_module.entities.aws.Account(id="111111111111", name="test-account")

    with (
        patch.object(main_module.access_control, "make_decision_on_access_request", return_value=fake_decision),
        patch.object(main_module.organizations, "describe_account", return_value=fake_account),
        patch.object(main_module.slack_helpers.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(main_module.slack_helpers, "find_approvers_in_slack", return_value=([], ["approver@example.com"])),
        patch.object(main_module.access_control, "execute_decision", return_value=False),
    ):
        result, succeeded = main_module.process_access_request(request=request, requester=requester, client=client)

    assert result is fake_decision
    assert result.reason == main_module.access_control.DecisionReason.RequiresApproval
    assert succeeded is False
    assert any("cannot be processed" in (c.kwargs.get("text") or "").lower() for c in client.chat_update.call_args_list)


# ---------------------------------------------------------------------------
# handle_button_click
# ---------------------------------------------------------------------------
#
# No coverage existed for this function before -- it's the approver-clicks-
# Approve path, and it had the exact same "recolor the message green before
# the grant is attempted" bug process_access_request did, independently,
# since the two functions don't share that part of their code. See
# test_process_access_request_reflects_a_grant_failure_instead_of_claiming_success
# above for the sibling bug on the self-approval/CLI path.


def _button_click_content_fields() -> list[dict]:
    return [
        {"text": "Requester: <@U_REQ>"},
        {"text": "Account: 111111111111 #111111111111"},
        {"text": "Role name: FullOrgAdmin"},
        {"text": "Reason: testing"},
        {"text": "Permission duration: 1h 0m"},
    ]


def _button_click_body() -> dict:
    return {
        "actions": [{"value": "approve"}],
        "user": {"id": "U_APPROVER"},
        "message": {"ts": "12345.6789", "blocks": [{"block_id": "content", "fields": _button_click_content_fields()}]},
        "channel": {"id": "C123"},
    }


def test_handle_button_click_reflects_a_grant_failure_instead_of_claiming_success(main_module):
    """Regression test: chat_update (recoloring the message green and
    saying "Permissions granted to @x by @y") previously ran before
    execute_decision -- a failure there (a stale/bogus permission set name,
    IAM Identity Center throttling, anything) left that message
    permanently incorrect, with no visible correction (@handle_errors'
    generic error post is a separate message, not a fix to this one).
    execute_decision now runs first, so a failure can override the message
    before it's ever posted."""
    body = _button_click_body()
    approver = MagicMock(id="U_APPROVER", email="approver@example.com")
    requester = MagicMock(id="U_REQ", email="req@example.com")
    client = MagicMock()
    client.conversations_members.return_value = MagicMock(data={"members": []})

    fake_decision = main_module.access_control.ApproveRequestDecision(grant=True, permit=True, based_on_statements=frozenset())

    with (
        patch.object(main_module.slack_helpers, "get_user", side_effect=[approver, requester]),
        patch.object(main_module.access_control, "make_decision_on_approve_request", return_value=fake_decision),
        patch.object(main_module.access_control, "execute_decision", side_effect=RuntimeError("boom: permission set not found")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        main_module.handle_button_click.__wrapped__(body=body, client=client, context={})

    update_call = client.chat_update.call_args
    assert update_call is not None, "chat_update should still have run, with the failure reflected in it"
    assert "error occurred" in update_call.kwargs["text"].lower()
    assert not any("permissions granted" in (c.kwargs.get("text") or "").lower() for c in client.chat_postMessage.call_args_list)
    # The dedup cache must be cleared even on a caught failure -- otherwise
    # this exact request is permanently stuck reporting "already in
    # progress" to any retry.
    assert main_module.cache_for_dublicate_requests == {}


def test_handle_button_click_posts_granted_message_on_success(main_module):
    """Companion to the failure-path test above: the reordering must not
    break the ordinary success path."""
    body = _button_click_body()
    approver = MagicMock(id="U_APPROVER", email="approver@example.com")
    requester = MagicMock(id="U_REQ", email="req@example.com")
    client = MagicMock()
    client.conversations_members.return_value = MagicMock(data={"members": []})

    fake_decision = main_module.access_control.ApproveRequestDecision(grant=True, permit=True, based_on_statements=frozenset())

    with (
        patch.object(main_module.slack_helpers, "get_user", side_effect=[approver, requester]),
        patch.object(main_module.access_control, "make_decision_on_approve_request", return_value=fake_decision),
        patch.object(main_module.access_control, "execute_decision", return_value=True),
    ):
        result = main_module.handle_button_click.__wrapped__(body=body, client=client, context={})

    update_call = client.chat_update.call_args
    assert update_call is not None
    assert "permissions granted" in update_call.kwargs["text"].lower()
    assert result is not None
    assert main_module.cache_for_dublicate_requests == {}


def test_handle_button_click_notification_failure_after_a_successful_grant_is_not_reported_as_failure(main_module):
    """Regression test for the reorder's own regression: once
    execute_decision succeeds, a Slack API failure while posting/updating
    the resulting notifications must not propagate -- that would report
    "this failed" for a request that actually succeeded, the inverted-truth
    outcome the whole reordering exists to prevent, just from the
    notification side rather than the ordering side."""
    body = _button_click_body()
    approver = MagicMock(id="U_APPROVER", email="approver@example.com")
    requester = MagicMock(id="U_REQ", email="req@example.com")
    client = MagicMock()
    client.conversations_members.return_value = MagicMock(data={"members": []})
    client.chat_update.side_effect = RuntimeError("Slack is briefly unavailable")

    fake_decision = main_module.access_control.ApproveRequestDecision(grant=True, permit=True, based_on_statements=frozenset())

    with (
        patch.object(main_module.slack_helpers, "get_user", side_effect=[approver, requester]),
        patch.object(main_module.access_control, "make_decision_on_approve_request", return_value=fake_decision),
        patch.object(main_module.access_control, "execute_decision", return_value=True),
    ):
        # Must not raise: the grant succeeded, only the follow-up
        # notification failed.
        result = main_module.handle_button_click.__wrapped__(body=body, client=client, context={})

    assert result is None
