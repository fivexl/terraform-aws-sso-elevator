"""Sync configuration loader for attribute-based group sync.

This module provides configuration loading and validation for the
attribute-based group synchronization feature.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from config import get_logger

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient

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
        if attributes is None:
            errors.append(f"Rule {i}: missing 'attributes' field")
        elif not isinstance(attributes, dict):
            errors.append(f"Rule {i}: 'attributes' must be a dictionary")
        elif len(attributes) == 0:
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


def get_all_groups_from_identity_store(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
) -> dict[str, str]:
    """Query Identity Store for all groups and return name-to-ID mapping.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.

    Returns:
        Dictionary mapping group names to their IDs.
    """
    name_to_id: dict[str, str] = {}

    try:
        paginator = identity_store_client.get_paginator("list_groups")
        for page in paginator.paginate(IdentityStoreId=identity_store_id):
            for group in page.get("Groups", []):
                display_name = group.get("DisplayName")
                group_id = group.get("GroupId")
                if display_name and group_id:
                    name_to_id[display_name] = group_id
    except Exception as e:
        logger.exception(f"Failed to list groups from Identity Store: {e}")
        raise

    logger.info(f"Retrieved {len(name_to_id)} groups from Identity Store")
    return name_to_id


def resolve_group_names_from_identity_store(
    config: SyncConfiguration,
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    cached_groups: dict[str, str] | None = None,
) -> tuple[SyncConfiguration, dict[str, str]]:
    """Resolve group names to IDs by querying Identity Store.

    This function queries the Identity Store for all groups and resolves
    the managed group names to their IDs. It supports caching to minimize
    API calls.

    Args:
        config: The sync configuration with group names.
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        cached_groups: Optional cached name-to-ID mapping to use first.

    Returns:
        Tuple of (updated SyncConfiguration, name-to-ID mapping for caching).

    Note:
        Missing groups are logged as warnings but do not cause failures.
        The returned configuration will only contain IDs for groups that
        were successfully resolved.
    """
    # Use cached groups if available, otherwise query Identity Store
    if cached_groups is not None:
        name_to_id = cached_groups
        logger.debug("Using cached group name-to-ID mapping")
    else:
        name_to_id = get_all_groups_from_identity_store(identity_store_client, identity_store_id)

    # Resolve group names using the mapping
    resolved_config = resolve_group_names(config, name_to_id)

    # Log any groups that couldn't be resolved
    missing_groups = [name for name in config.managed_group_names if name not in resolved_config.managed_group_ids]
    if missing_groups:
        for group_name in missing_groups:
            logger.error(f"Group '{group_name}' not found in Identity Store - rules referencing this group will be skipped")

    return resolved_config, name_to_id


def get_valid_rules_for_resolved_groups(
    config: SyncConfiguration,
) -> list[dict]:
    """Get mapping rules that reference resolved groups only.

    This function filters the mapping rules to only include those that
    reference groups that have been successfully resolved to IDs.

    Args:
        config: The sync configuration with resolved group IDs.

    Returns:
        List of valid mapping rules (those referencing resolved groups).

    Note:
        Rules referencing unresolved groups are logged as errors and skipped.
    """
    valid_rules: list[dict] = []
    resolved_group_names = set(config.managed_group_ids.keys())

    for rule in config.mapping_rules:
        group_name = rule.get("group_name")
        if group_name in resolved_group_names:
            valid_rules.append(rule)
        else:
            logger.error(f"Skipping rule for group '{group_name}' - group not found in Identity Store")

    logger.info(
        f"Validated {len(valid_rules)} of {len(config.mapping_rules)} rules "
        f"({len(config.mapping_rules) - len(valid_rules)} skipped due to missing groups)"
    )

    return valid_rules
