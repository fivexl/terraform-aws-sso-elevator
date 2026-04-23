"""Teams user resolution via Microsoft Graph API and Bot Framework TeamsInfo.

Handles user identity resolution for the Teams integration.
"""

from __future__ import annotations

import asyncio
import logging

import entities.teams

logger = logging.getLogger(__name__)

# Maximum retries for Graph API 429 responses
_MAX_GRAPH_RETRIES = 3


async def get_user_from_activity(turn_context) -> entities.teams.TeamsUser:
    """Extract user info from incoming activity via TeamsInfo.

    Args:
        turn_context: Bot Framework TurnContext with the incoming activity.

    Returns:
        TeamsUser populated from the activity's from_property.
    """
    from botbuilder.core.teams import TeamsInfo  # type: ignore[import]

    activity = turn_context.activity
    from_prop = activity.from_property

    # Try to get full member info via TeamsInfo for email
    try:
        member = await TeamsInfo.get_member(turn_context, from_prop.id)
        email = getattr(member, "email", "") or ""
        display_name = getattr(member, "name", "") or from_prop.name or ""
        aad_object_id = getattr(member, "aad_object_id", "") or getattr(from_prop, "aad_object_id", "") or ""
    except Exception as e:
        logger.exception(f"Failed to get member info via TeamsInfo, falling back to activity data: {e}")
        email = ""
        display_name = from_prop.name or ""
        aad_object_id = getattr(from_prop, "aad_object_id", "") or ""

    return entities.teams.TeamsUser(
        id=from_prop.id,
        aad_object_id=aad_object_id,
        email=email,
        display_name=display_name,
    )


async def get_user_by_email(graph_client, email: str) -> entities.teams.TeamsUser:
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
    import httpx  # type: ignore[import]

    last_error: Exception | None = None
    for attempt in range(_MAX_GRAPH_RETRIES):
        try:
            result = await graph_client.users.get(
                request_configuration=_build_user_filter_config(email)
            )
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


def _build_user_filter_config(email: str):
    """Build Graph API request configuration with email filter."""
    try:
        from msgraph.generated.users.users_request_builder import UsersRequestBuilder  # type: ignore[import]

        query_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
            filter=f"mail eq '{email}' or userPrincipalName eq '{email}'",
            select=["id", "mail", "userPrincipalName", "displayName"],
        )
        from msgraph.generated.models.o_data_errors.o_data_error import ODataError  # type: ignore[import]
        from kiota_abstractions.base_request_configuration import RequestConfiguration  # type: ignore[import]

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
            if status == 429:
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


async def check_user_in_channel(turn_context, channel_id: str, user_aad_id: str) -> bool:
    """Check if user is a member of the approval channel via TeamsInfo.

    Args:
        turn_context: Bot Framework TurnContext.
        channel_id: Teams channel ID to check membership in.
        user_aad_id: AAD object ID of the user to check.

    Returns:
        True if user is in the channel, False otherwise (including on error).
    """
    try:
        from botbuilder.core.teams import TeamsInfo  # type: ignore[import]

        members = await TeamsInfo.get_team_members(turn_context)
        for member in members:
            if getattr(member, "aad_object_id", None) == user_aad_id:
                return True
        return False
    except Exception as e:
        logger.exception(f"Failed to check channel membership, treating user as non-member: {e}")
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
    graph_client,
    sso_user_id: str,
    sso_client,
    identity_store_client,
    cfg,
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
