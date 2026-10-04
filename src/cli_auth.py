"""Identity verification for the CLI access-request path.

The CLI signs its request directly against API Gateway (SigV4, an AWS_IAM
authorizer). API Gateway verifies the signature and puts the caller's
identity at requestContext.identity.userArn before the Lambda runs.
extract_identity doesn't verify a signature; it decides whether that
identity is trustworthy enough to act on: an IAM Identity Center session
whose session name resolves to a real Identity Store user.

The caller's account is not checked here. The REST API's resource policy
(aws:PrincipalOrgID) makes API Gateway reject callers outside the AWS
Organization before this code runs.

The remaining residual risk is a direct lambda:InvokeFunction call: the
invoker controls the whole event, including requestContext.identity, so
this check is only as strong as the restriction on who may invoke the
Lambda (see the README's CLI section).

IAM Identity Center sets the session name (RoleSessionName) to the caller's
Identity Store username, which is not always an email (an AD sAMAccountName
is a valid username). So the session name is matched exactly against
UserName and the email is read from that user's record.

A username truncated by RoleSessionName's 64-character limit does not match
exactly and is rejected. That is an accepted, fail-closed limitation.
"""

import json
import re
from typing import TYPE_CHECKING

import botocore.exceptions

import config
import sso

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_s3 import S3Client

# Matches all three real AWS partitions (aws, aws-cn, aws-us-gov) -- a
# hardcoded "aws" would reject every request outside the standard partition
# with the same generic message a genuinely invalid ARN gets.
_ASSUMED_ROLE_ARN_RE = re.compile(r"^arn:(?:aws|aws-cn|aws-us-gov):sts::\d{12}:assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)$")

# IAM reserves role names starting with this prefix in every account: CreateRole fails with
# "The role name ... is reserved for AWS use", even for an administrator. So the name alone
# proves the role was provisioned by IAM Identity Center, in any account, with no IAM call.
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
    """Return the requester's (email, UserId, the full list_users() snapshot they were matched
    against), or None. user_arn must be an assumed-role session under a role named with
    SSO_ROLE_NAME_PREFIX whose session name exactly matches one Identity Store UserName.

    The UserId is the one this session was verified against, so a caller can cross-check a
    later email-based lookup against it. The snapshot lets that cross-check reuse this scan
    instead of paying for another full paginated list_users."""
    cfg = config.get_config()
    match = _ASSUMED_ROLE_ARN_RE.match(user_arn)
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
    found = sso.find_email_by_username(list_of_users, match["session_name"])
    if found is None:
        return None
    email, user_id = found
    return email, user_id, list_of_users
