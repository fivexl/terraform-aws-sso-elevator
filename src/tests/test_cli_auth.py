import cli_auth

# Matches conftest.py's mock_env: cli_expected_account_id="111111111111",
# cli_sso_role_name_prefix="AWSReservedSSO_".
EMAIL_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/requester@example.com"
NO_EMAIL_ARN = "arn:aws:sts::111111111111:assumed-role/SomeRole/i-0abc123def456"

# Correct account and SSO role prefix, but no '@' in the session name -- this ARN
# tests the account+prefix checks pass while the email check fails on its own.
WRONG_ACCOUNT_ARN = "arn:aws:sts::999999999999:assumed-role/AWSReservedSSO_FullOrgAdmin_bb7a6d8b5397bb50/attacker@evil.com"

# Correct account and an '@' in the session name, but not an SSO-provisioned role.
NON_SSO_ROLE_ARN = "arn:aws:sts::111111111111:assumed-role/SomeOtherRole/attacker@evil.com"

NOT_ASSUMED_ROLE_ARN = "arn:aws:iam::111111111111:user/requester@example.com"


def test_extract_identity_accepts_valid_sso_session():
    assert cli_auth.extract_identity(EMAIL_ARN) == "requester@example.com"


def test_extract_identity_rejects_missing_email():
    assert cli_auth.extract_identity(NO_EMAIL_ARN) is None


def test_extract_identity_rejects_wrong_account_even_with_email():
    assert cli_auth.extract_identity(WRONG_ACCOUNT_ARN) is None


def test_extract_identity_rejects_non_sso_role_even_with_email():
    assert cli_auth.extract_identity(NON_SSO_ROLE_ARN) is None


def test_extract_identity_rejects_non_assumed_role_arn():
    assert cli_auth.extract_identity(NOT_ASSUMED_ROLE_ARN) is None


def test_extract_identity_rejects_empty_string():
    assert cli_auth.extract_identity("") is None
