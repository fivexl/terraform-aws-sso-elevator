"""Identity checks for the CLI access-request path.

cli_proof establishes the caller's ARN through STS; this module decides whether that identity may
act. Trust model and its limits: README "CLI tool".
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import botocore.exceptions

import config
import errors
import sso

logger = config.get_logger(service="cli_auth")

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_organizations import OrganizationsClient
    from mypy_boto3_s3 import S3Client

_ASSUMED_ROLE_ARN_RE = re.compile(r"arn:aws:sts::\d{12}:assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)")

# IAM reserves this role-name prefix for IAM Identity Center in every account (README "CLI tool").
SSO_ROLE_NAME_PREFIX = "AWSReservedSSO_"


class TransientAWSError(Exception):
    """Raised when an Identity Store or Organizations lookup fails for a reason that says nothing
    about whether the caller's identity is valid (throttling, a 5xx, a connectivity failure).
    Callers surface it as a 503 the CLI can retry, not as GENERIC_REJECTION or a 500."""


GENERIC_REJECTION = {
    "statusCode": 403,
    "headers": {"content-type": "application/json"},
    "body": json.dumps(
        {
            "message": (
                "Request rejected: could not verify your identity. Check that you are signed in with AWS SSO, "
                "your system clock is correct, and the endpoint and API id are right."
            )
        }
    ),
}


@contextmanager
def _transient_as_retryable() -> Iterator[None]:
    """Turn a throttle, 5xx or connectivity failure into TransientAWSError; other ClientErrors propagate."""
    try:
        yield
    except botocore.exceptions.ClientError as e:
        if sso.is_transient_aws_error(e):
            raise TransientAWSError from e
        raise
    except botocore.exceptions.BotoCoreError as e:
        raise TransientAWSError from e


def caller_account_in_organization(org_client: "OrganizationsClient", account_id: str) -> bool:
    """Whether account_id belongs to this deployment's organization. STS vouches for any AWS
    account, so a session from another organization must stop here. Fails closed."""
    with _transient_as_retryable():
        try:
            org_client.describe_account(AccountId=account_id)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") == "AccountNotFoundException":
                return False
            raise
    return True


def extract_identity(
    user_arn: str, identity_store_client: "IdentityStoreClient", identity_store_id: str, s3_client: "S3Client"
) -> tuple[str, str, dict] | None:
    """Return (email, UserId, list_users snapshot) for an IAM Identity Center session whose
    session name exactly matches one Identity Store UserName, else None. The UserId and snapshot
    let the caller cross-check a later email lookup without another list_users scan."""
    cfg = config.get_config()
    match = _ASSUMED_ROLE_ARN_RE.fullmatch(user_arn)
    if not match or not match["role_name"].startswith(SSO_ROLE_NAME_PREFIX):
        return None

    # A cold cache (or caching disabled) re-raises the raw error.
    with _transient_as_retryable():
        list_of_users = sso.list_users_with_cache(identity_store_client, identity_store_id, s3_client, cfg)
    try:
        found = sso.find_email_by_username(list_of_users, match["session_name"])
    except errors.AmbiguousSSOUser:
        logger.warning(
            "Rejected CLI request: session name matches more than one Identity Store user", extra={"session_name": match["session_name"]}
        )
        return None
    if found is None:
        return None
    email, user_id = found
    return email, user_id, list_of_users
