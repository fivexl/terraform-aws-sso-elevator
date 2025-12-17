"""Property-based tests for sync notifications.

Tests the correctness of Slack notification functions for attribute sync operations.
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from sync_notifications import (
    SyncSummary,
    format_attributes,
    notify_manual_assignment_detected,
    notify_manual_assignment_removed,
    notify_sync_error,
    notify_sync_summary,
    notify_user_added_to_group,
    send_notification_for_action,
)
from sync_state import SyncAction


# Strategies for generating test data
attribute_name_strategy = st.sampled_from(["department", "employeeType", "costCenter", "jobTitle", "location", "team"])

attribute_value_strategy = st.sampled_from(
    [
        "Engineering",
        "Sales",
        "HR",
        "Finance",
        "Marketing",
        "Operations",
        "FullTime",
        "PartTime",
        "Contractor",
        "Intern",
        "CC001",
        "CC002",
        "Manager",
        "Engineer",
    ]
)

user_id_strategy = st.uuids().map(str)
group_id_strategy = st.uuids().map(str)
email_strategy = st.emails()

group_name_strategy = st.sampled_from(
    [
        "Engineering",
        "Sales",
        "HR",
        "Finance",
        "Marketing",
        "Operations",
        "Admins",
        "Developers",
    ]
)


@st.composite
def attributes_strategy(draw: st.DrawFn) -> dict[str, str]:
    """Generate a dictionary of attributes."""
    num_attrs = draw(st.integers(min_value=1, max_value=4))
    attr_names = draw(st.permutations(["department", "employeeType", "costCenter", "jobTitle"]))
    selected_attrs = attr_names[:num_attrs]
    return {name: draw(attribute_value_strategy) for name in selected_attrs}


@st.composite
def add_action_strategy(draw: st.DrawFn) -> SyncAction:
    """Generate a SyncAction for an add operation."""
    return SyncAction(
        action_type="add",
        user_id=draw(user_id_strategy),
        user_email=draw(email_strategy),
        group_id=draw(group_id_strategy),
        group_name=draw(group_name_strategy),
        reason=f"User matches attribute rules for group '{draw(group_name_strategy)}'",
        matched_attributes=draw(attributes_strategy()),
    )


@st.composite
def warn_action_strategy(draw: st.DrawFn) -> SyncAction:
    """Generate a SyncAction for a warn operation (manual assignment detected)."""
    return SyncAction(
        action_type="warn",
        user_id=draw(user_id_strategy),
        user_email=draw(email_strategy),
        group_id=draw(group_id_strategy),
        group_name=draw(group_name_strategy),
        reason=f"User does not match attribute rules for group '{draw(group_name_strategy)}' (manual assignment detected)",
        matched_attributes=draw(attributes_strategy()),
    )


@st.composite
def remove_action_strategy(draw: st.DrawFn) -> SyncAction:
    """Generate a SyncAction for a remove operation."""
    return SyncAction(
        action_type="remove",
        user_id=draw(user_id_strategy),
        user_email=draw(email_strategy),
        group_id=draw(group_id_strategy),
        group_name=draw(group_name_strategy),
        reason=f"User does not match attribute rules for group '{draw(group_name_strategy)}' and policy is 'remove'",
        matched_attributes=draw(attributes_strategy()),
    )


@st.composite
def sync_summary_strategy(draw: st.DrawFn) -> SyncSummary:
    """Generate a SyncSummary."""
    return SyncSummary(
        users_evaluated=draw(st.integers(min_value=0, max_value=1000)),
        groups_processed=draw(st.integers(min_value=0, max_value=100)),
        users_added=draw(st.integers(min_value=0, max_value=100)),
        users_removed=draw(st.integers(min_value=0, max_value=100)),
        manual_assignments_detected=draw(st.integers(min_value=0, max_value=50)),
        manual_assignments_removed=draw(st.integers(min_value=0, max_value=50)),
        errors=draw(st.lists(st.text(min_size=1, max_size=50), max_size=10)),
    )


class TestFormatAttributes:
    """Tests for the format_attributes helper function."""

    def test_format_attributes_with_none(self):
        """format_attributes should return 'N/A' for None input."""
        assert format_attributes(None) == "N/A"

    def test_format_attributes_with_empty_dict(self):
        """format_attributes should return 'N/A' for empty dict."""
        assert format_attributes({}) == "N/A"

    @settings(max_examples=100)
    @given(attrs=attributes_strategy())
    def test_format_attributes_includes_all_keys(self, attrs: dict[str, str]):
        """
        For any non-empty attributes dictionary, format_attributes should
        include all keys in the output.
        """
        result = format_attributes(attrs)
        for key in attrs:
            assert key in result

    @settings(max_examples=100)
    @given(attrs=attributes_strategy())
    def test_format_attributes_includes_all_values(self, attrs: dict[str, str]):
        """
        For any non-empty attributes dictionary, format_attributes should
        include all values in the output.
        """
        result = format_attributes(attrs)
        for value in attrs.values():
            assert value in result


class TestUserAdditionNotification:
    """
    **Feature: attribute-based-group-sync, Property 14: User addition notification**
    **Validates: Requirements 6.2**

    For any user added to a group via sync, a Slack notification should be sent
    with user and group details.
    """

    @settings(max_examples=100)
    @given(action=add_action_strategy())
    def test_user_addition_notification_calls_slack_api(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 14: User addition notification**
        **Validates: Requirements 6.2**

        For any add action, notify_user_added_to_group should call the Slack API
        with a message containing user and group details.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_user_added_to_group(mock_client, action)

            assert result.success is True
            mock_client.chat_postMessage.assert_called_once()

            # Verify the message contains required information
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert action.user_email in message_text
            assert action.group_name in message_text

    @settings(max_examples=100)
    @given(action=add_action_strategy())
    def test_user_addition_notification_includes_matched_attributes(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 14: User addition notification**
        **Validates: Requirements 6.2**

        For any add action with matched attributes, the notification should
        include the matched attribute information.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_user_added_to_group(mock_client, action)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            # Verify matched attributes are mentioned
            if action.matched_attributes:
                for attr_name in action.matched_attributes:
                    assert attr_name in message_text

    @settings(max_examples=100)
    @given(action=add_action_strategy())
    def test_user_addition_notification_handles_api_error(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 14: User addition notification**
        **Validates: Requirements 6.2**

        For any add action, if the Slack API fails, the function should return
        a failure result without raising an exception.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = Exception("API Error")

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_user_added_to_group(mock_client, action)

            assert result.success is False
            assert result.error is not None

    @settings(max_examples=100)
    @given(action=add_action_strategy(), channel_id=st.text(min_size=5, max_size=20, alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"))
    def test_user_addition_notification_uses_custom_channel(self, action: SyncAction, channel_id: str):
        """
        **Feature: attribute-based-group-sync, Property 14: User addition notification**
        **Validates: Requirements 6.2**

        For any add action with a custom channel ID, the notification should
        be sent to that channel.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "DEFAULT_CHANNEL"

            result = notify_user_added_to_group(mock_client, action, channel_id=channel_id)

            assert result.success is True
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert call_kwargs["channel"] == channel_id


class TestManualAssignmentNotification:
    """
    **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
    **Validates: Requirements 3.3, 3.4, 4.5**

    For any manually-added user who doesn't match rules, the system should log
    a warning and send a Slack notification with user and group details.
    """

    @settings(max_examples=100)
    @given(action=warn_action_strategy())
    def test_manual_assignment_detected_notification_calls_slack_api(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
        **Validates: Requirements 3.3, 3.4, 4.5**

        For any warn action (manual assignment detected), notify_manual_assignment_detected
        should call the Slack API with a message containing user and group details.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_manual_assignment_detected(mock_client, action)

            assert result.success is True
            mock_client.chat_postMessage.assert_called_once()

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert action.user_email in message_text
            assert action.group_name in message_text
            assert "Manual Assignment Detected" in message_text

    @settings(max_examples=100)
    @given(action=warn_action_strategy())
    def test_manual_assignment_detected_notification_includes_warning_indicator(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
        **Validates: Requirements 3.3, 3.4, 4.5**

        For any warn action, the notification should include a warning indicator
        to alert administrators.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_manual_assignment_detected(mock_client, action)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            # Should contain warning emoji or text
            assert ":warning:" in message_text or "Warning" in message_text

    @settings(max_examples=100)
    @given(action=remove_action_strategy())
    def test_manual_assignment_removed_notification_calls_slack_api(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
        **Validates: Requirements 3.3, 3.4, 4.5**

        For any remove action (manual assignment removed), notify_manual_assignment_removed
        should call the Slack API with a message containing user and group details.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_manual_assignment_removed(mock_client, action)

            assert result.success is True
            mock_client.chat_postMessage.assert_called_once()

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert action.user_email in message_text
            assert action.group_name in message_text
            assert "Removed" in message_text

    @settings(max_examples=100)
    @given(action=warn_action_strategy())
    def test_manual_assignment_notification_handles_api_error(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 11: Manual assignment notification**
        **Validates: Requirements 3.3, 3.4, 4.5**

        For any warn action, if the Slack API fails, the function should return
        a failure result without raising an exception.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = Exception("API Error")

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_manual_assignment_detected(mock_client, action)

            assert result.success is False
            assert result.error is not None


class TestSyncErrorNotification:
    """Tests for sync error notifications."""

    @settings(max_examples=100)
    @given(
        error_message=st.text(min_size=1, max_size=200),
        error_count=st.integers(min_value=1, max_value=100),
    )
    def test_sync_error_notification_calls_slack_api(self, error_message: str, error_count: int):
        """
        For any error message and count, notify_sync_error should call the Slack API
        with a message containing the error details.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_sync_error(mock_client, error_message, error_count)

            assert result.success is True
            mock_client.chat_postMessage.assert_called_once()

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert str(error_count) in message_text
            assert "Error" in message_text


class TestSyncSummaryNotification:
    """Tests for sync summary notifications."""

    @settings(max_examples=100)
    @given(summary=sync_summary_strategy())
    def test_sync_summary_notification_calls_slack_api(self, summary: SyncSummary):
        """
        For any sync summary, notify_sync_summary should call the Slack API
        with a message containing the summary statistics.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_sync_summary(mock_client, summary)

            assert result.success is True
            mock_client.chat_postMessage.assert_called_once()

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            # Verify key statistics are in the message
            assert str(summary.users_evaluated) in message_text
            assert str(summary.groups_processed) in message_text
            assert str(summary.users_added) in message_text

    @settings(max_examples=100)
    @given(summary=sync_summary_strategy())
    def test_sync_summary_shows_error_indicator_when_errors_present(self, summary: SyncSummary):
        """
        For any sync summary with errors, the notification should include
        a warning indicator.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = notify_sync_summary(mock_client, summary)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            if summary.errors:
                assert ":warning:" in message_text or "Error" in message_text
            else:
                assert ":white_check_mark:" in message_text or "Successfully" in message_text


class TestSendNotificationForAction:
    """Tests for the send_notification_for_action convenience function."""

    @settings(max_examples=100)
    @given(action=add_action_strategy())
    def test_send_notification_routes_add_action_correctly(self, action: SyncAction):
        """
        For any add action, send_notification_for_action should route to
        notify_user_added_to_group.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = send_notification_for_action(mock_client, action)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert "User Added to Group" in message_text

    @settings(max_examples=100)
    @given(action=warn_action_strategy())
    def test_send_notification_routes_warn_action_correctly(self, action: SyncAction):
        """
        For any warn action, send_notification_for_action should route to
        notify_manual_assignment_detected.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = send_notification_for_action(mock_client, action)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert "Manual Assignment Detected" in message_text

    @settings(max_examples=100)
    @given(action=remove_action_strategy())
    def test_send_notification_routes_remove_action_correctly(self, action: SyncAction):
        """
        For any remove action, send_notification_for_action should route to
        notify_manual_assignment_removed.
        """
        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch("sync_notifications.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_get_config.return_value = mock_cfg
            mock_cfg.slack_channel_id = "C12345"

            result = send_notification_for_action(mock_client, action)

            assert result.success is True

            call_kwargs = mock_client.chat_postMessage.call_args[1]
            message_text = call_kwargs["text"]

            assert "Manual Assignment Removed" in message_text
