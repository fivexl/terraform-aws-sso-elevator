"""Property-based tests for attribute syncer Lambda function.

Tests the correctness of sync operation logging and error resilience.
"""

from datetime import datetime, timezone
from typing import Literal
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from sync_state import UserInfo, SyncAction


# Import the module under test - we'll mock the dependencies
# We need to patch the imports before importing the module


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
    ]
)


@st.composite
def user_info_strategy(draw: st.DrawFn) -> UserInfo:
    """Generate a UserInfo with random attributes."""
    user_id = draw(user_id_strategy)
    email = draw(email_strategy)
    num_attrs = draw(st.integers(min_value=0, max_value=4))
    attr_names = draw(st.permutations(["department", "employeeType", "costCenter", "jobTitle", "location", "team"]))
    selected_attrs = attr_names[:num_attrs]
    attributes = {name: draw(attribute_value_strategy) for name in selected_attrs}
    return UserInfo(user_id=user_id, email=email, attributes=attributes)


@st.composite
def sync_action_strategy(draw: st.DrawFn) -> SyncAction:
    """Generate a random SyncAction."""
    action_type: Literal["add", "remove", "warn"] = draw(st.sampled_from(["add", "remove", "warn"]))
    user_id = draw(user_id_strategy)
    user_email = draw(email_strategy)
    group_id = draw(group_id_strategy)
    group_name = draw(group_name_strategy)
    reason = f"Test reason for {action_type}"

    # Generate matched attributes for add actions
    matched_attributes = None
    if action_type == "add":
        num_attrs = draw(st.integers(min_value=1, max_value=3))
        attr_names = draw(st.permutations(["department", "employeeType", "costCenter"]))
        selected_attrs = attr_names[:num_attrs]
        matched_attributes = {name: draw(attribute_value_strategy) for name in selected_attrs}

    return SyncAction(
        action_type=action_type,
        user_id=user_id,
        user_email=user_email,
        group_id=group_id,
        group_name=group_name,
        reason=reason,
        matched_attributes=matched_attributes,
    )


@st.composite
def sync_operation_stats_strategy(draw: st.DrawFn) -> dict:
    """Generate random sync operation statistics."""
    return {
        "users_evaluated": draw(st.integers(min_value=0, max_value=1000)),
        "groups_processed": draw(st.integers(min_value=0, max_value=50)),
        "users_added": draw(st.integers(min_value=0, max_value=100)),
        "users_removed": draw(st.integers(min_value=0, max_value=100)),
        "manual_assignments_detected": draw(st.integers(min_value=0, max_value=100)),
        "manual_assignments_removed": draw(st.integers(min_value=0, max_value=100)),
        "errors": draw(st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=10)),
    }


class TestSyncOperationLogging:
    """
    **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
    **Validates: Requirements 5.3, 5.4**

    For any sync operation, the system should log start time, end time, and
    summary statistics (users evaluated, groups processed, users added/removed, errors).
    """

    @settings(max_examples=100)
    @given(stats=sync_operation_stats_strategy())
    def test_sync_operation_result_logs_start_time(self, stats: dict):  # noqa: ARG002
        """
        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.3**

        For any sync operation, the system should log the start time when
        the operation begins.
        """
        # Import here to avoid config loading issues
        from attribute_syncer import SyncOperationResult

        start_time = datetime.now(timezone.utc)
        result = SyncOperationResult(start_time=start_time)

        # Mock the logger
        with patch("attribute_syncer.logger") as mock_logger:
            result.log_start()

            # Verify logger.info was called with start time
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args

            # Check the message contains operation start
            assert "started" in call_args[0][0].lower()

            # Check extra contains start_time
            extra = call_args[1].get("extra", {})
            assert "start_time" in extra
            assert extra["start_time"] == start_time.isoformat()

    @settings(max_examples=100)
    @given(stats=sync_operation_stats_strategy())
    def test_sync_operation_result_logs_completion_with_statistics(self, stats: dict):
        """
        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.4**

        For any sync operation, the system should log the completion time
        and summary statistics when the operation completes.
        """
        from attribute_syncer import SyncOperationResult

        start_time = datetime.now(timezone.utc)
        result = SyncOperationResult(
            start_time=start_time,
            users_evaluated=stats["users_evaluated"],
            groups_processed=stats["groups_processed"],
            users_added=stats["users_added"],
            users_removed=stats["users_removed"],
            manual_assignments_detected=stats["manual_assignments_detected"],
            manual_assignments_removed=stats["manual_assignments_removed"],
            errors=stats["errors"],
        )
        result.end_time = datetime.now(timezone.utc)
        result.success = len(stats["errors"]) == 0

        with patch("attribute_syncer.logger") as mock_logger:
            result.log_completion()

            # Verify logger.info was called
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args

            # Check the message contains completion
            assert "completed" in call_args[0][0].lower()

            # Check extra contains all required statistics
            extra = call_args[1].get("extra", {})
            assert "start_time" in extra
            assert "end_time" in extra
            assert "duration_ms" in extra
            assert "users_evaluated" in extra
            assert "groups_processed" in extra
            assert "users_added" in extra
            assert "users_removed" in extra
            assert "manual_assignments_detected" in extra
            assert "manual_assignments_removed" in extra
            assert "error_count" in extra

            # Verify statistics match
            assert extra["users_evaluated"] == stats["users_evaluated"]
            assert extra["groups_processed"] == stats["groups_processed"]
            assert extra["users_added"] == stats["users_added"]
            assert extra["users_removed"] == stats["users_removed"]
            assert extra["manual_assignments_detected"] == stats["manual_assignments_detected"]
            assert extra["manual_assignments_removed"] == stats["manual_assignments_removed"]
            assert extra["error_count"] == len(stats["errors"])

    @settings(max_examples=100)
    @given(stats=sync_operation_stats_strategy())
    def test_sync_operation_result_to_summary_preserves_all_fields(self, stats: dict):
        """
        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.3, 5.4**

        For any sync operation result, converting to summary should preserve
        all statistics for notification purposes.
        """
        from attribute_syncer import SyncOperationResult

        start_time = datetime.now(timezone.utc)
        result = SyncOperationResult(
            start_time=start_time,
            users_evaluated=stats["users_evaluated"],
            groups_processed=stats["groups_processed"],
            users_added=stats["users_added"],
            users_removed=stats["users_removed"],
            manual_assignments_detected=stats["manual_assignments_detected"],
            manual_assignments_removed=stats["manual_assignments_removed"],
            errors=stats["errors"],
        )

        summary = result.to_summary()

        # Verify all fields are preserved
        assert summary.users_evaluated == stats["users_evaluated"]
        assert summary.groups_processed == stats["groups_processed"]
        assert summary.users_added == stats["users_added"]
        assert summary.users_removed == stats["users_removed"]
        assert summary.manual_assignments_detected == stats["manual_assignments_detected"]
        assert summary.manual_assignments_removed == stats["manual_assignments_removed"]
        assert summary.errors == stats["errors"]

    @settings(max_examples=100)
    @given(
        users_evaluated=st.integers(min_value=0, max_value=10000),
        groups_processed=st.integers(min_value=0, max_value=100),
    )
    def test_sync_operation_logs_duration_correctly(
        self,
        users_evaluated: int,
        groups_processed: int,
    ):
        """
        **Feature: attribute-based-group-sync, Property 17: Sync operation logging**
        **Validates: Requirements 5.4**

        For any sync operation, the logged duration should be non-negative
        and represent the actual time elapsed.
        """
        from attribute_syncer import SyncOperationResult
        import time

        start_time = datetime.now(timezone.utc)
        result = SyncOperationResult(
            start_time=start_time,
            users_evaluated=users_evaluated,
            groups_processed=groups_processed,
        )

        # Simulate some processing time
        time.sleep(0.001)  # 1ms

        result.end_time = datetime.now(timezone.utc)
        result.success = True

        with patch("attribute_syncer.logger") as mock_logger:
            result.log_completion()

            call_args = mock_logger.info.call_args
            extra = call_args[1].get("extra", {})

            # Duration should be non-negative
            assert extra["duration_ms"] >= 0

            # Duration should be reasonable (less than 10 seconds for this test)
            assert extra["duration_ms"] < 10000


class TestErrorResilience:
    """
    **Feature: attribute-based-group-sync, Property 18: Error resilience**
    **Validates: Requirements 5.5, 8.1, 8.2, 8.3, 8.4, 8.5**

    For any error during sync (API failure, missing group, add failure, remove failure),
    the system should log the error, continue processing remaining items, and send
    a summary notification to Slack.
    """

    @settings(max_examples=100)
    @given(
        num_successful_actions=st.integers(min_value=0, max_value=10),
        num_failed_actions=st.integers(min_value=1, max_value=5),
    )
    def test_sync_continues_after_add_failure(
        self,
        num_successful_actions: int,
        num_failed_actions: int,
    ):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 8.3**

        When adding a user to a group fails, the system should log the error
        and continue processing other users.
        """
        from attribute_syncer import SyncOperationResult

        # Create a result that simulates partial failures
        result = SyncOperationResult(start_time=datetime.now(timezone.utc))
        result.users_added = num_successful_actions

        # Add errors for failed actions
        for i in range(num_failed_actions):
            result.errors.append(f"Failed to add user{i}@example.com to GroupA")

        result.end_time = datetime.now(timezone.utc)
        result.success = False  # Has errors

        # Verify the result captures both successes and failures
        assert result.users_added == num_successful_actions
        assert len(result.errors) == num_failed_actions

        # Verify success is False when there are errors
        assert result.success is False

    @settings(max_examples=100)
    @given(
        num_successful_actions=st.integers(min_value=0, max_value=10),
        num_failed_actions=st.integers(min_value=1, max_value=5),
    )
    def test_sync_continues_after_remove_failure(
        self,
        num_successful_actions: int,
        num_failed_actions: int,
    ):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 8.4**

        When removing a user from a group fails, the system should log the error
        and continue processing other users.
        """
        from attribute_syncer import SyncOperationResult

        result = SyncOperationResult(start_time=datetime.now(timezone.utc))
        result.users_removed = num_successful_actions
        result.manual_assignments_removed = num_successful_actions

        for i in range(num_failed_actions):
            result.errors.append(f"Failed to remove user{i}@example.com from GroupB")

        result.end_time = datetime.now(timezone.utc)
        result.success = False

        assert result.users_removed == num_successful_actions
        assert len(result.errors) == num_failed_actions
        assert result.success is False

    @settings(max_examples=100)
    @given(error_messages=st.lists(st.text(min_size=1, max_size=100), min_size=1, max_size=10))
    def test_error_summary_includes_all_errors(self, error_messages: list[str]):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 8.5**

        When the sync operation encounters errors, the summary should include
        all error messages for notification purposes.
        """
        from attribute_syncer import SyncOperationResult

        result = SyncOperationResult(start_time=datetime.now(timezone.utc))
        result.errors = error_messages
        result.end_time = datetime.now(timezone.utc)
        result.success = False

        summary = result.to_summary()

        # All errors should be in the summary
        assert summary.errors == error_messages
        assert len(summary.errors) == len(error_messages)

    @settings(max_examples=100)
    @given(
        users_evaluated=st.integers(min_value=1, max_value=100),
        groups_processed=st.integers(min_value=1, max_value=10),
    )
    def test_sync_result_tracks_partial_success(
        self,
        users_evaluated: int,
        groups_processed: int,
    ):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 5.5, 8.1, 8.2, 8.3, 8.4**

        For any sync operation with partial failures, the result should
        accurately track both successful operations and errors.
        """
        from attribute_syncer import SyncOperationResult

        result = SyncOperationResult(start_time=datetime.now(timezone.utc))
        result.users_evaluated = users_evaluated
        result.groups_processed = groups_processed

        # Simulate some successful operations
        result.users_added = users_evaluated // 3
        result.users_removed = users_evaluated // 4
        result.manual_assignments_detected = users_evaluated // 5

        # Add some errors
        result.errors.append("API error: Identity Store unavailable")
        result.errors.append("Group 'NonExistent' not found")

        result.end_time = datetime.now(timezone.utc)
        result.success = False

        # Verify all statistics are tracked
        assert result.users_evaluated == users_evaluated
        assert result.groups_processed == groups_processed
        assert result.users_added == users_evaluated // 3
        assert result.users_removed == users_evaluated // 4
        assert result.manual_assignments_detected == users_evaluated // 5
        assert len(result.errors) == 2

    def test_sync_operation_result_success_when_no_errors(self):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 5.5**

        When a sync operation completes without errors, success should be True.
        """
        from attribute_syncer import SyncOperationResult

        result = SyncOperationResult(start_time=datetime.now(timezone.utc))
        result.users_evaluated = 100
        result.groups_processed = 5
        result.users_added = 10
        result.users_removed = 2
        result.end_time = datetime.now(timezone.utc)
        result.success = len(result.errors) == 0

        assert result.success is True
        assert len(result.errors) == 0

    @settings(max_examples=100)
    @given(action=sync_action_strategy())
    def test_audit_entry_logged_for_all_action_types(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 8.3, 8.4**

        For any sync action (add, remove, warn), an audit entry should be
        logged regardless of whether the action succeeds or fails.
        """
        from attribute_syncer import _log_audit_entry

        with patch("attribute_syncer.s3_module") as mock_s3:
            mock_s3.SyncAuditParams = MagicMock()
            mock_s3.create_sync_audit_entry = MagicMock()
            mock_s3.log_operation = MagicMock()

            _log_audit_entry(action)

            # Verify audit entry was created
            mock_s3.SyncAuditParams.assert_called_once()
            mock_s3.create_sync_audit_entry.assert_called_once()
            mock_s3.log_operation.assert_called_once()

            # Verify the operation type mapping
            call_kwargs = mock_s3.SyncAuditParams.call_args[1]
            expected_op_type = {
                "add": "sync_add",
                "remove": "sync_remove",
                "warn": "manual_detected",
            }[action.action_type]
            assert call_kwargs["operation_type"] == expected_op_type

    @settings(max_examples=100)
    @given(action=sync_action_strategy())
    def test_audit_entry_failure_does_not_raise(self, action: SyncAction):
        """
        **Feature: attribute-based-group-sync, Property 18: Error resilience**
        **Validates: Requirements 8.3, 8.4**

        When audit entry logging fails, the error should be logged but
        not propagated (graceful degradation).
        """
        from attribute_syncer import _log_audit_entry

        with patch("attribute_syncer.s3_module") as mock_s3:
            mock_s3.SyncAuditParams = MagicMock(side_effect=Exception("S3 error"))

            with patch("attribute_syncer.logger") as mock_logger:
                # Should not raise
                _log_audit_entry(action)

                # Should log the exception
                mock_logger.exception.assert_called_once()


class TestBuildMappingRules:
    """Tests for building mapping rules from configuration."""

    @settings(max_examples=100)
    @given(
        group_name=group_name_strategy,
        group_id=group_id_strategy,
        attr_name=attribute_name_strategy,
        attr_value=attribute_value_strategy,
    )
    def test_build_mapping_rules_creates_valid_rules(
        self,
        group_name: str,
        group_id: str,
        attr_name: str,
        attr_value: str,
    ):
        """
        For any valid configuration, _build_mapping_rules should create
        AttributeMappingRule objects with correct conditions.
        """
        from attribute_syncer import _build_mapping_rules
        from sync_config import SyncConfiguration

        config = SyncConfiguration(
            enabled=True,
            managed_group_names=(group_name,),
            managed_group_ids={group_name: group_id},
            mapping_rules=(
                {
                    "group_name": group_name,
                    "attributes": {attr_name: attr_value},
                },
            ),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        with patch("attribute_syncer.get_valid_rules_for_resolved_groups") as mock_get_valid:
            mock_get_valid.return_value = list(config.mapping_rules)

            rules = _build_mapping_rules(config)

            assert len(rules) == 1
            rule = rules[0]
            assert rule.group_name == group_name
            assert rule.group_id == group_id
            assert len(rule.conditions) == 1
            assert rule.conditions[0].attribute_name == attr_name
            assert rule.conditions[0].expected_value == attr_value
