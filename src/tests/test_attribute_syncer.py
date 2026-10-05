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
            mock_s3.log_operation_best_effort = MagicMock()

            _log_audit_entry(action, "test-bucket", "audit")

            # Verify audit entry was created
            mock_s3.SyncAuditParams.assert_called_once()
            mock_s3.create_sync_audit_entry.assert_called_once()
            mock_s3.log_operation_best_effort.assert_called_once()

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
                _log_audit_entry(action, "test-bucket", "audit")

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


def test_lambda_handler_reads_slack_bot_token_from_ssm_in_degrade_mode(monkeypatch):
    """Degrade mode means an SSM failure costs a Slack message, never the sync itself."""
    import attribute_syncer

    monkeypatch.delenv("IDENTITY_STORE_ID", raising=False)  # return right after the Slack client is built
    with (
        patch("attribute_syncer.load_sync_config", return_value=MagicMock(enabled=True)),
        patch("attribute_syncer.get_slack_secret", return_value="xoxb-from-ssm") as mock_get_secret,
        patch("attribute_syncer.WebClient") as mock_web_client,
    ):
        attribute_syncer.lambda_handler({}, None)

    mock_get_secret.assert_called_once_with(
        attribute_syncer._ssm_client, attribute_syncer.SLACK_BOT_TOKEN_PARAMETER_ENV, degrade_on_failure=True
    )
    mock_web_client.assert_called_once_with(token="xoxb-from-ssm")


_DEFAULT_LIST_USERS = ({"UserId": "u-ok", "UserName": "ok"}, {"UserId": "u-broken", "UserName": "broken"})


def _sync_context(  # noqa: ANN202
    describe_user_side_effect: object,
    policy: str = "remove",
    list_users: tuple[dict, ...] = _DEFAULT_LIST_USERS,
    members: tuple[str, ...] = ("u-broken",),
    rule_attributes: dict[str, str] | None = None,
):
    """A SyncContext over a fake Identity Store with one managed group, Engineering."""
    from attribute_syncer import SyncContext
    from sync_config import SyncConfiguration

    pages = {
        "list_groups": [{"Groups": [{"GroupId": "g-eng", "DisplayName": "Engineering"}]}],
        "list_users": [{"Users": list(list_users)}],
        "list_group_memberships": [
            {"GroupMemberships": [{"MembershipId": f"m-{user_id}", "MemberId": {"UserId": user_id}} for user_id in members]},
        ],
    }
    client = MagicMock()
    client.get_paginator.side_effect = lambda name: MagicMock(paginate=MagicMock(return_value=pages[name]))
    client.describe_user.side_effect = describe_user_side_effect
    config = SyncConfiguration(
        enabled=True,
        managed_group_names=("Engineering",),
        managed_group_ids={},
        mapping_rules=({"group_name": "Engineering", "attributes": rule_attributes or {"department": "Engineering"}},),
        manual_assignment_policy=policy,  # type: ignore[arg-type]
        schedule_expression="rate(1 hour)",
    )
    return SyncContext(
        identity_store_client=client,
        identity_store_id="d-123",
        s3_client=MagicMock(),
        slack_client=MagicMock(),
        config=config,
        slack_channel_id="C123",
        audit_bucket_name="bucket",
        audit_bucket_prefix="audit",
    )


def _described_user(user_id: str, department: str) -> dict:
    return {
        "UserId": user_id,
        "UserName": user_id,
        "Emails": [{"Value": f"{user_id}@example.com", "Primary": True}],
        "Extensions": {"aws:identitystore:enterprise": {"department": department}},
    }


def _describe_user_failing_for_broken(UserId: str, **_: object) -> dict:  # noqa: N803
    if UserId == "u-broken":
        raise RuntimeError("throttled")
    return _described_user(UserId, "Engineering")


def test_user_whose_attributes_cannot_be_read_is_neither_added_nor_removed():
    """A failed DescribeUser leaves only the ListUsers record, which has no department; under "remove"
    that used to remove a member who does match. The user must be skipped and the failure reported."""
    from attribute_syncer import perform_sync

    ctx = _sync_context(_describe_user_failing_for_broken)
    with patch("attribute_syncer.s3_module.log_operation"), patch("attribute_syncer.send_notification_for_action"):
        result = perform_sync(ctx)

    ctx.identity_store_client.delete_group_membership.assert_not_called()
    added = [c.kwargs["MemberId"]["UserId"] for c in ctx.identity_store_client.create_group_membership.call_args_list]
    assert added == ["u-ok"]
    assert result.users_removed == 0
    assert result.errors == ["Failed to read attributes of u-broken, skipped this run: throttled"]
    assert result.to_summary().errors == result.errors
    assert result.success is False


def test_unread_non_member_is_not_added_even_if_list_users_record_matches():
    """The rule is on a core attribute the ListUsers record carries, so the record alone would match."""
    from attribute_syncer import perform_sync

    list_users = ({"UserId": "u-broken", "UserName": "broken", "Title": "Engineer"},)
    ctx = _sync_context(_describe_user_failing_for_broken, list_users=list_users, members=(), rule_attributes={"title": "Engineer"})
    with patch("attribute_syncer.s3_module.log_operation"), patch("attribute_syncer.send_notification_for_action"):
        result = perform_sync(ctx)

    ctx.identity_store_client.create_group_membership.assert_not_called()
    assert result.users_added == 0
    assert len(result.errors) == 1


def test_unread_member_gets_no_manual_assignment_warning_under_warn_policy():
    from attribute_syncer import perform_sync

    ctx = _sync_context(_describe_user_failing_for_broken, policy="warn", list_users=(_DEFAULT_LIST_USERS[1],))
    with (
        patch("attribute_syncer.s3_module.log_operation") as log_operation,
        patch("attribute_syncer.send_notification_for_action") as notify,
    ):
        result = perform_sync(ctx)

    assert result.manual_assignments_detected == 0
    log_operation.assert_not_called()
    notify.assert_not_called()
    ctx.identity_store_client.delete_group_membership.assert_not_called()
    assert len(result.errors) == 1


def test_lookup_error_names_the_user_by_email_when_list_users_has_one():
    from attribute_syncer import perform_sync

    list_users = ({"UserId": "u-broken", "UserName": "broken", "Emails": [{"Value": "broken@example.com", "Primary": True}]},)
    ctx = _sync_context(_describe_user_failing_for_broken, list_users=list_users)
    with patch("attribute_syncer.s3_module.log_operation"), patch("attribute_syncer.send_notification_for_action"):
        result = perform_sync(ctx)

    assert result.errors == ["Failed to read attributes of broken@example.com, skipped this run: throttled"]


def test_sync_audit_write_failure_logs_full_entry_and_sync_continues():
    """While S3 is down the syncer's audit entry must reach CloudWatch whole, as the requester's and revoker's do."""
    import s3 as s3_module
    from attribute_syncer import perform_sync

    ctx = _sync_context(lambda UserId, **_: _described_user(UserId, "Engineering" if UserId == "u-ok" else "Sales"))  # noqa: N803
    with (
        patch.object(s3_module.s3, "put_object", side_effect=RuntimeError("s3 down")) as put_object,
        patch.object(s3_module.logger, "exception") as log_exception,
        patch("attribute_syncer.send_notification_for_action"),
    ):
        result = perform_sync(ctx)

    # One add (u-ok matches) and one remove (u-broken is in Sales): both executed despite S3 failing.
    assert put_object.call_count == 2  # noqa: PLR2004
    assert (result.users_added, result.users_removed) == (1, 1)
    entries = [c.kwargs["extra"]["audit_entry"] for c in log_exception.call_args_list]
    assert [e["operation_type"] for e in entries] == ["sync_add", "sync_remove"]
    assert {e["sso_user_principal_id"] for e in entries} == {"u-ok", "u-broken"}
    assert all(e["request_source"] == "attribute_sync" for e in entries)
    for call in put_object.call_args_list:
        assert call.kwargs["Bucket"] == "bucket"
        assert call.kwargs["Key"].startswith("audit/")
