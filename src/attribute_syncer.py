"""Main attribute syncer Lambda function for attribute-based group sync.

This module provides the Lambda handler and orchestration logic for
automatically synchronizing users to groups based on their attributes.

**Feature: attribute-based-group-sync**
**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 5.3, 5.4, 5.5, 8.1, 8.2, 8.3, 8.4, 8.5**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import boto3
from slack_sdk import WebClient

import cache as cache_module
import s3 as s3_module
from attribute_mapper import AttributeCondition, AttributeMappingRule, AttributeMapper
from config import get_logger
from sync_config import (
    SyncConfiguration,
    SyncConfigurationError,
    load_sync_config,
    resolve_group_names_from_identity_store,
    get_valid_rules_for_resolved_groups,
)
from sync_notifications import (
    SyncSummary,
    notify_sync_error,
    notify_sync_summary,
    send_notification_for_action,
)
from sync_state import (
    SyncAction,
    SyncStateManager,
    get_managed_groups,
    get_users_with_attributes,
)

if TYPE_CHECKING:
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_s3 import S3Client

logger = get_logger(service="attribute_syncer")

# Module-level boto3 clients for better Lambda cold start performance
# These are initialized once per container and reused across invocations
_identity_store_client: IdentityStoreClient = boto3.client("identitystore")
_s3_client: S3Client = boto3.client("s3")


@dataclass
class SyncOperationResult:
    """Result of a sync operation.

    **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
    **Validates: Requirements 5.3, 5.4**
    """

    start_time: datetime
    end_time: datetime | None = None
    success: bool = False

    # Statistics
    users_evaluated: int = 0
    groups_processed: int = 0
    users_added: int = 0
    users_removed: int = 0
    manual_assignments_detected: int = 0
    manual_assignments_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_summary(self) -> SyncSummary:
        """Convert to SyncSummary for notifications."""
        return SyncSummary(
            users_evaluated=self.users_evaluated,
            groups_processed=self.groups_processed,
            users_added=self.users_added,
            users_removed=self.users_removed,
            manual_assignments_detected=self.manual_assignments_detected,
            manual_assignments_removed=self.manual_assignments_removed,
            errors=self.errors,
        )

    def log_start(self) -> None:
        """Log the start of a sync operation.

        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.3**
        """
        logger.info(
            "Attribute sync operation started",
            extra={
                "operation": "sync_start",
                "start_time": self.start_time.isoformat(),
            },
        )

    def log_completion(self) -> None:
        """Log the completion of a sync operation.

        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.4**
        """
        duration_ms = None
        if self.end_time:
            duration_ms = int((self.end_time - self.start_time).total_seconds() * 1000)

        logger.info(
            "Attribute sync operation completed",
            extra={
                "operation": "sync_complete",
                "start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat() if self.end_time else None,
                "duration_ms": duration_ms,
                "success": self.success,
                "users_evaluated": self.users_evaluated,
                "groups_processed": self.groups_processed,
                "users_added": self.users_added,
                "users_removed": self.users_removed,
                "manual_assignments_detected": self.manual_assignments_detected,
                "manual_assignments_removed": self.manual_assignments_removed,
                "error_count": len(self.errors),
            },
        )


def _build_mapping_rules(
    config: SyncConfiguration,
) -> list[AttributeMappingRule]:
    """Build AttributeMappingRule objects from configuration.

    Args:
        config: The sync configuration with resolved group IDs.

    Returns:
        List of AttributeMappingRule objects.
    """
    valid_rules = get_valid_rules_for_resolved_groups(config)
    mapping_rules: list[AttributeMappingRule] = []

    for rule_dict in valid_rules:
        group_name = rule_dict.get("group_name", "")
        group_id = config.get_group_id(group_name)
        if not group_id:
            continue

        attributes = rule_dict.get("attributes", {})
        conditions = tuple(
            AttributeCondition(attribute_name=attr_name, expected_value=attr_value) for attr_name, attr_value in attributes.items()
        )

        mapping_rules.append(
            AttributeMappingRule(
                group_name=group_name,
                group_id=group_id,
                conditions=conditions,
            )
        )

    return mapping_rules


def _execute_add_action(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    action: SyncAction,
) -> bool:
    """Execute an add action by adding a user to a group.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        action: The add action to execute.

    Returns:
        True if successful, False otherwise.
    """
    try:
        identity_store_client.create_group_membership(
            IdentityStoreId=identity_store_id,
            GroupId=action.group_id,
            MemberId={"UserId": action.user_id},
        )
        logger.info(
            f"Added user {action.user_email} to group {action.group_name}",
            extra={
                "operation": "add_user",
                "user_id": action.user_id,
                "user_email": action.user_email,
                "group_id": action.group_id,
                "group_name": action.group_name,
            },
        )
        return True
    except Exception as e:
        logger.exception(f"Failed to add user {action.user_email} to group {action.group_name}: {e}")
        return False


def _execute_remove_action(
    identity_store_client: IdentityStoreClient,
    identity_store_id: str,
    action: SyncAction,
    membership_id: str | None = None,
) -> bool:
    """Execute a remove action by removing a user from a group.

    Args:
        identity_store_client: The Identity Store client.
        identity_store_id: The Identity Store ID.
        action: The remove action to execute.
        membership_id: Optional membership ID (if known).

    Returns:
        True if successful, False otherwise.
    """
    try:
        # If we don't have the membership ID, we need to find it
        if not membership_id:
            paginator = identity_store_client.get_paginator("list_group_memberships")
            for page in paginator.paginate(IdentityStoreId=identity_store_id, GroupId=action.group_id):
                for membership in page.get("GroupMemberships", []):
                    member_id = membership.get("MemberId", {})
                    if member_id.get("UserId") == action.user_id:
                        membership_id = membership.get("MembershipId")
                        break
                if membership_id:
                    break

        if not membership_id:
            logger.warning(f"Could not find membership ID for user {action.user_email} in group {action.group_name}")
            return False

        identity_store_client.delete_group_membership(
            IdentityStoreId=identity_store_id,
            MembershipId=membership_id,
        )
        logger.info(
            f"Removed user {action.user_email} from group {action.group_name}",
            extra={
                "operation": "remove_user",
                "user_id": action.user_id,
                "user_email": action.user_email,
                "group_id": action.group_id,
                "group_name": action.group_name,
            },
        )
        return True
    except Exception as e:
        logger.exception(f"Failed to remove user {action.user_email} from group {action.group_name}: {e}")
        return False


def _log_audit_entry(action: SyncAction, bucket_name: str, bucket_prefix: str) -> None:
    """Log an audit entry for a sync action.

    Args:
        action: The sync action to log.
        bucket_name: S3 bucket name for audit entries.
        bucket_prefix: S3 key prefix for partitions.
    """
    try:
        # Map action type to operation type
        operation_type_map = {
            "add": "sync_add",
            "remove": "sync_remove",
            "warn": "manual_detected",
        }
        operation_type = operation_type_map.get(action.action_type, "manual_detected")

        audit_params = s3_module.SyncAuditParams(
            operation_type=operation_type,  # type: ignore[arg-type]
            sso_user_principal_id=action.user_id,
            sso_user_email=action.user_email,
            group_id=action.group_id,
            group_name=action.group_name,
            reason=action.reason,
            matched_attributes=action.matched_attributes,
        )
        audit_entry = s3_module.create_sync_audit_entry(audit_params)
        s3_module.log_operation(audit_entry, bucket_name, bucket_prefix)
    except Exception as e:
        logger.exception(f"Failed to log audit entry for action {action.action_type}: {e}")


@dataclass
class SyncContext:
    """Context for sync operations containing all required clients and config."""

    identity_store_client: IdentityStoreClient
    identity_store_id: str
    s3_client: S3Client
    slack_client: WebClient
    config: SyncConfiguration
    cache_config: cache_module.CacheConfig
    slack_channel_id: str
    audit_bucket_name: str
    audit_bucket_prefix: str


def _execute_action(
    ctx: SyncContext,
    action: SyncAction,
    result: SyncOperationResult,
) -> None:
    """Execute a single sync action and update result statistics."""
    if action.action_type == "add":
        success = _execute_add_action(
            identity_store_client=ctx.identity_store_client,
            identity_store_id=ctx.identity_store_id,
            action=action,
        )
        if success:
            result.users_added += 1
            _log_audit_entry(action, ctx.audit_bucket_name, ctx.audit_bucket_prefix)
            send_notification_for_action(ctx.slack_client, action, ctx.slack_channel_id)
        else:
            result.errors.append(f"Failed to add {action.user_email} to {action.group_name}")

    elif action.action_type == "remove":
        success = _execute_remove_action(
            identity_store_client=ctx.identity_store_client,
            identity_store_id=ctx.identity_store_id,
            action=action,
        )
        if success:
            result.users_removed += 1
            result.manual_assignments_removed += 1
            _log_audit_entry(action, ctx.audit_bucket_name, ctx.audit_bucket_prefix)
            send_notification_for_action(ctx.slack_client, action, ctx.slack_channel_id)
        else:
            result.errors.append(f"Failed to remove {action.user_email} from {action.group_name}")

    elif action.action_type == "warn":
        result.manual_assignments_detected += 1
        _log_audit_entry(action, ctx.audit_bucket_name, ctx.audit_bucket_prefix)
        send_notification_for_action(ctx.slack_client, action, ctx.slack_channel_id)


def _finalize_result(result: SyncOperationResult) -> SyncOperationResult:
    """Finalize the sync result with end time and success status."""
    result.success = len(result.errors) == 0
    result.end_time = datetime.now(timezone.utc)
    result.log_completion()
    return result


def perform_sync(ctx: SyncContext) -> SyncOperationResult:  # noqa: PLR0912, PLR0915
    """Perform the main sync operation.

    **Feature: attribute-based-group-sync, Property 18: Error resilience**
    **Validates: Requirements 5.5, 8.1, 8.2, 8.3, 8.4, 8.5**

    Args:
        ctx: SyncContext containing all required clients and configuration.

    Returns:
        SyncOperationResult with statistics and errors.
    """
    result = SyncOperationResult(start_time=datetime.now(timezone.utc))
    result.log_start()

    try:
        # Step 1: Resolve group names to IDs
        logger.info("Resolving group names to IDs")
        try:
            resolved_config, all_groups = resolve_group_names_from_identity_store(
                config=ctx.config,
                identity_store_client=ctx.identity_store_client,
                identity_store_id=ctx.identity_store_id,
                cached_groups=cache_module.get_cached_groups(ctx.s3_client, ctx.cache_config),
            )
        except Exception as e:
            logger.exception(f"Failed to resolve group names: {e}")
            result.errors.append(f"Failed to resolve group names: {e}")
            return _finalize_result(result)

        # Step 2: Build mapping rules
        mapping_rules = _build_mapping_rules(resolved_config)
        if not mapping_rules:
            logger.warning("No valid mapping rules after resolution")
            result.errors.append("No valid mapping rules after resolution")
            return _finalize_result(result)

        mapper = AttributeMapper(mapping_rules)

        # Step 3: Get users with attributes
        logger.info("Fetching users with attributes")
        try:
            users = get_users_with_attributes(
                identity_store_client=ctx.identity_store_client,
                identity_store_id=ctx.identity_store_id,
                s3_client=ctx.s3_client,
                cache_config=ctx.cache_config,
            )
            result.users_evaluated = len(users)
        except Exception as e:
            logger.exception(f"Failed to fetch users: {e}")
            result.errors.append(f"Failed to fetch users: {e}")
            return _finalize_result(result)

        # Step 4: Get managed groups with current membership
        logger.info("Fetching managed groups and membership state")
        try:
            _, current_state = get_managed_groups(
                identity_store_client=ctx.identity_store_client,
                identity_store_id=ctx.identity_store_id,
                s3_client=ctx.s3_client,
                cache_config=ctx.cache_config,
                managed_group_names=list(resolved_config.managed_group_names),
            )
            result.groups_processed = len(current_state)
        except Exception as e:
            logger.exception(f"Failed to fetch managed groups: {e}")
            result.errors.append(f"Failed to fetch managed groups: {e}")
            return _finalize_result(result)

        # Step 5: Create sync state manager and compute actions
        manager = SyncStateManager(
            managed_group_ids=resolved_config.managed_group_ids,
            mapper=mapper,
            manual_assignment_policy=resolved_config.manual_assignment_policy,
        )

        actions = manager.compute_sync_actions(users, current_state)
        logger.info(f"Computed {len(actions)} sync actions")

        # Step 6: Execute actions
        for action in actions:
            try:
                _execute_action(ctx, action, result)
            except Exception as e:
                logger.exception(f"Error executing action {action.action_type} for {action.user_email}: {e}")
                result.errors.append(f"Error executing {action.action_type} for {action.user_email}: {e}")

        # Step 7: Update cache with fresh data
        try:
            cache_module.set_cached_groups(ctx.s3_client, ctx.cache_config, all_groups)
        except Exception as e:
            logger.warning(f"Failed to update groups cache: {e}")

        return _finalize_result(result)

    except Exception as e:
        logger.exception(f"Unexpected error during sync: {e}")
        result.errors.append(f"Unexpected error: {e}")
        return _finalize_result(result)


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    """Lambda handler entry point for attribute sync.

    **Feature: attribute-based-group-sync**
    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 5.3, 5.4, 5.5**

    Args:
        event: Lambda event (typically from EventBridge schedule).
        context: Lambda context.

    Returns:
        Dictionary with sync operation result.
    """
    logger.info("Attribute syncer Lambda invoked", extra={"event": event})

    # Load configuration
    try:
        config = load_sync_config()
    except SyncConfigurationError as e:
        logger.exception(f"Configuration error: {e}")
        return {
            "statusCode": 500,
            "body": {"error": str(e), "success": False},
        }

    if not config.enabled:
        logger.info("Attribute sync is disabled, skipping")
        return {
            "statusCode": 200,
            "body": {"message": "Attribute sync is disabled", "success": True},
        }

    # Initialize Slack client (token may change, so not module-level)
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    slack_client = WebClient(token=slack_bot_token)

    # Get identity store ID from environment
    identity_store_id = os.environ.get("IDENTITY_STORE_ID", "")
    if not identity_store_id:
        logger.error("IDENTITY_STORE_ID environment variable not set")
        return {
            "statusCode": 500,
            "body": {"error": "IDENTITY_STORE_ID not configured", "success": False},
        }

    # Create cache config directly from environment variables
    cache_config = cache_module.CacheConfig(
        bucket_name=os.environ.get("CONFIG_BUCKET_NAME", "sso-elevator-config"),
        enabled=os.environ.get("CACHE_ENABLED", "true").lower() == "true",
    )

    # Get slack channel ID from environment
    slack_channel_id = os.environ.get("SLACK_CHANNEL_ID", "")

    # Get audit bucket config from environment
    audit_bucket_name = os.environ.get("S3_BUCKET_FOR_AUDIT_ENTRY_NAME", "")
    audit_bucket_prefix = os.environ.get("S3_BUCKET_PREFIX_FOR_PARTITIONS", "audit")

    # Create sync context using module-level boto3 clients
    ctx = SyncContext(
        identity_store_client=_identity_store_client,
        identity_store_id=identity_store_id,
        s3_client=_s3_client,
        slack_client=slack_client,
        config=config,
        cache_config=cache_config,
        slack_channel_id=slack_channel_id,
        audit_bucket_name=audit_bucket_name,
        audit_bucket_prefix=audit_bucket_prefix,
    )

    # Perform sync
    result = perform_sync(ctx)

    # Send summary notification only if there were changes or errors
    has_changes = result.users_added > 0 or result.users_removed > 0 or result.manual_assignments_detected > 0 or result.errors

    if has_changes:
        try:
            if result.errors:
                notify_sync_error(
                    slack_client=slack_client,
                    error_message="\n".join(result.errors[:5]),
                    error_count=len(result.errors),
                    channel_id=slack_channel_id,
                )
            notify_sync_summary(slack_client=slack_client, summary=result.to_summary(), channel_id=slack_channel_id)
        except Exception as e:
            logger.exception(f"Failed to send summary notification: {e}")
    else:
        logger.info("No changes detected, skipping notification")

    return {
        "statusCode": 200 if result.success else 500,
        "body": {
            "success": result.success,
            "users_evaluated": result.users_evaluated,
            "groups_processed": result.groups_processed,
            "users_added": result.users_added,
            "users_removed": result.users_removed,
            "manual_assignments_detected": result.manual_assignments_detected,
            "manual_assignments_removed": result.manual_assignments_removed,
            "error_count": len(result.errors),
        },
    }
