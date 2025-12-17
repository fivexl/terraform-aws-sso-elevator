"""Attribute mapping engine for evaluating users against attribute-based group rules.

This module provides the core logic for matching user attributes against
configured mapping rules to determine group membership.
"""

from dataclasses import dataclass

from config import get_logger

logger = get_logger(service="attribute_mapper")


@dataclass(frozen=True)
class AttributeCondition:
    """Single attribute condition (e.g., department = "Engineering").

    Represents a condition that checks if a user's attribute matches
    an expected value using exact string matching.
    """

    attribute_name: str
    expected_value: str

    def matches(self, user_attributes: dict[str, str]) -> bool:
        """Check if user attributes satisfy this condition.

        Args:
            user_attributes: Dictionary of user attribute name to value.

        Returns:
            True if the user has the attribute and it exactly matches
            the expected value, False otherwise.
        """
        actual_value = user_attributes.get(self.attribute_name)
        return actual_value == self.expected_value


@dataclass(frozen=True)
class AttributeMappingRule:
    """Complete mapping rule for a group.

    A rule defines which users should be members of a group based on
    their attributes. All conditions must match (AND logic).
    """

    group_name: str
    group_id: str
    conditions: tuple[AttributeCondition, ...]

    def matches(self, user_attributes: dict[str, str]) -> bool:
        """Check if user matches ALL conditions (AND logic).

        Args:
            user_attributes: Dictionary of user attribute name to value.

        Returns:
            True if all conditions match, False if any condition fails.
            Returns False if there are no conditions.
        """
        if not self.conditions:
            return False

        all_match = True
        for condition in self.conditions:
            actual_value = user_attributes.get(condition.attribute_name)
            condition_matches = actual_value == condition.expected_value
            if not condition_matches:
                all_match = False
                logger.debug(
                    f"Condition mismatch for group '{self.group_name}': "
                    f"attribute '{condition.attribute_name}' - "
                    f"expected '{condition.expected_value}', got '{actual_value}'"
                )

        return all_match


class AttributeMapper:
    """Evaluates users against mapping rules to determine group membership."""

    def __init__(self, rules: list[AttributeMappingRule]) -> None:
        """Initialize the mapper with a list of rules.

        Args:
            rules: List of attribute mapping rules to evaluate against.
        """
        self._rules = rules
        self._rules_by_group: dict[str, AttributeMappingRule] = {rule.group_id: rule for rule in rules}

    @property
    def rules(self) -> list[AttributeMappingRule]:
        """Get the list of mapping rules."""
        return self._rules

    def get_target_groups_for_user(self, user_attributes: dict[str, str]) -> set[str]:
        """Return set of group IDs the user should belong to.

        Evaluates the user's attributes against all rules and returns
        the IDs of groups where the user matches the rule conditions.

        Args:
            user_attributes: Dictionary of user attribute name to value.

        Returns:
            Set of group IDs that the user should be a member of.
        """
        matching_groups: set[str] = set()

        for rule in self._rules:
            if rule.matches(user_attributes):
                matching_groups.add(rule.group_id)
                logger.debug(
                    f"User matches rule for group '{rule.group_name}': "
                    f"user_attributes={user_attributes}, "
                    f"rule_conditions={[(c.attribute_name, c.expected_value) for c in rule.conditions]}"
                )

        return matching_groups

    def get_rule_for_group(self, group_id: str) -> AttributeMappingRule | None:
        """Get the mapping rule for a specific group.

        Args:
            group_id: The ID of the group to look up.

        Returns:
            The mapping rule for the group, or None if no rule exists.
        """
        return self._rules_by_group.get(group_id)
