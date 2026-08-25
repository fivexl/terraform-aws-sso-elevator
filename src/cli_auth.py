"""Identity verification for the CLI access-request path.

The CLI signs its request directly against API Gateway (SigV4, an AWS_IAM
authorizer) instead of going through Slack. API Gateway verifies the
signature itself and populates requestContext.authorizer.iam.userArn with
the caller's verified identity before the Lambda ever runs — extract_identity
here doesn't verify a signature, it decides whether that already-verified
identity is trustworthy enough to act on: a real SSO session, in the expected
account, with an email in it.
"""

import json
import re

import config

_ASSUMED_ROLE_ARN_RE = re.compile(r"^arn:aws:sts::(?P<account_id>\d{12}):assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)$")

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


def extract_identity(user_arn: str) -> str | None:
    """Return the requester's email, but only if user_arn is an assumed-role
    session in the expected account, under an SSO-provisioned role, with an
    email as the session name. A spoofed ARN that satisfies only one or two
    of those checks — wrong account, non-SSO role, or no '@' — is rejected."""
    cfg = config.get_config()
    match = _ASSUMED_ROLE_ARN_RE.match(user_arn)
    if not match:
        return None
    # No cli_expected_account_id configured means match["account_id"] (always a
    # 12-digit string from the regex) can never equal it — every CLI request is
    # rejected until an operator explicitly sets it. Fails closed, not open.
    if match["account_id"] != cfg.cli_expected_account_id:
        return None
    if not match["role_name"].startswith(cfg.cli_sso_role_name_prefix):
        return None

    session_name = match["session_name"]
    if "@" not in session_name:
        return None
    return session_name
