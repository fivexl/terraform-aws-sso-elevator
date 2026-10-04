"""Identity check for the CLI access-request path.

API Gateway's AWS_IAM authorizer verifies the signature; this module decides whether that identity
may act. Trust model and its limits: README "CLI tool".
"""

import json
import re
from typing import TYPE_CHECKING

import botocore.exceptions

import config
import errors
import sso

logger = config.get_logger(service="cli_auth")

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_s3 import S3Client

_ASSUMED_ROLE_ARN_RE = re.compile(r"arn:aws:sts::\d{12}:assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)")

# IAM reserves this role-name prefix for IAM Identity Center in every account (README "CLI tool").
SSO_ROLE_NAME_PREFIX = "AWSReservedSSO_"


class TransientIdentityStoreError(Exception):
    """Raised when the Identity Store lookup fails for a reason that says nothing about
    whether the caller's identity is valid (throttling, a 5xx, a connectivity failure).
    Callers surface it as a 503 the CLI can retry, not as GENERIC_REJECTION or a 500."""


GENERIC_REJECTION = {
    "statusCode": 403,
    "headers": {"content-type": "application/json"},
    "body": json.dumps(
        {
            "message": (
                "The credentials provided are not associated with an SSO session. Please sign in using your AWS SSO session and try again."
            )
        }
    ),
}


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

    # A cold cache (or caching disabled) re-raises the raw error. A throttle, 5xx or
    # connectivity failure says nothing about the caller, so it becomes a retryable 503.
    # Other ClientErrors (e.g. a missing identitystore:ListUsers permission) propagate.
    try:
        list_of_users = sso.list_users_with_cache(identity_store_client, identity_store_id, s3_client, cfg)
    except botocore.exceptions.ClientError as e:
        if sso.is_transient_aws_error(e):
            raise TransientIdentityStoreError from e
        raise
    except botocore.exceptions.BotoCoreError as e:
        raise TransientIdentityStoreError from e
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
