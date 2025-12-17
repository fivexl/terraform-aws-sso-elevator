"""Slack notifications for attribute-based group sync operations.

This module provides functions to send Slack notifications for:
- Users added to groups via attribute sync
- Manual assignments detected
- Manual assignments removed
- Sync operation errors
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import get_config, get_logger

if TYPE_CHECKING:
    from slack_sdk import WebClient

    from sync_state import SyncAction

logger = get_logger(service="sync_notifications")
cfg = get_config()


@dataclass
class SyncNotificationResult:
    """Result of a notification attempt."""

    success: bool
    message: str
    error: str | None = None


def format_attributes(attributes: dict[str, str] | None) -> str:
    """Format attributes dictionary for display in Slack message.

    Args:
        attributes: Dictionary of attribute name to value, or None.

    Returns:
        Formatted string for Slack display.
    """
    if not attributes:
        return "N/A"
    return ", ".join(f"{k}={v}" for k, v in sorted(attributes.items()))


def notify_user_added_to_group(
    slack_client: WebClient,
    action: SyncAction,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send Slack notification when a user is added to a group via attribute sync.

    **Feature: attribute-based-group-sync, Property 14: User addition notification**
    **Validates: Requirements 6.2**

    Args:
        slack_client: Slack WebClient instance.
        action: The SyncAction describing the add operation.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    target_channel = channel_id or cfg.slack_channel_id

    attrs_display = format_attributes(action.matched_attributes)
    text = (
        f":white_check_mark: *Attribute Sync: User Added to Group*\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Matched Attributes: {attrs_display}\n"
        f"• Reason: {action.reason}"
    )

    try:
        slack_client.chat_postMessage(
            channel=target_channel,
            text=text,
        )
        logger.info(f"Sent user addition notification for {action.user_email} to group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send user addition notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_manual_assignment_detected(
    slack_client: WebClient,
    action: SyncAction,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send Slack notification when a manual assignment is detected.

    **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
    **Validates: Requirements 3.3, 3.4, 4.5**

    Args:
        slack_client: Slack WebClient instance.
        action: The SyncAction describing the manual assignment.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    target_channel = channel_id or cfg.slack_channel_id

    expected_attrs = format_attributes(action.matched_attributes)
    text = (
        f":warning: *Attribute Sync: Manual Assignment Detected*\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Expected Attributes: {expected_attrs}\n"
        f"• Reason: {action.reason}\n"
        f"_This user was added to the group manually and does not match the attribute rules._"
    )

    try:
        slack_client.chat_postMessage(
            channel=target_channel,
            text=text,
        )
        logger.info(f"Sent manual assignment detection notification for {action.user_email} in group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send manual assignment detection notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_manual_assignment_removed(
    slack_client: WebClient,
    action: SyncAction,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send Slack notification when a manual assignment is removed.

    **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
    **Validates: Requirements 3.3, 3.4, 4.5**

    Args:
        slack_client: Slack WebClient instance.
        action: The SyncAction describing the removal.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    target_channel = channel_id or cfg.slack_channel_id

    expected_attrs = format_attributes(action.matched_attributes)
    text = (
        f":x: *Attribute Sync: Manual Assignment Removed*\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Expected Attributes: {expected_attrs}\n"
        f"• Reason: {action.reason}\n"
        f"_This user was removed because they don't match the attribute rules and the policy is set to 'remove'._"
    )

    try:
        slack_client.chat_postMessage(
            channel=target_channel,
            text=text,
        )
        logger.info(f"Sent manual assignment removal notification for {action.user_email} from group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send manual assignment removal notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_sync_error(
    slack_client: WebClient,
    error_message: str,
    error_count: int = 1,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send Slack notification when sync operation encounters errors.

    **Validates: Requirements 5.5**

    Args:
        slack_client: Slack WebClient instance.
        error_message: Description of the error(s).
        error_count: Number of errors encountered.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    target_channel = channel_id or cfg.slack_channel_id

    text = f":rotating_light: *Attribute Sync: Error Encountered*\n• Error Count: {error_count}\n• Details: {error_message}"

    try:
        slack_client.chat_postMessage(
            channel=target_channel,
            text=text,
        )
        logger.info(f"Sent sync error notification: {error_count} error(s)")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send sync error notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


@dataclass
class SyncSummary:
    """Summary of a sync operation for notification purposes."""

    users_evaluated: int
    groups_processed: int
    users_added: int
    users_removed: int
    manual_assignments_detected: int
    manual_assignments_removed: int
    errors: list[str]


def notify_sync_summary(
    slack_client: WebClient,
    summary: SyncSummary,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send Slack notification with sync operation summary.

    Args:
        slack_client: Slack WebClient instance.
        summary: SyncSummary with operation statistics.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    target_channel = channel_id or cfg.slack_channel_id

    # Determine emoji based on errors
    if summary.errors:
        emoji = ":warning:"
        status = "Completed with Errors"
    else:
        emoji = ":white_check_mark:"
        status = "Completed Successfully"

    text = (
        f"{emoji} *Attribute Sync: {status}*\n"
        f"• Users Evaluated: {summary.users_evaluated}\n"
        f"• Groups Processed: {summary.groups_processed}\n"
        f"• Users Added: {summary.users_added}\n"
        f"• Users Removed: {summary.users_removed}\n"
        f"• Manual Assignments Detected: {summary.manual_assignments_detected}\n"
        f"• Manual Assignments Removed: {summary.manual_assignments_removed}"
    )

    max_errors_to_display = 5
    if summary.errors:
        error_text = "\n".join(f"  - {e}" for e in summary.errors[:max_errors_to_display])
        if len(summary.errors) > max_errors_to_display:
            error_text += f"\n  ... and {len(summary.errors) - max_errors_to_display} more errors"
        text += f"\n• Errors ({len(summary.errors)}):\n{error_text}"

    try:
        slack_client.chat_postMessage(
            channel=target_channel,
            text=text,
        )
        logger.info(f"Sent sync summary notification: {summary.users_added} added, {summary.users_removed} removed")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send sync summary notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def send_notification_for_action(
    slack_client: WebClient,
    action: SyncAction,
    channel_id: str | None = None,
) -> SyncNotificationResult:
    """Send appropriate Slack notification based on action type.

    This is a convenience function that routes to the appropriate
    notification function based on the action type.

    Args:
        slack_client: Slack WebClient instance.
        action: The SyncAction to notify about.
        channel_id: Optional channel ID override (defaults to config).

    Returns:
        SyncNotificationResult indicating success or failure.
    """
    if action.action_type == "add":
        return notify_user_added_to_group(slack_client, action, channel_id)
    elif action.action_type == "warn":
        return notify_manual_assignment_detected(slack_client, action, channel_id)
    elif action.action_type == "remove":
        return notify_manual_assignment_removed(slack_client, action, channel_id)
    else:
        logger.warning(f"Unknown action type: {action.action_type}")
        return SyncNotificationResult(
            success=False,
            message=f"Unknown action type: {action.action_type}",
            error="Invalid action type",
        )
