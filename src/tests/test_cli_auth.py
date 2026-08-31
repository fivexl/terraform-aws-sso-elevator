from types import SimpleNamespace
from unittest.mock import patch

import botocore.exceptions
import pytest

import cli_auth

# Matches conftest.py's mock_env: cli_expected_account_id="111111111111",
# cli_sso_role_name_prefix="AWSReservedSSO_".
EMAIL_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/requester@example.com"

# AD-style username, no '@' at all -- the exact case the identity-store
# lookup exists to handle: RoleSessionName is the Identity Store username,
# not necessarily an email.
USERNAME_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/jsmith"

# A session name that doesn't match any real Identity Store user -- e.g. a
# long email truncated by RoleSessionName's 64-character limit.
UNMATCHED_SESSION_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/i-0abc123def456"

# Correct account and a name-prefix match, but the session name resolves to
# nothing -- this ARN tests the account+role checks pass while the identity
# resolution fails on its own.
WRONG_ACCOUNT_ARN = "arn:aws:sts::999999999999:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/attacker@evil.com"

# Correct account and a session name, but the role name itself doesn't even
# have the expected prefix -- rejected before get_role is ever called.
NON_SSO_ROLE_ARN = "arn:aws:sts::111111111111:assumed-role/SomeOtherRole/attacker@evil.com"

# The actual bypass this module exists to close: a role name with the right
# prefix -- satisfying the old, insufficient check -- but not actually
# provisioned by IAM Identity Center (i.e. not at the reserved path), which
# is exactly what anyone with iam:CreateRole could produce themselves.
SPOOFED_PREFIX_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Spoofed/attacker@evil.com"

NOT_ASSUMED_ROLE_ARN = "arn:aws:iam::111111111111:user/requester@example.com"

RESERVED_PATH = "/aws-reserved/sso.amazonaws.com/"
# Multi-region (or non-us-east-1 identity source) deployments add a region
# segment to the reserved path -- still a genuine SSO role.
RESERVED_PATH_WITH_REGION = "/aws-reserved/sso.amazonaws.com/eu-central-1/"

IDENTITY_STORE_ID = "d-1234567890"


def _get_role_response(path: str) -> dict:
    return {"Role": {"Path": path, "RoleName": "irrelevant-for-this-test"}}


def _access_denied() -> botocore.exceptions.ClientError:
    # What this Lambda's own iam:GetRole call actually gets back for a role
    # outside the reserved path, since slack_handler_lambda.tf's policy
    # scopes that permission to the reserved-path resource shape only.
    return botocore.exceptions.ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
        operation_name="GetRole",
    )


def _no_such_entity() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        error_response={"Error": {"Code": "NoSuchEntity", "Message": "role not found"}},
        operation_name="GetRole",
    )


def extract_identity(user_arn: str, identity_store_client=None):
    """extract_identity always takes an identity store client + id now --
    this wrapper lets most call sites in this file omit the boilerplate."""
    return cli_auth.extract_identity(user_arn, identity_store_client, IDENTITY_STORE_ID)


@pytest.fixture(autouse=True)
def mock_iam_client():
    """Stubs cli_auth's iam:GetRole call so tests don't hit real AWS. Each
    test below configures return_value/side_effect on the fixture's mock
    directly, since which role gets looked up (and what path it "has")
    varies per test."""
    with patch.object(cli_auth, "_iam_client") as mock_client:
        yield mock_client


@pytest.fixture(autouse=True)
def mock_find_email_by_username():
    """Stubs the Identity Store username-to-email lookup so tests don't hit
    real AWS. Defaults to "no match" (None); tests that need a successful
    resolution set return_value explicitly."""
    with patch.object(cli_auth.sso, "find_email_by_username", return_value=None) as mock_find:
        yield mock_find


def test_extract_identity_accepts_valid_sso_session(mock_iam_client, mock_find_email_by_username):
    mock_iam_client.get_role.return_value = _get_role_response(RESERVED_PATH)
    mock_find_email_by_username.return_value = "requester@example.com"

    assert extract_identity(EMAIL_ARN) == "requester@example.com"
    mock_find_email_by_username.assert_called_once_with(None, IDENTITY_STORE_ID, "requester@example.com")


def test_extract_identity_accepts_valid_sso_session_with_region_segment(mock_iam_client, mock_find_email_by_username):
    """Multi-region / non-us-east-1-identity-source deployments provision
    roles with an extra region segment in the path -- must still be
    accepted, not just the no-region shape."""
    mock_iam_client.get_role.return_value = _get_role_response(RESERVED_PATH_WITH_REGION)
    mock_find_email_by_username.return_value = "requester@example.com"

    assert extract_identity(EMAIL_ARN) == "requester@example.com"


def test_extract_identity_accepts_ad_style_username_with_no_at_sign(mock_iam_client, mock_find_email_by_username):
    """The fix this module exists for: RoleSessionName being an AD-style
    username (no '@' at all) is a legitimate SSO session, not something to
    reject outright -- it must be resolved through the Identity Store,
    same as any other username."""
    mock_iam_client.get_role.return_value = _get_role_response(RESERVED_PATH)
    mock_find_email_by_username.return_value = "j.smith@company.com"

    assert extract_identity(USERNAME_ARN) == "j.smith@company.com"
    mock_find_email_by_username.assert_called_once_with(None, IDENTITY_STORE_ID, "jsmith")


def test_extract_identity_rejects_session_name_matching_no_user(mock_iam_client):
    """A session name that doesn't resolve to any real Identity Store user
    -- e.g. a long email truncated by RoleSessionName's 64-character limit
    -- is rejected. This is a known, accepted limitation (see
    sso.find_email_by_username's docstring): it deliberately does not fall
    back to a fuzzy/prefix match, since that could resolve to the wrong
    person."""
    mock_iam_client.get_role.return_value = _get_role_response(RESERVED_PATH)
    # mock_find_email_by_username's default return_value (None) already
    # models "no match" -- nothing to configure.

    assert extract_identity(UNMATCHED_SESSION_ARN) is None


def test_extract_identity_rejects_wrong_account_even_with_valid_session_name(mock_iam_client, mock_find_email_by_username):
    assert extract_identity(WRONG_ACCOUNT_ARN) is None
    # Rejected on the account check alone -- neither AWS call should be reached.
    mock_iam_client.get_role.assert_not_called()
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_everything_when_account_id_unset(mock_iam_client, mock_find_email_by_username):
    """Regression test for the "fails closed by default" claim: with
    cli_expected_account_id left at its "" default (as it is for any
    deployment/test that hasn't set it), every request is rejected --
    including one with an otherwise-perfectly-valid ARN. This is the
    Python-level default's actual behavior; it is NOT what a real
    Terraform deployment does by default (see the comments on
    cli_expected_account_id in config.py and cli_auth.py)."""
    with patch.object(cli_auth.config, "get_config") as mock_get_config:
        mock_get_config.return_value = SimpleNamespace(cli_expected_account_id="", cli_sso_role_name_prefix="AWSReservedSSO_")
        assert extract_identity(EMAIL_ARN) is None
    mock_iam_client.get_role.assert_not_called()
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_non_sso_role_even_with_valid_session_name(mock_iam_client, mock_find_email_by_username):
    assert extract_identity(NON_SSO_ROLE_ARN) is None
    # Rejected on the name-prefix check alone -- neither AWS call should be reached.
    mock_iam_client.get_role.assert_not_called()
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_role_with_matching_prefix_but_wrong_path(mock_iam_client, mock_find_email_by_username):
    """The bypass this fix closes: role_name.startswith(prefix) alone used
    to be sufficient. A role anyone could create with iam:CreateRole --
    right name, wrong (non-reserved) path -- must now be rejected. Covers
    the case where get_role somehow still succeeds despite the path
    mismatch (defense in depth on top of the AccessDenied case below)."""
    mock_iam_client.get_role.return_value = _get_role_response("/")

    assert extract_identity(SPOOFED_PREFIX_ARN) is None
    mock_iam_client.get_role.assert_called_once_with(RoleName="AWSReservedSSO_Spoofed")
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_role_get_role_access_denied(mock_iam_client, mock_find_email_by_username):
    """The realistic outcome for a spoofed (non-reserved-path) role: this
    Lambda's own iam:GetRole permission is scoped to the reserved path, so
    AWS itself refuses the read rather than returning the role's real
    (non-matching) path."""
    mock_iam_client.get_role.side_effect = _access_denied()

    assert extract_identity(SPOOFED_PREFIX_ARN) is None
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_role_that_no_longer_exists(mock_iam_client, mock_find_email_by_username):
    mock_iam_client.get_role.side_effect = _no_such_entity()

    assert extract_identity(EMAIL_ARN) is None
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_non_assumed_role_arn(mock_iam_client, mock_find_email_by_username):
    assert extract_identity(NOT_ASSUMED_ROLE_ARN) is None
    mock_iam_client.get_role.assert_not_called()
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_rejects_empty_string(mock_iam_client, mock_find_email_by_username):
    assert extract_identity("") is None
    mock_iam_client.get_role.assert_not_called()
    mock_find_email_by_username.assert_not_called()
