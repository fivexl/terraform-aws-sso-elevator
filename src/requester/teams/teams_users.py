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


async def get_user_from_activity(turn_context: object) -> entities.teams.TeamsUser:
    """Extract user info from an incoming activity (ActivityContext or legacy TurnContext)."""
    activity = turn_context.activity
    from_prop = getattr(activity, "from_property", None) or getattr(activity, "from_", None)
    if from_prop is None:
        raise ValueError("Activity has no sender (from)")

    conv = getattr(activity, "conversation", None)
    if conv and getattr(conv, "id", None) and hasattr(turn_context, "api"):
        try:
            member = await turn_context.api.conversations.members(cast("str", conv.id)).get_by_id(str(from_prop.id))
            email = str(getattr(member, "email", None) or "")
            display_name = str(getattr(member, "name", None) or from_prop.name or "")
            aad_object_id = str(getattr(member, "aad_object_id", None) or getattr(from_prop, "aad_object_id", None) or "")
        except Exception as e:
            logger.exception("Failed to get member from conversation API, using activity: %s", e)
            email = ""
            display_name = from_prop.name or ""
            aad_object_id = str(getattr(from_prop, "aad_object_id", None) or "")
    else:
        email = ""
        display_name = from_prop.name or ""
        aad_object_id = str(getattr(from_prop, "aad_object_id", None) or "")

    return entities.teams.TeamsUser(
        id=str(from_prop.id),
        aad_object_id=aad_object_id,
        email=email,
        display_name=display_name,
    )


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
                logger.warning(f"Graph API rate limited (429), retrying after {retry_after}s (attempt {attempt + 1})")
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

        query_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
            filter=f"mail eq '{email}' or userPrincipalName eq '{email}'",
            select=["id", "mail", "userPrincipalName", "displayName"],
        )

        return RequestConfiguration(query_parameters=query_params)
    except ImportError:
        return None


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
            members = await turn_context.api.conversations.members(channel_id).get()
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
            logger.warning(f"No email found for SSO user {sso_user_id}")
            return None

        return await get_user_by_email(graph_client, email)
    except Exception as e:
        logger.exception(f"Failed to resolve SSO principal {sso_user_id} to Teams user: {e}")
        return None
