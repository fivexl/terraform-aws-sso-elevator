"""Teams user resolution via Microsoft Graph API and conversation members API.

Handles user identity resolution for the Teams integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import entities.teams

if TYPE_CHECKING:
    import config as app_config
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_sso_admin import SSOAdminClient

logger = logging.getLogger(__name__)

# Maximum retries for Graph API 429 responses
_MAX_GRAPH_RETRIES = 3
# HTTP 429 Too Many Requests (Graph rate limit)
_HTTP_STATUS_TOO_MANY_REQUESTS = 429


def _activity_from(activity: object) -> object | None:
    return getattr(activity, "from_property", None) or getattr(activity, "from_", None)


async def get_user_from_activity(turn_context: object) -> entities.teams.TeamsUser:
    """Extract user info from an incoming activity (ActivityContext or legacy TurnContext)."""
    activity = turn_context.activity
    from_prop = _activity_from(activity)
    if from_prop is None:
        raise ValueError("Activity has no sender (from)")

    default_display_name = str(getattr(from_prop, "name", None) or "")
    default_aad_object_id = str(getattr(from_prop, "aad_object_id", None) or "")

    conv = getattr(activity, "conversation", None)
    if conv and getattr(conv, "id", None) and hasattr(turn_context, "api"):
        try:
            member = await turn_context.api.conversations.members(cast("str", conv.id)).get(str(from_prop.id))
            raw_addr = getattr(member, "email", None) or getattr(member, "user_principal_name", None)
            email = str(raw_addr or "")
            display_name = str(getattr(member, "name", None) or default_display_name)
            aad_object_id = str(getattr(member, "aad_object_id", None) or default_aad_object_id)
        except Exception as e:
            logger.exception("Failed to get member from conversation API, using activity: %s", e)
            email = ""
            display_name = default_display_name
            aad_object_id = default_aad_object_id
    else:
        email = ""
        display_name = default_display_name
        aad_object_id = default_aad_object_id

    return entities.teams.TeamsUser(
        id=str(from_prop.id),
        aad_object_id=aad_object_id,
        email=email,
        display_name=display_name,
    )


def _local_part_for_roster_match(addr: str) -> str:
    s = (addr or "").strip().lower()
    if not s:
        return ""
    if "@" in s:
        return s.rsplit("@", 1)[0]
    return s


def _teams_user_from_conversation_member(member: object) -> entities.teams.TeamsUser | None:
    rid = str(getattr(member, "id", None) or "").strip()
    if not rid:
        return None
    raw_addr = getattr(member, "email", None) or getattr(member, "user_principal_name", None)
    email = str(raw_addr or "").strip()
    display_name = str(getattr(member, "name", None) or email or rid)
    aad = str(getattr(member, "aad_object_id", None) or "")
    return entities.teams.TeamsUser(
        id=rid,
        aad_object_id=aad,
        email=email,
        display_name=display_name,
    )


def _roster_list_members_api(
    app: object,
    *,
    service_url: str | None,
    fallback_tenant_id: str | None,
) -> object | None:
    """Use the same regional Bot ``serviceUrl`` as sends/updates, or 404 on ``GET .../conversations/.../members``."""
    su = (service_url or "").strip()
    tid = (fallback_tenant_id or "").strip()
    if not su and tid:
        su = f"https://smba.trafficmanager.net/{tid}/"
    su = su.rstrip("/")
    default = getattr(app, "api", None)
    if not su:
        return default
    as_http = getattr(getattr(app, "activity_sender", None), "_client", None) or getattr(app, "http_client", None)
    if as_http is None:
        return default
    from microsoft_teams.api import ApiClient  # type: ignore[import]

    return ApiClient(su, as_http, cloud=getattr(app, "cloud", None))


async def fetch_channel_roster_teams_users(
    app: object,
    base_channel_conversation_id: str,
    *,
    service_url: str | None = None,
    fallback_tenant_id: str | None = None,
) -> list[entities.teams.TeamsUser]:
    """Load channel members via Bot ``conversations/{id}/members`` for local-part approver matching.

    When calling outside a turn (e.g. approver ping), pass ``service_url`` from the stored activity — the
    default :attr:`App.api` base URL may not resolve the channel and can return 404.
    """
    cid = (base_channel_conversation_id or "").strip()
    if not cid:
        return []
    api = _roster_list_members_api(app, service_url=service_url, fallback_tenant_id=fallback_tenant_id)
    if api is None or not hasattr(api, "conversations"):
        return []
    try:
        members = await api.conversations.members(cid).get_all()  # type: ignore[union-attr]
    except Exception as e:
        logger.exception("Failed to list channel members for roster approver match: %s", e)
        return []
    out: list[entities.teams.TeamsUser] = []
    for m in members or []:
        u = _teams_user_from_conversation_member(m)
        if u and u.id:
            out.append(u)
    return out


def find_teams_user_in_roster_by_approver_email(
    approver_email: str,
    roster: list[entities.teams.TeamsUser],
) -> entities.teams.TeamsUser | None:
    """Match policy approver to a channel member by the part before ``@`` (UPN/mail may differ in domain)."""
    ap = _local_part_for_roster_match(approver_email)
    if not ap:
        return None
    for u in roster:
        lp = _local_part_for_roster_match(u.email)
        if lp and lp == ap:
            return u
    return None


async def get_user_by_email_with_config(graph_client: object, email: str, cfg: "app_config.Config") -> entities.teams.TeamsUser:
    """Resolve a directory approver in Entra, trying :func:`sso.ordered_email_variants_for_graph_lookup` in order."""
    from sso import ordered_email_variants_for_graph_lookup  # type: ignore[import]

    last_error: Exception | None = None
    for cand in ordered_email_variants_for_graph_lookup(email, cfg):
        try:
            return await get_user_by_email(graph_client, cand)
        except Exception as e:
            last_error = e
    raise last_error or ValueError(f"No Teams user found for: {email}")


async def get_user_by_email(graph_client: object, email: str) -> entities.teams.TeamsUser:
    """Look up a Teams user by email via Microsoft Graph API.

    Handles 429 rate limiting with up to _MAX_GRAPH_RETRIES retries,
    respecting the Retry-After header.

    Args:
        graph_client: Microsoft Graph SDK client.
        email: Email address to look up.

    Returns:
        TeamsUser for the found user.

    Raises:
        Exception: If user not found or retries exhausted.
    """

    last_error: Exception | None = None
    for attempt in range(_MAX_GRAPH_RETRIES):
        try:
            result = await graph_client.users.get(request_configuration=_build_user_filter_config(email))
            users = result.value if result and result.value else []
            if not users:
                raise ValueError(f"No Teams user found with email: {email}")
            user = users[0]
            return entities.teams.TeamsUser(
                id=user.id or "",
                aad_object_id=user.id or "",
                email=user.mail or user.user_principal_name or email,
                display_name=user.display_name or email,
            )
        except Exception as e:
            # Check for 429 rate limit
            retry_after = _extract_retry_after(e)
            if retry_after is not None and attempt < _MAX_GRAPH_RETRIES - 1:
                logger.warning(
                    "Graph API rate limited (429), retrying after %ss (attempt %s)",
                    retry_after,
                    attempt + 1,
                )
                await asyncio.sleep(retry_after)
                last_error = e
                continue
            last_error = e
            break

    raise last_error or RuntimeError(f"Failed to get user by email: {email}")


def _build_user_filter_config(email: str) -> object | None:
    """Build Graph API request configuration with email filter."""
    try:
        from msgraph.generated.users.users_request_builder import UsersRequestBuilder  # type: ignore[import]
        from kiota_abstractions.base_request_configuration import RequestConfiguration  # type: ignore[import]

        # OData string literal escaping uses doubled single quotes.
        # This is a no-op for normal emails, and hardens against malformed input.
        safe_email = _escape_odata_string_literal(email)
        query_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
            filter=f"mail eq '{safe_email}' or userPrincipalName eq '{safe_email}'",
            select=["id", "mail", "userPrincipalName", "displayName"],
        )

        return RequestConfiguration(query_parameters=query_params)
    except ImportError:
        return None


def _escape_odata_string_literal(value: str) -> str:
    """Escape a value for embedding inside single-quoted OData string literal."""
    return (value or "").replace("'", "''")


def _extract_retry_after(error: Exception) -> float | None:
    """Extract Retry-After value from a 429 error, if present."""
    # Try common patterns for Graph SDK errors
    for attr in ("response", "error", "inner_error"):
        inner = getattr(error, attr, None)
        if inner is not None:
            status = getattr(inner, "status_code", None) or getattr(inner, "status", None)
            if status == _HTTP_STATUS_TOO_MANY_REQUESTS:
                headers = getattr(inner, "headers", {}) or {}
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
                if retry_after:
                    try:
                        return float(retry_after)
                    except (ValueError, TypeError):
                        pass
                return 1.0  # default 1 second if header missing
    # Check if error message contains 429
    if "429" in str(error):
        return 1.0
    return None


async def check_user_in_channel(turn_context: object, channel_id: str, user_aad_id: str) -> bool:
    """Check if user is a member of the given channel (Teams SDK or legacy context)."""
    if hasattr(turn_context, "api"):
        try:
            members = await turn_context.api.conversations.members(channel_id).get_all()
            for member in members:
                if getattr(member, "aad_object_id", None) == user_aad_id:
                    return True
            return False
        except Exception as e:
            logger.exception("Failed to check channel membership: %s", e)
            return False
    logger.debug("No Teams API on context; skipping channel membership check")
    return False


def build_mention(user_id: str, display_name: str) -> tuple[str, dict]:
    """Build mention text and Mention entity object for a Teams user.

    Args:
        user_id: Teams user ID.
        display_name: Display name of the user.

    Returns:
        Tuple of (mention_text, mention_entity_dict).
        mention_text contains <at>display_name</at>.
        mention_entity_dict is the Mention entity for the activity's entities array.
    """
    mention_text = f"<at>{display_name}</at>"
    mention_entity = {
        "type": "mention",
        "text": mention_text,
        "mentioned": {
            "id": user_id,
            "name": display_name,
        },
    }
    return mention_text, mention_entity


async def resolve_principal_to_teams_user(
    graph_client: object,
    sso_user_id: str,
    sso_client: "SSOAdminClient",
    identity_store_client: "IdentityStoreClient",
    cfg: "app_config.Config",
) -> entities.teams.TeamsUser | None:
    """Resolve SSO principal ID to a Teams user via email and Microsoft Graph API.

    Used in the revoker to build mentions for notifications.

    Args:
        graph_client: Microsoft Graph SDK client.
        sso_user_id: AWS SSO principal/user ID.
        sso_client: AWS SSO admin client.
        identity_store_client: AWS Identity Store client.
        cfg: Application config.

    Returns:
        TeamsUser if found, None otherwise.
    """
    import sso as sso_module  # type: ignore[import]

    try:
        sso_instance = sso_module.describe_sso_instance(sso_client, cfg.sso_instance_arn)
        identity_store_user = identity_store_client.describe_user(
            IdentityStoreId=sso_instance.identity_store_id,
            UserId=sso_user_id,
        )
        emails = identity_store_user.get("Emails", [])
        email = next((e["Value"] for e in emails if e.get("Primary")), None)
        if not email and emails:
            email = emails[0].get("Value")
        if not email:
            logger.warning("No email found for SSO user %s", sso_user_id)
            return None

        return await get_user_by_email_with_config(graph_client, email, cfg)
    except Exception as e:
        logger.exception("Failed to resolve SSO principal %s to Teams user: %s", sso_user_id, e)
        return None
