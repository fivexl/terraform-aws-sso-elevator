from unittest.mock import patch

import botocore.exceptions
import pytest

import cli_auth

EMAIL_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/requester@example.com"

# AD-style username with no '@': the session name is the Identity Store username, not an email.
USERNAME_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/jsmith"

# A session name that matches no Identity Store user, e.g. a truncated long username.
UNMATCHED_SESSION_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/i-0abc123def456"

OTHER_ACCOUNT_ARN = "arn:aws:sts::222222222222:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/requester@example.com"

NON_SSO_ROLE_ARN = "arn:aws:sts::111111111111:assumed-role/SomeOtherRole/requester@example.com"

# Close to the reserved prefix but not it; IAM lets anyone create these names.
LOOKALIKE_PREFIX_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSOAdmin/requester@example.com"

NOT_ASSUMED_ROLE_ARN = "arn:aws:iam::111111111111:user/requester@example.com"

IDENTITY_STORE_ID = "d-1234567890"


def _client_error(code: str, status: int = 400) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        error_response={"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation_name="ListUsers",
    )


def extract_identity(user_arn: str):
    return cli_auth.extract_identity(user_arn, None, IDENTITY_STORE_ID, None)


@pytest.fixture(autouse=True)
def mock_list_users():
    """Patches list_users_with_cache as a whole; its cache behavior is covered in test_cache.py/test_sso.py."""
    with patch.object(cli_auth.sso, "list_users_with_cache", return_value={"Users": []}) as mock_list:
        yield mock_list


@pytest.fixture(autouse=True)
def mock_find_email_by_username():
    """Defaults to "no match"; tests that need a resolved user set return_value."""
    with patch.object(cli_auth.sso, "find_email_by_username", return_value=None) as mock_find:
        yield mock_find


def test_extract_identity_accepts_valid_sso_session(mock_find_email_by_username):
    mock_find_email_by_username.return_value = ("requester@example.com", "u-1")

    assert extract_identity(EMAIL_ARN) == ("requester@example.com", "u-1", {"Users": []})
    mock_find_email_by_username.assert_called_once_with({"Users": []}, "requester@example.com")


def test_extract_identity_accepts_caller_from_another_account(mock_find_email_by_username):
    mock_find_email_by_username.return_value = ("requester@example.com", "u-1")

    assert extract_identity(OTHER_ACCOUNT_ARN) == ("requester@example.com", "u-1", {"Users": []})


@pytest.mark.parametrize("partition", ["aws-us-gov", "aws-cn"])
def test_extract_identity_accepts_other_partitions(partition, mock_find_email_by_username):
    mock_find_email_by_username.return_value = ("requester@example.com", "u-1")
    arn = EMAIL_ARN.replace("arn:aws:", f"arn:{partition}:")

    assert extract_identity(arn) == ("requester@example.com", "u-1", {"Users": []})


def test_extract_identity_accepts_ad_style_username_with_no_at_sign(mock_find_email_by_username):
    mock_find_email_by_username.return_value = ("j.smith@company.com", "u-2")

    assert extract_identity(USERNAME_ARN) == ("j.smith@company.com", "u-2", {"Users": []})
    mock_find_email_by_username.assert_called_once_with({"Users": []}, "jsmith")


def test_extract_identity_rejects_session_name_matching_no_user():
    assert extract_identity(UNMATCHED_SESSION_ARN) is None


@pytest.mark.parametrize("arn", [NON_SSO_ROLE_ARN, LOOKALIKE_PREFIX_ARN, NOT_ASSUMED_ROLE_ARN, ""])
def test_extract_identity_rejects_non_sso_identity_without_identity_store_lookup(arn, mock_list_users, mock_find_email_by_username):
    mock_find_email_by_username.return_value = ("requester@example.com", "u-1")

    assert extract_identity(arn) is None
    mock_list_users.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        _client_error("Throttling"),
        # A 5xx code outside the transient allowlist; caught by the HTTP-status fallback.
        _client_error("SomeUnmappedServiceError", status=503),
        botocore.exceptions.EndpointConnectionError(endpoint_url="https://identitystore.amazonaws.com"),
    ],
)
def test_extract_identity_raises_transient_error_on_identity_store_failure(error, mock_list_users, mock_find_email_by_username):
    mock_list_users.side_effect = error

    with pytest.raises(cli_auth.TransientIdentityStoreError):
        extract_identity(EMAIL_ARN)
    mock_find_email_by_username.assert_not_called()


def test_extract_identity_propagates_non_transient_identity_store_error(mock_list_users):
    """A non-transient error such as a missing identitystore:ListUsers permission is a
    misconfiguration, so it propagates instead of becoming a rejection or a retry."""
    mock_list_users.side_effect = _client_error("AccessDeniedException")

    with pytest.raises(botocore.exceptions.ClientError):
        extract_identity(EMAIL_ARN)
