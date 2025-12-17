"""Sync state manager for attribute-based group sync.

This module tracks current vs desired group membership state and computes
the required sync actions (add, remove, warn).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import cache as cache_module
from config import get_logger

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_s3 import S3Client

    from attribute_mapper import AttributeMapper

logger = get_logger(service="sync_state")


@dataclass(frozen=True)
class GroupMembershipState:
    """Current state of a group's membership.

    Attributes:
        group_id: The Identity Store group ID.
        group_name: Human-readable group name.
        current_members: Set of user principal IDs currently in the group.
    """

    group_id: str
    group_name: str
    current_members: frozenset[str]


@dataclass(frozen=True)
class UserInfo:
    """Basic user information for sync operations.

    Attributes:
        user_id: The Identity Store user principal ID.
        email: User's email address.
        attributes: Dictionary of user attribute name to value.
    """

    user_id: str
    email: str
    attributes: dict[str, str]


@dataclass(frozen=True)
class SyncAction:
    """An action to take during sync.

    Attributes:
        action_type: Type of action - "add", "remove", or "warn".
        user_id: The user principal ID to act on.
        user_email: The user's email address for logging/notifications.
        group_id: The group ID to modify.
        group_name: Human-readable group name for logging/notifications.
        reason: Human-readable explanation of why this action is needed.
        matched_attributes: Attributes that matched (for add) or were expected (for remove/warn).
    """

    action_type: Literal["add", "remove", "warn"]
    user_id: str
    user_email: str
    group_id: str
    group_name: str
    reason: str
    matched_attributes: dict[str, str] | None = None


class SyncStateManager:
    """Computes required sync actions based on current and desired state.

    This class evaluates users against attribute mapping rules and compares
    with current group memberships to determine what actions are needed.
    """

    def __init__(
        self,
        managed_group_ids: dict[str, str],
        mapper: AttributeMapper,
        manual_assignment_policy: Literal["warn", "remove"],
    ) -> None:
        """Initialize the sync state manager.

        Args:
            managed_group_ids: Mapping of group name to group ID for managed groups.
            mapper: AttributeMapper instance with configured rules.
            manual_assignment_policy: Policy for handling manual assignments.
        """
        self._managed_group_ids = managed_group_ids
        self._managed_group_names = {v: k for k, v in managed_group_ids.items()}
        self._mapper = mapper
        self._manual_assignment_policy = manual_assignment_policy

    def compute_sync_actions(
        self,
        users: list[UserInfo],
        current_state: dict[str, GroupMembershipState],
    ) -> list[SyncAction]:
        """Compute all actions needed to reach desired state.

        This method:
        1. Evaluates each user against mapping rules to determine target groups
        2. Compares with current group memberships
        3. Generates add actions for users who should be in groups but aren't
        4. Generates remove/warn actions for users in groups who shouldn't be

        Args:
            users: List of users with their attributes.
            current_state: Current group membership state keyed by group ID.

        Returns:
            List of SyncAction objects describing required changes.
        """
        actions: list[SyncAction] = []

        # Build user lookup for quick access
        users_by_id = {user.user_id: user for user in users}

        # For each managed group, compute required actions
        for group_id, state in current_state.items():
            if group_id not in self._managed_group_names:
                # Skip non-managed groups
                continue

            group_name = state.group_name
            rule = self._mapper.get_rule_for_group(group_id)

            # Log the rule configuration for this group
            if rule:
                rule_conditions = [(c.attribute_name, c.expected_value) for c in rule.conditions]
                logger.info(f"Processing group '{group_name}' (id={group_id}) with rule conditions: {rule_conditions}")
            else:
                logger.warning(f"No rule found for managed group '{group_name}' (id={group_id})")

            # Compute desired members for this group
            desired_members: set[str] = set()
            for user in users:
                logger.debug(f"Evaluating user '{user.email}' (id={user.user_id}) with attributes: {user.attributes}")
                target_groups = self._mapper.get_target_groups_for_user(user.attributes)
                if group_id in target_groups:
                    desired_members.add(user.user_id)
                    logger.debug(f"User '{user.email}' matches rules for group '{group_name}'")

            # Log summary for this group
            logger.info(f"Group '{group_name}': current_members={len(state.current_members)}, desired_members={len(desired_members)}")

            # Find users to add (in desired but not in current)
            users_to_add = desired_members - state.current_members
            for user_id in users_to_add:
                user = users_by_id.get(user_id)
                if user:
                    matched_attrs = self._get_matched_attributes(user.attributes, rule)
                    expected_attrs = self._get_expected_attributes(rule) if rule else None
                    logger.info(
                        f"ADD action: user '{user.email}' to group '{group_name}' - "
                        f"user_attributes={user.attributes}, "
                        f"rule_expected={expected_attrs}, "
                        f"matched_attributes={matched_attrs}"
                    )
                    actions.append(
                        SyncAction(
                            action_type="add",
                            user_id=user_id,
                            user_email=user.email,
                            group_id=group_id,
                            group_name=group_name,
                            reason=f"User matches attribute rules for group '{group_name}'",
                            matched_attributes=matched_attrs,
                        )
                    )

            # Find manual assignments (in current but not in desired)
            manual_assignments = state.current_members - desired_members
            for user_id in manual_assignments:
                user = users_by_id.get(user_id)
                if user:
                    expected_attrs = self._get_expected_attributes(rule) if rule else None
                    # Determine action based on policy
                    if self._manual_assignment_policy == "remove":
                        action_type: Literal["add", "remove", "warn"] = "remove"
                        reason = f"User does not match attribute rules for group '{group_name}' and policy is 'remove'"
                    else:
                        action_type = "warn"
                        reason = f"User does not match attribute rules for group '{group_name}' (manual assignment detected)"

                    logger.info(
                        f"{action_type.upper()} action: user '{user.email}' in group '{group_name}' - "
                        f"user_attributes={user.attributes}, "
                        f"rule_expected={expected_attrs}, "
                        f"policy={self._manual_assignment_policy}"
                    )
                    actions.append(
                        SyncAction(
                            action_type=action_type,
                            user_id=user_id,
                            user_email=user.email,
                            group_id=group_id,
                            group_name=group_name,
                            reason=reason,
                            matched_attributes=expected_attrs,
                        )
                    )

        return actions

    def _get_matched_attributes(
        self,
        user_attributes: dict[str, str],
        rule: object | None,
    ) -> dict[str, str] | None:
        """Extract the attributes that matched the rule.

        Args:
            user_attributes: The user's attributes.
            rule: The AttributeMappingRule that matched.

        Returns:
            Dictionary of matched attribute name to value, or None if no rule.
        """
        if rule is None:
            return None

        # Get the attribute names from the rule's conditions
        from attribute_mapper import AttributeMappingRule

        if not isinstance(rule, AttributeMappingRule):
            return None

        matched = {}
        for condition in rule.conditions:
            attr_name = condition.attribute_name
            if attr_name in user_attributes:
                matched[attr_name] = user_attributes[attr_name]

        return matched if matched else None

    def _get_expected_attributes(self, rule: object | None) -> dict[str, str] | None:
        """Get the expected attributes from a rule.

        Args:
            rule: The AttributeMappingRule.

        Returns:
            Dictionary of expected attribute name to value, or None if no rule.
        """
        if rule is None:
            return None

        from attribute_mapper import AttributeMappingRule

        if not isinstance(rule, AttributeMappingRule):
            return None

        return {cond.attribute_name: cond.expected_value for cond in rule.conditions}

    def get_users_matching_group(
        self,
        users: list[UserInfo],
        group_id: str,
    ) -> set[str]:
        """Get user IDs that should be members of a group based on rules.

        Args:
            users: List of users with their attributes.
            group_id: The group ID to check.

        Returns:
            Set of user IDs that match the rules for this group.
        """
        matching_users: set[str] = set()
        for user in users:
            target_groups = self._mapper.get_target_groups_for_user(user.attributes)
            if group_id in target_groups:
                matching_users.add(user.user_id)
        return matching_users

    def is_manual_assignment(
        self,
        user: UserInfo,
        group_id: str,
    ) -> bool:
        """Check if a user's membership in a group is a manual assignment.

        A manual assignment is when a user is in a group but doesn't match
        the attribute rules for that group.

        Args:
            user: The user to check.
            group_id: The group ID to check.

        Returns:
            True if the user doesn't match rules for this group, False otherwise.
        """
        target_groups = self._mapper.get_target_groups_for_user(user.attributes)
        return group_id not in target_groups


# -----------------User and Group Data Retrieval-----------------#


def _extract_user_email(user: dict) -> str:
    """Extract primary email from user's Emails list."""
    emails = user.get("Emails", [])
    for email_entry in emails:
        if email_entry.get("Primary", False):
            return email_entry.get("Value", "")
    return emails[0].get("Value", "") if emails else ""


def _extract_user_attributes(user: dict) -> dict[str, str]:
    """Extract attributes from user data."""
    attributes: dict[str, str] = {}

    # Standard SCIM attributes - map field names to attribute keys
    scim_fields = [
        ("DisplayName", "displayName"),
        ("Title", "title"),
        ("Locale", "locale"),
        ("Timezone", "timezone"),
        ("UserType", "userType"),
        ("PreferredLanguage", "preferredLanguage"),
        ("ProfileUrl", "profileUrl"),
        ("NickName", "nickName"),
    ]
    for field, attr_key in scim_fields:
        if user.get(field):
            attributes[attr_key] = user[field]

    # Name attributes
    name = user.get("Name", {})
    name_fields = [
        ("GivenName", "givenName"),
        ("FamilyName", "familyName"),
        ("MiddleName", "middleName"),
        ("HonorificPrefix", "honorificPrefix"),
        ("HonorificSuffix", "honorificSuffix"),
    ]
    for field, attr_key in name_fields:
        if name.get(field):
            attributes[attr_key] = name[field]

    # Enterprise extension attributes (commonly used for ABAC)
    for ext_id in user.get("ExternalIds", []):
        issuer = ext_id.get("Issuer", "")
        ext_id_value = ext_id.get("Id", "")
        if issuer and ext_id_value:
            attributes[f"externalId_{issuer}"] = ext_id_value

    return attributes


def _fetch_users_from_identity_store(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
) -> list[dict]:
    """Fetch all users with their attributes from Identity Store.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        List of user dictionaries with attributes.
    """
    users: list[dict] = []

    paginator = identity_store_client.get_paginator("list_users")
    for page in paginator.paginate(IdentityStoreId=identity_store_id):
        for user in page.get("Users", []):
            users.append(
                {
                    "user_id": user.get("UserId"),
                    "username": user.get("UserName", ""),
                    "email": _extract_user_email(user),
                    "attributes": _extract_user_attributes(user),
                }
            )

    logger.info(f"Fetched {len(users)} users from Identity Store")
    return users


def _fetch_groups_from_identity_store(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
) -> dict[str, str]:
    """Fetch all groups from Identity Store and return name-to-ID mapping.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        Dictionary mapping group names to their IDs.
    """
    name_to_id: dict[str, str] = {}

    paginator = identity_store_client.get_paginator("list_groups")
    for page in paginator.paginate(IdentityStoreId=identity_store_id):
        for group in page.get("Groups", []):
            display_name = group.get("DisplayName")
            group_id = group.get("GroupId")
            if display_name and group_id:
                name_to_id[display_name] = group_id

    logger.info(f"Fetched {len(name_to_id)} groups from Identity Store")
    return name_to_id


def get_users_with_attributes(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    s3_client: S3Client,
    cache_config: cache_module.CacheConfig,
) -> list[UserInfo]:
    """Get all users with their attributes, using cache with API fallback.

    This function uses the cache resilience pattern:
    - If cache is available and valid, use cached data
    - If cache is unavailable, fall back to direct API calls
    - Update cache after successful API calls

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        s3_client: S3 client for cache operations.
        cache_config: Cache configuration.

    Returns:
        List of UserInfo objects with user attributes.

    Raises:
        Exception: If both cache and API fail.
    """
    users_data = cache_module.with_cache_resilience(
        cache_getter=lambda: cache_module.get_cached_users_with_attributes(s3_client, cache_config),
        api_getter=lambda: _fetch_users_from_identity_store(identity_store_client, identity_store_id),
        cache_setter=lambda users: cache_module.set_cached_users_with_attributes(s3_client, cache_config, users),
        resource_name="users_with_attributes",
    )

    # Convert dictionaries to UserInfo objects
    return [
        UserInfo(
            user_id=user["user_id"],
            email=user["email"],
            attributes=user["attributes"],
        )
        for user in users_data
    ]


def get_managed_groups(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    s3_client: S3Client,
    cache_config: cache_module.CacheConfig,
    managed_group_names: list[str],
) -> tuple[dict[str, str], dict[str, GroupMembershipState]]:
    """Get managed groups with their current membership state.

    This function:
    1. Fetches all groups (using cache with API fallback)
    2. Filters to only managed groups
    3. Fetches current membership for each managed group

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        s3_client: S3 client for cache operations.
        cache_config: Cache configuration.
        managed_group_names: List of group names to manage.

    Returns:
        Tuple of:
        - Dictionary mapping group names to IDs (for all groups, for caching)
        - Dictionary mapping group IDs to GroupMembershipState (for managed groups only)

    Raises:
        Exception: If both cache and API fail.
    """
    # Get all groups (name to ID mapping)
    all_groups = cache_module.with_cache_resilience(
        cache_getter=lambda: cache_module.get_cached_groups(s3_client, cache_config),
        api_getter=lambda: _fetch_groups_from_identity_store(identity_store_client, identity_store_id),
        cache_setter=lambda groups: cache_module.set_cached_groups(s3_client, cache_config, groups),
        resource_name="groups",
    )

    # Filter to managed groups and get their membership state
    managed_group_ids: dict[str, str] = {}
    current_state: dict[str, GroupMembershipState] = {}

    for group_name in managed_group_names:
        group_id = all_groups.get(group_name)
        if group_id:
            managed_group_ids[group_name] = group_id

            # Fetch current membership for this group
            try:
                members = _fetch_group_members(identity_store_client, identity_store_id, group_id)
                current_state[group_id] = GroupMembershipState(
                    group_id=group_id,
                    group_name=group_name,
                    current_members=frozenset(members),
                )
            except Exception as e:
                logger.exception(f"Failed to fetch members for group '{group_name}': {e}")
                # Continue with empty membership - will be handled by error resilience
                current_state[group_id] = GroupMembershipState(
                    group_id=group_id,
                    group_name=group_name,
                    current_members=frozenset(),
                )
        else:
            logger.warning(f"Managed group '{group_name}' not found in Identity Store")

    logger.info(f"Retrieved {len(managed_group_ids)} of {len(managed_group_names)} managed groups")
    return all_groups, current_state


def _fetch_group_members(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    group_id: str,
) -> set[str]:
    """Fetch all member user IDs for a group.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        group_id: The group ID to fetch members for.

    Returns:
        Set of user principal IDs that are members of the group.
    """
    members: set[str] = set()

    paginator = identity_store_client.get_paginator("list_group_memberships")
    for page in paginator.paginate(IdentityStoreId=identity_store_id, GroupId=group_id):
        for membership in page.get("GroupMemberships", []):
            member_id = membership.get("MemberId", {})
            user_id = member_id.get("UserId")
            if user_id:
                members.add(user_id)

    return members
