"""Identity verification for the CLI access-request path.

The CLI signs its request directly against API Gateway (SigV4, an AWS_IAM
authorizer) instead of going through Slack. API Gateway verifies the
signature itself and populates requestContext.authorizer.iam.userArn with
the caller's verified identity before the Lambda ever runs — extract_identity
here doesn't verify a signature, it decides whether that already-verified
identity is trustworthy enough to act on: a real SSO session, in the expected
account, under a genuinely SSO-provisioned role, resolved to a real
registered email via an Identity Store lookup.

An assumed-role ARN (arn:aws:sts::<account>:assumed-role/<role-name>/<session>)
never carries the underlying role's IAM path, only its name — and a role's
*name* is not an AWS-enforced signal at all; anyone with iam:CreateRole can
name a role of their own AWSReservedSSO_Anything. What AWS actually protects
is the *path* IAM Identity Center provisions its roles under
(/aws-reserved/sso.amazonaws.com/), which is why this looks the role up via
iam:GetRole rather than trusting the name alone.

Separately, the session name (RoleSessionName) is set by IAM Identity Center
to the caller's Identity Store *username*, not necessarily their email —
that's only true when the identity source's username happens to be an email
(e.g. a plain AD sAMAccountName or a long email truncated to
RoleSessionName's 64-character limit are both real, valid usernames that
aren't literally an email string). Treating it as the email directly would
wrongly reject those legitimate sessions, so this looks the real email up
from the Identity Store instead of parsing the session name as one.
"""

import json
import re
from typing import TYPE_CHECKING

import boto3
import botocore.exceptions

import config
import sso

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient

_ASSUMED_ROLE_ARN_RE = re.compile(r"^arn:aws:sts::(?P<account_id>\d{12}):assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)$")

# The path prefix IAM Identity Center provisions its own roles under — this
# is the part of a role's identity AWS itself enforces (documented as
# "protected, only modifiable by AWS"), unlike the role's name, which anyone
# with iam:CreateRole can imitate. A prefix, not an exact path, because a
# multi-region (or non-us-east-1 identity source) deployment adds a region
# segment: .../sso.amazonaws.com/<region>/AWSReservedSSO_... — the same
# reason slack_handler_lambda.tf's own IAM policy lists both resource shapes.
_SSO_RESERVED_ROLE_PATH_PREFIX = "/aws-reserved/sso.amazonaws.com/"

_iam_client = boto3.Session().client("iam")

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


def extract_identity(user_arn: str, identity_store_client: "IdentityStoreClient", identity_store_id: str) -> str | None:
    """Return the requester's real, registered email, but only if user_arn is
    an assumed-role session in the expected account, under a role that's
    genuinely IAM Identity Center-provisioned (verified via its real IAM
    path, not just its name), whose session name resolves to an Identity
    Store user by exact username match. A spoofed ARN, or a role session
    whose name doesn't match any real username, is rejected."""
    cfg = config.get_config()
    match = _ASSUMED_ROLE_ARN_RE.match(user_arn)
    if not match:
        return None
    # cli_expected_account_id is always the account this module is deployed
    # into (see locals.tf) — not operator-configurable, precisely because
    # this check needs to agree with _is_sso_provisioned_role's iam:GetRole
    # call below, which can only ever resolve against that same account.
    if match["account_id"] != cfg.cli_expected_account_id:
        return None

    role_name = match["role_name"]
    if not role_name.startswith(cfg.cli_sso_role_name_prefix):
        return None
    if not _is_sso_provisioned_role(role_name):
        return None

    # session_name is the Identity Store username IAM Identity Center set
    # RoleSessionName to, not necessarily an email itself -- see the module
    # docstring. Resolving it through the Identity Store, rather than
    # returning it directly, is what makes an AD-style username (no '@' at
    # all) work here the same way it already does on the Slack path.
    list_of_users = sso.list_users(identity_store_client, identity_store_id)
    return sso.find_email_by_username(list_of_users, match["session_name"])


def _is_sso_provisioned_role(role_name: str) -> bool:
    """Whether role_name is a real IAM role at IAM Identity Center's own
    reserved path. This is the part role_name.startswith(prefix) can't
    check: an assumed-role ARN never carries the role's path, only its
    name, so a role created (with iam:CreateRole) at any ordinary path but
    given a matching name would pass a name-only check. iam:GetRole looks
    the role up directly to get its real path.

    This Lambda's own iam:GetRole permission (slack_handler_lambda.tf) is
    itself scoped to only the reserved-path resource shape, so a role at
    any other path 403s here rather than returning its real (non-matching)
    path — caught the same as any other lookup failure, since either way
    the answer is "not a genuine SSO role"."""
    try:
        role = _iam_client.get_role(RoleName=role_name)["Role"]
    except botocore.exceptions.ClientError:
        # Covers both "no such role" (deleted between assuming it and this
        # call) and "access denied" (this Lambda's own IAM policy already
        # refuses to let it read a role outside the reserved path) —
        # either way, reject rather than assume.
        return False
    return role["Path"].startswith(_SSO_RESERVED_ROLE_PATH_PREFIX)
