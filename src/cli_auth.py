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
(a plain AD sAMAccountName is a real, valid username that isn't literally an
email string). Treating it as the email directly would wrongly reject a
legitimate sAMAccountName-style session, so this looks the real email up
from the Identity Store by exact username match instead of parsing the
session name as one.

This exact-match lookup does NOT help a username long enough to get
truncated by RoleSessionName's 64-character limit -- the truncated string
won't exactly match the real (longer) UserName either, so that case is
correctly rejected rather than "handled" (see
test_find_email_by_username_does_not_prefix_match_a_truncated_username in
test_sso.py, and find_email_by_username's own docstring). That's an
intentional, accepted, fail-closed limitation, not a bug.
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

# Matches all three real AWS partitions (aws, aws-cn, aws-us-gov) -- a
# hardcoded "aws" would reject 100% of requests in GovCloud/China with the
# same generic "not associated with an SSO session" message a genuinely
# invalid ARN gets, giving an operator there nothing to go on.
#
# This alone does NOT make GovCloud/China actually work end-to-end, though:
# the module's IAM policies (slack_handler_lambda.tf's iam:GetRole
# resources, locals.tf's Lambda/S3 ARNs) hardcode the "aws" partition
# throughout, pre-existing and module-wide, not something this file
# controls. A genuine GovCloud/China SSO session would pass this regex, then
# fail at the iam:GetRole call below with AccessDenied (the policy's
# resources never match an arn:aws-us-gov:iam::... role) -- rejected with
# the same generic message a spoofed ARN gets, which is at least a safe
# (fail-closed) outcome, just not the informative one the comment above
# describes. Threading data.aws_partition.current.partition through those
# other files, to make non-"aws" partitions genuinely work, is a separate,
# module-wide change this PR does not make.
_ASSUMED_ROLE_ARN_RE = re.compile(
    r"^arn:(?:aws|aws-cn|aws-us-gov):sts::(?P<account_id>\d{12}):assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)$"
)

# The path prefix IAM Identity Center provisions its own roles under — this
# is the part of a role's identity AWS itself enforces, unlike the role's
# name, which anyone with iam:CreateRole can imitate. A prefix, not an exact
# path, because a
# multi-region (or non-us-east-1 identity source) deployment adds a region
# segment: .../sso.amazonaws.com/<region>/AWSReservedSSO_... — the same
# reason slack_handler_lambda.tf's own IAM policy lists both resource shapes.
_SSO_RESERVED_ROLE_PATH_PREFIX = "/aws-reserved/sso.amazonaws.com/"

_iam_client = boto3.Session().client("iam")

# botocore error codes that mean "IAM couldn't answer right now", as opposed
# to "this role genuinely isn't SSO-provisioned". Conflating the two would
# tell a legitimate caller their credentials are invalid (GENERIC_REJECTION)
# during a transient IAM hiccup, when the honest answer is "try again" --
# see TransientIAMError below.
_TRANSIENT_IAM_ERROR_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
    }
)


class TransientIAMError(Exception):
    """Raised when iam:GetRole fails for a reason that says nothing about
    whether role_name is actually SSO-provisioned (throttling, a 5xx, IAM
    itself being unavailable). Callers should surface this distinctly from a
    real rejection -- e.g. as a 503 the CLI can retry -- rather than letting
    it fall through to GENERIC_REJECTION's "your credentials are invalid"
    message, or to a generic 500 that pages the approvals channel for what's
    just AWS being temporarily unavailable."""


def _is_transient(error: botocore.exceptions.ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    if code in _TRANSIENT_IAM_ERROR_CODES:
        return True
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return status >= 500  # noqa: PLR2004


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


def extract_identity(user_arn: str, identity_store_client: "IdentityStoreClient", identity_store_id: str) -> tuple[str, str, dict] | None:
    """Return the requester's real, registered (email, UserId, the full
    list_users() snapshot they were matched against), but only if user_arn is
    an assumed-role session in the expected account, under a role that's
    genuinely IAM Identity Center-provisioned (verified via its real IAM
    path, not just its name), whose session name resolves to an Identity
    Store user by exact username match. A spoofed ARN, or a role session
    whose name doesn't match any real username, is rejected.

    The UserId comes along for the ride from find_email_by_username's own
    match -- this is the UserId this specific, IAM-authenticated session was
    actually verified against, available for a caller to cross-check against
    any later, independent email-based UserId lookup rather than silently
    trusting that a second lookup resolves to the same person. The
    list_of_users snapshot is returned too so that cross-check can reuse it
    instead of paying for (and separately having to guard against throttling
    on) another full paginated Identity Store scan -- list_users is
    documented, in sso.py, as expensive enough that a caller needing more
    than one lookup in the same request should fetch once and reuse it."""
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
    #
    # This is a full paginated Identity Store scan -- the call on this path
    # most likely to throttle -- so it gets the same transient-vs-real
    # distinction _is_sso_provisioned_role's iam:GetRole call already makes:
    # a throttle/5xx/connectivity failure says nothing about whether the
    # caller's identity is valid, so it's surfaced as TransientIAMError
    # (a 503 the CLI can retry) rather than falling through to the generic
    # exception handler as a 500 plus a Slack post. A non-transient
    # ClientError (e.g. this Lambda's own IAM policy unexpectedly missing
    # identitystore:ListUsers) is a real misconfiguration, not something to
    # silently swallow -- that's left to propagate and page the channel.
    try:
        list_of_users = sso.list_users(identity_store_client, identity_store_id)
    except botocore.exceptions.ClientError as e:
        if _is_transient(e):
            raise TransientIAMError from e
        raise
    except botocore.exceptions.BotoCoreError as e:
        raise TransientIAMError from e
    found = sso.find_email_by_username(list_of_users, match["session_name"])
    if found is None:
        return None
    # list_of_users is returned alongside (email, user_id) so a caller that
    # needs a second lookup against the same Identity Store snapshot (e.g.
    # main.py's email round-trip cross-check) can reuse it instead of paying
    # for -- and separately having to guard -- another full paginated scan.
    email, user_id = found
    return email, user_id, list_of_users


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
    except botocore.exceptions.ClientError as e:
        if _is_transient(e):
            raise TransientIAMError from e
        # Covers both "no such role" (deleted between assuming it and this
        # call) and "access denied" (this Lambda's own IAM policy already
        # refuses to let it read a role outside the reserved path) —
        # either way, reject rather than assume.
        return False
    except botocore.exceptions.BotoCoreError as e:
        # A connect/read-timeout or endpoint-resolution failure (no HTTP
        # response at all, so _is_transient's status-code/error-code check
        # doesn't apply) says nothing about whether role_name is genuinely
        # SSO-provisioned, same as a transient ClientError above.
        raise TransientIAMError from e
    return role["Path"].startswith(_SSO_RESERVED_ROLE_PATH_PREFIX)
