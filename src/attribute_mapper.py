"""Attribute mapping engine for evaluating users against attribute-based group rules.

This module provides the core logic for matching user attributes against
configured mapping rules to determine group membership.
"""

from dataclasses import dataclass

from config import get_logger

logger = get_logger(service="attribute_mapper")


_EMAIL_MASK_PREFIX_LEN = 5
_EMAIL_MASK_SUFFIX_LEN = 5
_EMAIL_MIN_LENGTH_FOR_MASKING = _EMAIL_MASK_PREFIX_LEN + _EMAIL_MASK_SUFFIX_LEN


def mask_email(email: str) -> str:
    """Mask email for logging - show first 5 and last 5 characters only.

    Args:
        email: The email address to mask.

    Returns:
        Masked email like "john.*****.com" or original if too short.
    """
    if len(email) <= _EMAIL_MIN_LENGTH_FOR_MASKING:
        return email
    return f"{email[:_EMAIL_MASK_PREFIX_LEN]}*****{email[-_EMAIL_MASK_SUFFIX_LEN:]}"


def _normalize_for_comparison(value: str | None) -> str:
    """Normalize a value for case-insensitive comparison.

    Args:
        value: The value to normalize.

    Returns:
        Lowercase version of the value, or empty string if None.
    """
    return value.lower() if value else ""


@dataclass(frozen=True)
class AttributeCondition:
    """Single attribute condition (e.g., department = "Engineering").

    Represents a condition that checks if a user's attribute matches
    an expected value using exact string matching.
    """

    attribute_name: str
    expected_value: str

    def matches(self, user_attributes: dict[str, str]) -> bool:
        """Check if user attributes satisfy this condition (case-insensitive).

        Args:
            user_attributes: Dictionary of user attribute name to value.

        Returns:
            True if the user has the attribute and it matches
            the expected value (case-insensitive), False otherwise.
        """
        # Case-insensitive attribute name lookup
        normalized_attr_name = _normalize_for_comparison(self.attribute_name)
        actual_value = None
        for attr_name, attr_value in user_attributes.items():
            if _normalize_for_comparison(attr_name) == normalized_attr_name:
                actual_value = attr_value
                break

        # Case-insensitive value comparison
        return _normalize_for_comparison(actual_value) == _normalize_for_comparison(self.expected_value)


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
            # Case-insensitive attribute name lookup
            normalized_attr_name = _normalize_for_comparison(condition.attribute_name)
            actual_value = None
            for attr_name, attr_value in user_attributes.items():
                if _normalize_for_comparison(attr_name) == normalized_attr_name:
                    actual_value = attr_value
                    break

            # Case-insensitive value comparison
            condition_matches = _normalize_for_comparison(actual_value) == _normalize_for_comparison(condition.expected_value)
            if not condition_matches:
                all_match = False
                logger.debug(
                    f"Condition mismatch for group '{self.group_name}': "
                    f"attribute '{condition.attribute_name}' - "
                    f"expected '{condition.expected_value}' (normalized: '{_normalize_for_comparison(condition.expected_value)}'), "
                    f"got '{actual_value}' (normalized: '{_normalize_for_comparison(actual_value)}')"
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
