"""Sync state manager for attribute-based group sync.

This module tracks current vs desired group membership state and computes
the required sync actions (add, remove, warn).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from attribute_mapper import mask_email
from config import get_logger

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient

    from attribute_mapper import AttributeMapper

logger = get_logger(service="sync_state")

# Attributes to exclude from logging (PII)
_SENSITIVE_ATTRIBUTES = frozenset(
    {
        "givenName",
        "familyName",
        "middleName",
        "honorificPrefix",
        "honorificSuffix",
        "displayName",
        "nickName",
    }
)


def _filter_sensitive_attributes(attributes: dict[str, str]) -> dict[str, str]:
    """Filter out sensitive attributes (names) from logging.

    Args:
        attributes: User attributes dictionary.

    Returns:
        Filtered dictionary without sensitive attributes.
    """
    return {k: v for k, v in attributes.items() if k not in _SENSITIVE_ATTRIBUTES}


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
                safe_attrs = _filter_sensitive_attributes(user.attributes)
                logger.debug(f"Evaluating user '{mask_email(user.email)}' (id={user.user_id}) with attributes: {safe_attrs}")
                target_groups = self._mapper.get_target_groups_for_user(user.attributes)
                if group_id in target_groups:
                    desired_members.add(user.user_id)
                    logger.debug(f"User '{mask_email(user.email)}' matches rules for group '{group_name}'")

            # Log summary for this group
            logger.info(f"Group '{group_name}': current_members={len(state.current_members)}, desired_members={len(desired_members)}")

            # Find users to add (in desired but not in current)
            users_to_add = desired_members - state.current_members
            for user_id in users_to_add:
                user = users_by_id.get(user_id)
                if user:
                    matched_attrs = self._get_matched_attributes(user.attributes, rule)
                    expected_attrs = self._get_expected_attributes(rule) if rule else None
                    safe_attrs = _filter_sensitive_attributes(user.attributes)
                    logger.info(
                        f"ADD action: user '{mask_email(user.email)}' to group '{group_name}' - "
                        f"user_attributes={safe_attrs}, "
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

                    safe_attrs = _filter_sensitive_attributes(user.attributes)
                    logger.info(
                        f"{action_type.upper()} action: user '{mask_email(user.email)}' in group '{group_name}' - "
                        f"user_attributes={safe_attrs}, "
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


def _extract_fields(source: dict, field_mappings: list[tuple[str, str]], target: dict[str, str]) -> None:
    """Extract fields from source dict to target dict using field mappings."""
    for field, attr_key in field_mappings:
        value = source.get(field)
        if value:
            target[attr_key] = value


def _extract_user_attributes(user: dict) -> dict[str, str]:
    """Extract attributes from user data.

    Extracts standard SCIM attributes, name attributes, enterprise extension
    attributes (department, costCenter, etc.), and external IDs.
    """
    attributes: dict[str, str] = {}

    # Standard SCIM attributes
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
    _extract_fields(user, scim_fields, attributes)

    # Name attributes
    name_fields = [
        ("GivenName", "givenName"),
        ("FamilyName", "familyName"),
        ("MiddleName", "middleName"),
        ("HonorificPrefix", "honorificPrefix"),
        ("HonorificSuffix", "honorificSuffix"),
    ]
    _extract_fields(user.get("Name", {}), name_fields, attributes)

    # Enterprise extension attributes (SCIM enterprise user schema)
    enterprise_ext_key = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
    enterprise_ext = user.get(enterprise_ext_key, {})
    enterprise_fields = [
        ("department", "department"),
        ("costCenter", "costCenter"),
        ("organization", "organization"),
        ("division", "division"),
        ("employeeNumber", "employeeNumber"),
    ]
    _extract_fields(enterprise_ext, enterprise_fields, attributes)

    # Manager is a nested object
    manager = enterprise_ext.get("manager", {})
    _extract_fields(manager, [("displayName", "managerDisplayName"), ("value", "managerId")], attributes)

    # AWS Identity Store Extensions (custom attributes)
    _extract_identity_store_extensions(user.get("Extensions", {}), attributes)

    # External IDs
    _extract_external_ids(user.get("ExternalIds", []), attributes)

    return attributes


def _extract_identity_store_extensions(extensions: dict, attributes: dict[str, str]) -> None:
    """Extract AWS Identity Store custom extension attributes."""
    for ext_key, ext_value in extensions.items():
        if isinstance(ext_value, dict):
            for attr_name, attr_value in ext_value.items():
                if isinstance(attr_value, str):
                    attributes[attr_name] = attr_value
        elif isinstance(ext_value, str):
            attr_name = ext_key.split(":")[-1] if ":" in ext_key else ext_key
            attributes[attr_name] = ext_value


def _extract_external_ids(external_ids: list, attributes: dict[str, str]) -> None:
    """Extract external ID attributes."""
    for ext_id in external_ids:
        issuer = ext_id.get("Issuer", "")
        ext_id_value = ext_id.get("Id", "")
        if issuer and ext_id_value:
            attributes[f"externalId_{issuer}"] = ext_id_value


def _fetch_users_from_identity_store(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
) -> list[dict]:
    """Fetch all users with their attributes from Identity Store.

    Uses list_users to get user IDs, then describe_user with Extensions
    to get full attributes including enterprise extension (department, etc.).

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        List of user dictionaries with attributes.
    """
    users: list[dict] = []

    # First, list all users to get their IDs
    paginator = identity_store_client.get_paginator("list_users")
    for page in paginator.paginate(IdentityStoreId=identity_store_id):
        for user in page.get("Users", []):
            user_id = user.get("UserId")
            if not user_id:
                continue

            # Call describe_user with Extensions to get enterprise attributes
            try:
                full_user = identity_store_client.describe_user(
                    IdentityStoreId=identity_store_id,
                    UserId=user_id,
                    Extensions=["aws:identitystore:enterprise"],
                )
            except Exception as e:
                logger.exception(f"Failed to describe user {user_id}: {e}")
                full_user = user

            extracted_attrs = _extract_user_attributes(full_user)
            user_email = _extract_user_email(full_user)
            # Log raw user data keys for debugging attribute extraction
            logger.debug(
                f"Raw user data keys for '{mask_email(user_email)}': {list(full_user.keys())}, "
                f"extracted attributes: {_filter_sensitive_attributes(extracted_attrs)}"
            )
            users.append(
                {
                    "user_id": user_id,
                    "username": full_user.get("UserName", ""),
                    "email": user_email,
                    "attributes": extracted_attrs,
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
) -> list[UserInfo]:
    """Get all users with their attributes from Identity Store.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        List of UserInfo objects with user attributes.
    """
    users_data = _fetch_users_from_identity_store(identity_store_client, identity_store_id)

    # Convert dictionaries to UserInfo objects
    return [
        UserInfo(
            user_id=user["user_id"],
            email=user["email"],
            attributes=user["attributes"],
        )
        for user in users_data
    ]


def get_all_groups(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
) -> dict[str, str]:
    """Get all groups from Identity Store.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        Dictionary mapping group names to IDs.
    """
    return _fetch_groups_from_identity_store(identity_store_client, identity_store_id)


def get_managed_groups(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    all_groups: dict[str, str],
    managed_group_names: list[str],
) -> dict[str, GroupMembershipState]:
    """Get managed groups with their current membership state.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        all_groups: Dictionary mapping group names to IDs.
        managed_group_names: List of group names to manage.

    Returns:
        Dictionary mapping group IDs to GroupMembershipState.
    """
    current_state: dict[str, GroupMembershipState] = {}

    for group_name in managed_group_names:
        group_id = all_groups.get(group_name)
        if group_id:
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

    logger.info(f"Retrieved {len(current_state)} of {len(managed_group_names)} managed groups")
    return current_state


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
