"""Notifications for attribute-based group sync operations.

This module provides functions to send notifications (Slack or Teams) for:
- Users added to groups via attribute sync
- Manual assignments detected
- Manual assignments removed
- Sync operation errors
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from config import get_logger

if TYPE_CHECKING:
    from requester.teams.teams_notifier import TeamsNotifier
    from slack_sdk import WebClient

    from sync_state import SyncAction

logger = get_logger(service="sync_notifications")

Notifier = Union["WebClient", "TeamsNotifier"]


@dataclass
class SyncNotificationResult:
    """Result of a notification attempt."""

    success: bool
    message: str
    error: str | None = None


def format_attributes(attributes: dict[str, str] | None) -> str:
    """Format attributes dictionary for display in a notification message."""
    if not attributes:
        return "N/A"
    return ", ".join(f"{k}={v}" for k, v in sorted(attributes.items()))


def _slack_send(notifier: Notifier, channel_id: str, text: str) -> None:
    """Send via Slack WebClient or Teams TeamsNotifier."""
    from requester.teams.teams_notifier import TeamsNotifier

    if isinstance(notifier, TeamsNotifier):
        asyncio.run(notifier.send_channel_text(text))
    else:
        notifier.chat_postMessage(channel=channel_id, text=text)  # type: ignore[attr-defined]


def notify_user_added_to_group(
    notifier: Notifier,
    action: SyncAction,
    channel_id: str,
) -> SyncNotificationResult:
    """Send notification when a user is added to a group via attribute sync."""
    attrs_display = format_attributes(action.matched_attributes)
    text = (
        f"Attribute Sync: User Added to Group\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Matched Attributes: {attrs_display}\n"
        f"• Reason: {action.reason}"
    )
    try:
        _slack_send(notifier, channel_id, text)
        logger.info(f"Sent user addition notification for {action.user_email} to group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send user addition notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_manual_assignment_detected(
    notifier: Notifier,
    action: SyncAction,
    channel_id: str,
) -> SyncNotificationResult:
    """Send notification when a manual assignment is detected."""
    expected_attrs = format_attributes(action.matched_attributes)
    text = (
        f"Attribute Sync: Manual Assignment Detected\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Expected Attributes: {expected_attrs}\n"
        f"• Reason: {action.reason}\n"
        f"This user was added to the group manually and does not match the attribute rules."
    )
    try:
        _slack_send(notifier, channel_id, text)
        logger.info(f"Sent manual assignment detection notification for {action.user_email} in group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send manual assignment detection notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_manual_assignment_removed(
    notifier: Notifier,
    action: SyncAction,
    channel_id: str,
) -> SyncNotificationResult:
    """Send notification when a manual assignment is removed."""
    expected_attrs = format_attributes(action.matched_attributes)
    text = (
        f"Attribute Sync: Manual Assignment Removed\n"
        f"• User: {action.user_email}\n"
        f"• Group: {action.group_name}\n"
        f"• Expected Attributes: {expected_attrs}\n"
        f"• Reason: {action.reason}\n"
        f"This user was removed because they don't match the attribute rules and the policy is set to 'remove'."
    )
    try:
        _slack_send(notifier, channel_id, text)
        logger.info(f"Sent manual assignment removal notification for {action.user_email} from group {action.group_name}")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send manual assignment removal notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def notify_sync_error(
    notifier: Notifier,
    error_message: str,
    error_count: int = 1,
    channel_id: str = "",
) -> SyncNotificationResult:
    """Send notification when sync operation encounters errors."""
    text = f"Attribute Sync: Error Encountered\n• Error Count: {error_count}\n• Details: {error_message}"
    try:
        _slack_send(notifier, channel_id, text)
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
    notifier: Notifier,
    summary: SyncSummary,
    channel_id: str = "",
) -> SyncNotificationResult:
    """Send notification with sync operation summary."""
    status = "Completed with Errors" if summary.errors else "Completed Successfully"

    text = (
        f"Attribute Sync: {status}\n"
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
        _slack_send(notifier, channel_id, text)
        logger.info(f"Sent sync summary notification: {summary.users_added} added, {summary.users_removed} removed")
        return SyncNotificationResult(success=True, message="Notification sent successfully")
    except Exception as e:
        logger.exception(f"Failed to send sync summary notification: {e}")
        return SyncNotificationResult(success=False, message="Failed to send notification", error=str(e))


def send_notification_for_action(
    notifier: Notifier,
    action: SyncAction,
    channel_id: str,
) -> SyncNotificationResult:
    """Send appropriate notification based on action type."""
    if action.action_type == "add":
        return notify_user_added_to_group(notifier, action, channel_id)
    elif action.action_type == "warn":
        return notify_manual_assignment_detected(notifier, action, channel_id)
    elif action.action_type == "remove":
        return notify_manual_assignment_removed(notifier, action, channel_id)
    else:
        logger.warning(f"Unknown action type: {action.action_type}")
        return SyncNotificationResult(
            success=False,
            message=f"Unknown action type: {action.action_type}",
            error="Invalid action type",
        )
