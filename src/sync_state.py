"""Sync state manager for attribute-based group sync.

This module tracks current vs desired group membership state and computes
the required sync actions (add, remove, warn).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from attribute_mapper import AttributeMapper


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

            # Compute desired members for this group
            desired_members: set[str] = set()
            for user in users:
                target_groups = self._mapper.get_target_groups_for_user(user.attributes)
                if group_id in target_groups:
                    desired_members.add(user.user_id)

            # Find users to add (in desired but not in current)
            users_to_add = desired_members - state.current_members
            for user_id in users_to_add:
                user = users_by_id.get(user_id)
                if user:
                    matched_attrs = self._get_matched_attributes(user.attributes, rule)
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
                    # Determine action based on policy
                    if self._manual_assignment_policy == "remove":
                        action_type: Literal["add", "remove", "warn"] = "remove"
                        reason = f"User does not match attribute rules for group '{group_name}' and policy is 'remove'"
                    else:
                        action_type = "warn"
                        reason = f"User does not match attribute rules for group '{group_name}' (manual assignment detected)"

                    expected_attrs = self._get_expected_attributes(rule) if rule else None
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
