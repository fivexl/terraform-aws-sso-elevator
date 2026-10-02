"""Identity verification for the CLI access-request path.

The CLI signs its request directly against API Gateway (SigV4, an AWS_IAM
authorizer) instead of going through Slack. API Gateway verifies the
signature itself and populates the caller's verified identity before the
Lambda ever runs (requestContext.identity.userArn on the REST API this route
uses -- see cli_rest_api.tf, issue #214) -- extract_identity here doesn't
verify a signature, it decides whether that already-verified identity is
trustworthy enough to act on: a real SSO session, under a role whose name
matches this deployment's configured prefix, resolved to a real registered
email via an Identity Store lookup.

Which AWS account the caller is in is NOT checked here as a reject condition
-- the real gate for account/org membership is the REST API's own resource
policy (scoped to aws:PrincipalOrgID): any account outside the AWS
Organization never reaches this code at all, API Gateway rejects it first.
Trusting the resource policy for that, rather than duplicating a single-
hardcoded-account comparison here, is an intentional simplification,
confirmed explicitly rather than an oversight.

Whether the role's real IAM path is genuinely IAM Identity Center's reserved
one IS still checked here, but only when the caller's account matches this
deployment's own (cli_expected_account_id) -- iam:GetRole is account-scoped,
so this Lambda can never resolve a role's real path in a *different*
account, meaning that stronger check is only available at all for the
same-account case. (An earlier version of this file dropped the path check
for every caller, same-account included, reasoning that it couldn't work
cross-account anyway -- found in review to be a broader regression than
necessary: the check remained fully valid, and AWS-enforced, for same-account
callers, so it's restored for exactly that case.)

The accepted residual risk, narrowed to cross-account callers only: an
assumed-role ARN never carries the underlying role's IAM path, only its name
-- a role's *name* is not an AWS-enforced signal at all; anyone with
iam:CreateRole in an allowed *different* account can name a role of their own
AWSReservedSSO_Anything, assume it, and set its session name to an arbitrary
string (RoleSessionName is caller-specified at AssumeRole time). If that
string happens to match a real Identity Store username, find_email_by_username
below would resolve it to that real user's email, letting the caller submit a
request attributed to someone else. This requires iam:CreateRole plus
sts:AssumeRole in some account already inside the organization -- a more
common permission combination (routine on developer sandboxes and CI/CD
roles) than "admin-only", so this residual risk should not be treated as
rare. Defending against it fully would require a cross-account role-path
verification mechanism this module does not yet have; until then, this is a
judged, accepted tradeoff for letting the CLI route work org-wide without a
per-identity IAM grant.

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
    from mypy_boto3_s3 import S3Client

# Matches all three real AWS partitions (aws, aws-cn, aws-us-gov) -- a
# hardcoded "aws" would reject 100% of requests in GovCloud/China with the
# same generic "not associated with an SSO session" message a genuinely
# invalid ARN gets, giving an operator there nothing to go on.
_ASSUMED_ROLE_ARN_RE = re.compile(
    r"^arn:(?:aws|aws-cn|aws-us-gov):sts::(?P<account_id>\d{12}):assumed-role/(?P<role_name>[^/]+)/(?P<session_name>.+)$"
)

# The path prefix IAM Identity Center provisions its own roles under — this is the part of a
# role's identity AWS itself enforces, unlike the role's name, which anyone with iam:CreateRole
# can imitate. A prefix, not an exact path, because a multi-region (or non-us-east-1 identity
# source) deployment adds a region segment: .../sso.amazonaws.com/<region>/AWSReservedSSO_... —
# the same reason slack_handler_lambda.tf's own IAM policy lists both resource shapes.
_SSO_RESERVED_ROLE_PATH_PREFIX = "/aws-reserved/sso.amazonaws.com/"

_iam_client = boto3.Session().client("iam")


class TransientIAMError(Exception):
    """Raised when verifying a caller's identity fails for a reason that says nothing about
    whether that identity is actually valid (throttling, a 5xx, IAM or Identity Store being
    unavailable). Callers should surface this distinctly from a real rejection -- e.g. as a
    503 the CLI can retry -- rather than letting it fall through to GENERIC_REJECTION's "your
    credentials are invalid" message, or to a generic 500 that pages the approvals channel for
    what's just AWS being temporarily unavailable."""


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
    """Return the requester's real, registered (email, UserId, the full
    list_users() snapshot they were matched against), but only if user_arn is
    an assumed-role session under a role whose name matches this deployment's
    configured SSO role-name prefix (and, for a same-account caller, whose
    real IAM path also confirms it's genuinely IAM Identity Center-provisioned
    -- see the module docstring for why that stronger check can't extend to a
    different-account caller), whose session name resolves to an Identity
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

    role_name = match["role_name"]
    if not role_name.startswith(cfg.cli_sso_role_name_prefix):
        return None

    # Only checkable for a same-account caller -- iam:GetRole is account-scoped, so this Lambda
    # can never resolve a role's real path in a different account. A different-account caller
    # skips straight to the Identity Store resolution below, relying on the REST API's resource
    # policy (org membership) and the name-prefix check above instead; see the module docstring
    # for the accepted residual risk that leaves for that case specifically.
    if match["account_id"] == cfg.cli_expected_account_id and not _is_sso_provisioned_role(role_name):
        return None

    # session_name is the Identity Store username IAM Identity Center set
    # RoleSessionName to, not necessarily an email itself -- see the module
    # docstring. Resolving it through the Identity Store, rather than
    # returning it directly, is what makes an AD-style username (no '@' at
    # all) work here the same way it already does on the Slack path.
    #
    # This is a full paginated Identity Store scan -- the call on this path
    # most likely to throttle -- so it goes through list_users_with_cache
    # (#193 item 2), not the raw, uncached list_users: every account/
    # permission-set catalog lookup on this same path already gets S3-backed
    # cache resilience, but this call, arguably the most throttle-prone one
    # here, previously had none at all -- every single CLI request paid for
    # a fresh scan with no fallback. The remaining try/except below still
    # matters for a *cold* cache (or caching disabled): list_users_with_cache
    # only absorbs a throttle/5xx silently when it has cached data to fall
    # back to, and re-raises the raw error otherwise -- the same
    # transient-vs-real distinction _is_sso_provisioned_role's iam:GetRole
    # call already makes: a throttle/5xx/connectivity failure says nothing
    # about whether the caller's identity is valid, so it's surfaced as
    # TransientIAMError (a 503 the CLI can retry) rather than falling
    # through to the generic exception handler as a 500 plus a Slack post.
    # A non-transient ClientError (e.g. this Lambda's own IAM policy
    # unexpectedly missing identitystore:ListUsers) is a real
    # misconfiguration, not something to silently swallow -- that's left to
    # propagate and page the channel.
    try:
        list_of_users = sso.list_users_with_cache(identity_store_client, identity_store_id, s3_client, cfg)
    except botocore.exceptions.ClientError as e:
        if sso.is_transient_aws_error(e):
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
    reserved path, within THIS Lambda's own account -- the caller must
    already be confirmed to be in this deployment's own account before this
    is called (see extract_identity), since iam:GetRole cannot resolve a
    role in a different account.

    This is the part role_name.startswith(prefix) can't check: an
    assumed-role ARN never carries the role's path, only its name, so a role
    created (with iam:CreateRole) at any ordinary path but given a matching
    name would pass a name-only check. iam:GetRole looks the role up
    directly to get its real path.

    This Lambda's own iam:GetRole permission (slack_handler_lambda.tf) is
    itself scoped to only the reserved-path resource shape, so a role at
    any other path 403s here rather than returning its real (non-matching)
    path — caught the same as any other lookup failure, since either way
    the answer is "not a genuine SSO role".

    Whether someone with iam:CreateRole could forge a role directly at the
    reserved path (making this whole check moot) was an open disagreement
    in #194's review, not just a documentation gap: confirmed closed by a
    live test against this deployment's own account -- `aws iam create-role
    --path /aws-reserved/sso.amazonaws.com/ ...` was rejected outright with
    "InvalidInput: The path '/aws-reserved/sso.amazonaws.com/' is reserved
    for AWS use", independent of the caller's own IAM permissions. AWS
    enforces this path as reserved at the IAM API level itself, not merely
    by convention."""
    try:
        role = _iam_client.get_role(RoleName=role_name)["Role"]
    except botocore.exceptions.ClientError as e:
        if sso.is_transient_aws_error(e):
            raise TransientIAMError from e
        # Covers both "no such role" (deleted between assuming it and this
        # call) and "access denied" (this Lambda's own IAM policy already
        # refuses to let it read a role outside the reserved path) —
        # either way, reject rather than assume.
        return False
    except botocore.exceptions.BotoCoreError as e:
        # A connect/read-timeout or endpoint-resolution failure (no HTTP
        # response at all, so is_transient_aws_error's status-code/error-code
        # check doesn't apply -- it always treats a bare BotoCoreError as
        # transient) says nothing about whether role_name is genuinely
        # SSO-provisioned, same as a transient ClientError above.
        raise TransientIAMError from e
    return role["Path"].startswith(_SSO_RESERVED_ROLE_PATH_PREFIX)
