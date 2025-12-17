"""Sync configuration loader for attribute-based group sync.

This module provides configuration loading and validation for the
attribute-based group synchronization feature.
"""

import json
import os
from dataclasses import dataclass
from typing import Literal

from config import get_logger

logger = get_logger(service="sync_config")


class SyncConfigurationError(Exception):
    """Raised when sync configuration is invalid."""


@dataclass(frozen=True)
class SyncConfiguration:
    """Complete sync configuration for attribute-based group sync.

    Attributes:
        enabled: Whether the sync feature is enabled.
        managed_group_names: List of group names to manage.
        managed_group_ids: Resolved name -> ID mapping (populated at runtime).
        mapping_rules: List of attribute mapping rules as raw dicts.
        manual_assignment_policy: Policy for handling manual assignments.
        schedule_expression: EventBridge schedule expression.
    """

    enabled: bool
    managed_group_names: tuple[str, ...]
    managed_group_ids: dict[str, str]
    mapping_rules: tuple[dict, ...]
    manual_assignment_policy: Literal["warn", "remove"]
    schedule_expression: str

    def get_group_id(self, group_name: str) -> str | None:
        """Get the group ID for a given group name.

        Args:
            group_name: The name of the group.

        Returns:
            The group ID if found, None otherwise.
        """
        return self.managed_group_ids.get(group_name)


def load_sync_config_from_env() -> SyncConfiguration:
    """Load sync configuration from environment variables.

    Environment variables:
        ATTRIBUTE_SYNC_ENABLED: "true" or "false" (default: "false")
        ATTRIBUTE_SYNC_MANAGED_GROUPS: JSON array of group names
        ATTRIBUTE_SYNC_RULES: JSON array of mapping rules
        ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY: "warn" or "remove" (default: "warn")
        ATTRIBUTE_SYNC_SCHEDULE: Schedule expression (default: "rate(1 hour)")

    Returns:
        SyncConfiguration with values from environment.

    Raises:
        SyncConfigurationError: If configuration is invalid.
    """
    enabled_str = os.environ.get("ATTRIBUTE_SYNC_ENABLED", "false").lower()
    enabled = enabled_str == "true"

    # Parse managed groups
    managed_groups_str = os.environ.get("ATTRIBUTE_SYNC_MANAGED_GROUPS", "[]")
    try:
        managed_groups_raw = json.loads(managed_groups_str)
        if not isinstance(managed_groups_raw, list):
            raise SyncConfigurationError("ATTRIBUTE_SYNC_MANAGED_GROUPS must be a JSON array")
        managed_group_names = tuple(str(g) for g in managed_groups_raw)
    except json.JSONDecodeError as e:
        raise SyncConfigurationError(f"Invalid JSON in ATTRIBUTE_SYNC_MANAGED_GROUPS: {e}") from e

    # Parse mapping rules
    rules_str = os.environ.get("ATTRIBUTE_SYNC_RULES", "[]")
    try:
        rules_raw = json.loads(rules_str)
        if not isinstance(rules_raw, list):
            raise SyncConfigurationError("ATTRIBUTE_SYNC_RULES must be a JSON array")
        mapping_rules = tuple(rules_raw)
    except json.JSONDecodeError as e:
        raise SyncConfigurationError(f"Invalid JSON in ATTRIBUTE_SYNC_RULES: {e}") from e

    # Parse manual assignment policy
    policy_str = os.environ.get("ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY", "warn").lower()
    if policy_str not in ("warn", "remove"):
        raise SyncConfigurationError(f"ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY must be 'warn' or 'remove', got '{policy_str}'")
    manual_assignment_policy: Literal["warn", "remove"] = policy_str  # type: ignore[assignment]

    # Parse schedule expression
    schedule_expression = os.environ.get("ATTRIBUTE_SYNC_SCHEDULE", "rate(1 hour)")

    return SyncConfiguration(
        enabled=enabled,
        managed_group_names=managed_group_names,
        managed_group_ids={},  # Will be populated by resolve_group_names
        mapping_rules=mapping_rules,
        manual_assignment_policy=manual_assignment_policy,
        schedule_expression=schedule_expression,
    )


def validate_sync_config(config: SyncConfiguration) -> list[str]:
    """Validate sync configuration and return list of errors.

    Validates:
        - If enabled, managed_group_names must not be empty
        - If enabled, mapping_rules must not be empty
        - All rules must reference groups in managed_group_names
        - All rules must have valid structure (group_name and attributes)

    Args:
        config: The sync configuration to validate.

    Returns:
        List of validation error messages. Empty if valid.
    """
    errors: list[str] = []

    if not config.enabled:
        # No validation needed if disabled
        return errors

    # Check managed groups
    if not config.managed_group_names:
        errors.append("attribute_sync_managed_groups must not be empty when attribute_sync_enabled is true")

    # Check mapping rules
    if not config.mapping_rules:
        errors.append("attribute_sync_rules must not be empty when attribute_sync_enabled is true")

    # Validate each rule
    managed_groups_set = set(config.managed_group_names)
    for i, rule in enumerate(config.mapping_rules):
        if not isinstance(rule, dict):
            errors.append(f"Rule {i}: must be a dictionary")
            continue

        # Check group_name
        group_name = rule.get("group_name")
        if not group_name:
            errors.append(f"Rule {i}: missing 'group_name' field")
        elif group_name not in managed_groups_set:
            errors.append(f"Rule {i}: group '{group_name}' is not in managed_groups list")

        # Check attributes
        attributes = rule.get("attributes")
        if not attributes:
            errors.append(f"Rule {i}: missing 'attributes' field")
        elif not isinstance(attributes, dict):
            errors.append(f"Rule {i}: 'attributes' must be a dictionary")
        elif not attributes:
            errors.append(f"Rule {i}: 'attributes' must not be empty")

    return errors


def load_sync_config() -> SyncConfiguration:
    """Load and validate sync configuration from environment.

    This is the main entry point for loading sync configuration.
    It loads from environment variables and validates the configuration.

    Returns:
        Validated SyncConfiguration.

    Raises:
        SyncConfigurationError: If configuration is invalid.
    """
    config = load_sync_config_from_env()

    errors = validate_sync_config(config)
    if errors:
        error_msg = "Sync configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.error(error_msg)
        raise SyncConfigurationError(error_msg)

    return config


def resolve_group_names(
    config: SyncConfiguration,
    group_name_to_id: dict[str, str],
) -> SyncConfiguration:
    """Resolve group names to IDs and return updated configuration.

    Args:
        config: The sync configuration with group names.
        group_name_to_id: Mapping of group names to their IDs.

    Returns:
        New SyncConfiguration with managed_group_ids populated.

    Raises:
        SyncConfigurationError: If any managed group cannot be resolved.
    """
    resolved_ids: dict[str, str] = {}
    missing_groups: list[str] = []

    for group_name in config.managed_group_names:
        group_id = group_name_to_id.get(group_name)
        if group_id:
            resolved_ids[group_name] = group_id
        else:
            missing_groups.append(group_name)

    if missing_groups:
        logger.warning(f"Could not resolve group names: {missing_groups}")

    # Return new config with resolved IDs
    return SyncConfiguration(
        enabled=config.enabled,
        managed_group_names=config.managed_group_names,
        managed_group_ids=resolved_ids,
        mapping_rules=config.mapping_rules,
        manual_assignment_policy=config.manual_assignment_policy,
        schedule_expression=config.schedule_expression,
    )
