"""Property-based tests for attribute sync audit logging.

These tests verify the correctness properties for audit entry creation,
completeness, and storage consistency for attribute-based group sync operations.
"""

import json
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from s3 import SyncAuditParams, create_sync_audit_entry, log_operation


# Strategies for generating test data
sync_operation_types = st.sampled_from(["sync_add", "sync_remove", "manual_detected"])

user_principal_id = st.text(min_size=10, max_size=50, alphabet="abcdef0123456789-")

email_strategy = st.emails()

group_id = st.text(min_size=10, max_size=50, alphabet="abcdef0123456789-")

group_name = st.text(
    min_size=1,
    max_size=100,
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ",
)

reason_text = st.text(min_size=1, max_size=500)

# Strategy for matched_attributes - dictionary of attribute name to value
attribute_name = st.sampled_from(["department", "employeeType", "costCenter", "jobTitle", "location"])
attribute_value = st.text(min_size=1, max_size=100)
matched_attributes_strategy = st.one_of(
    st.none(),
    st.dictionaries(attribute_name, attribute_value, min_size=1, max_size=5),
)


# Composite strategy for SyncAuditParams
@st.composite
def sync_audit_params_strategy(draw: st.DrawFn) -> SyncAuditParams:  # noqa: ANN201
    """Generate random SyncAuditParams for testing."""
    return SyncAuditParams(
        operation_type=draw(sync_operation_types),
        sso_user_principal_id=draw(user_principal_id),
        sso_user_email=draw(email_strategy),
        group_id=draw(group_id),
        group_name=draw(group_name),
        reason=draw(reason_text),
        matched_attributes=draw(matched_attributes_strategy),
    )


class TestAuditEntryCreation:
    """Property 13: Audit entry creation for all operations.

    *For any* sync operation (add, remove, or manual detection), an audit entry
    with the correct operation type ("sync_add", "sync_remove", or "manual_detected")
    should be written to S3.

    **Feature: attribute-based-group-sync, Property 13: Audit entry creation for all operations**
    **Validates: Requirements 4.4, 6.1, 6.3, 6.4**
    """

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    def test_sync_audit_entry_has_correct_operation_type(self, params: SyncAuditParams):
        """For any sync operation, the audit entry should have the correct operation type."""
        audit_entry = create_sync_audit_entry(params)

        # Property: operation_type in audit entry matches the input operation type
        assert audit_entry.operation_type == params.operation_type
        # Property: audit_entry_type matches operation_type for sync operations
        assert audit_entry.audit_entry_type == params.operation_type

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    def test_sync_audit_entry_marked_as_attribute_sync(self, params: SyncAuditParams):
        """For any sync operation, the audit entry should be marked as attribute_sync."""
        audit_entry = create_sync_audit_entry(params)

        # Property: sync_operation is always "attribute_sync" for sync entries
        assert audit_entry.sync_operation == "attribute_sync"

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_log_operation_writes_to_s3_with_correct_type(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any sync operation, log_operation should write to S3 with correct operation type."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        # Verify S3 put_object was called
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]

        # Parse the JSON body to verify operation_type
        body_json = json.loads(call_kwargs["Body"])
        assert body_json["operation_type"] == params.operation_type
        assert body_json["audit_entry_type"] == params.operation_type


class TestAuditEntryCompleteness:
    """Property 15: Audit entry completeness.

    *For any* audit entry, it should include all required fields: user ID, user email,
    group ID, matched attribute values, timestamp, and operation type.

    **Feature: attribute-based-group-sync, Property 15: Audit entry completeness**
    **Validates: Requirements 6.5**
    """

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    def test_sync_audit_entry_contains_all_required_fields(self, params: SyncAuditParams):
        """For any sync audit entry, all required fields should be present."""
        audit_entry = create_sync_audit_entry(params)

        # Property: All required fields are present and match input
        assert audit_entry.sso_user_principal_id == params.sso_user_principal_id  # user ID
        assert audit_entry.sso_user_email == params.sso_user_email  # user email
        assert audit_entry.group_id == params.group_id  # group ID
        assert audit_entry.matched_attributes == params.matched_attributes  # matched attributes
        assert audit_entry.operation_type == params.operation_type  # operation type
        assert audit_entry.reason == params.reason  # reason

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_logged_audit_entry_contains_timestamp(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any logged audit entry, timestamp should be present."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]
        body_json = json.loads(call_kwargs["Body"])

        # Property: timestamp fields are present
        assert "time" in body_json
        assert "timestamp" in body_json
        assert isinstance(body_json["timestamp"], int)

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_logged_audit_entry_contains_all_fields_in_json(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any logged audit entry, all required fields should be in the JSON."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]
        body_json = json.loads(call_kwargs["Body"])

        # Property: All required fields are present in JSON
        assert body_json["sso_user_principal_id"] == params.sso_user_principal_id
        assert body_json["sso_user_email"] == params.sso_user_email
        assert body_json["group_id"] == params.group_id
        assert body_json["group_name"] == params.group_name
        assert body_json["operation_type"] == params.operation_type
        assert body_json["reason"] == params.reason
        # matched_attributes is converted to "NA" if None
        expected_attrs = params.matched_attributes if params.matched_attributes is not None else "NA"
        assert body_json["matched_attributes"] == expected_attrs


class TestAuditEntryStorageConsistency:
    """Property 16: Audit entry storage consistency.

    *For any* audit entry, it should be written to the same S3 bucket and use
    the same partitioning scheme as existing SSO Elevator audit logs.

    **Feature: attribute-based-group-sync, Property 16: Audit entry storage consistency**
    **Validates: Requirements 6.6**
    """

    @given(
        params=sync_audit_params_strategy(),
        bucket_name=st.text(min_size=3, max_size=63, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-"),
        bucket_prefix=st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-/"),
    )
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_audit_entry_uses_configured_bucket(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
        bucket_name: str,
        bucket_prefix: str,
    ):
        """For any audit entry, it should be written to the configured S3 bucket."""
        mock_cfg.s3_bucket_for_audit_entry_name = bucket_name
        mock_cfg.s3_bucket_prefix_for_partitions = bucket_prefix.rstrip("/")
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]

        # Property: Uses the configured bucket
        assert call_kwargs["Bucket"] == bucket_name

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_audit_entry_uses_date_partitioning(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any audit entry, it should use YYYY/MM/DD date partitioning."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]
        key = call_kwargs["Key"]

        # Property: Key follows the pattern prefix/YYYY/MM/DD/uuid.json
        parts = key.split("/")
        assert len(parts) >= 5  # prefix, year, month, day, filename
        assert parts[-1].endswith(".json")
        # Verify date parts are valid
        year, month, day = parts[-4], parts[-3], parts[-2]
        assert len(year) == 4 and year.isdigit()
        assert len(month) == 2 and month.isdigit()
        assert len(day) == 2 and day.isdigit()

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_audit_entry_uses_json_content_type(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any audit entry, it should use application/json content type."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]

        # Property: Uses JSON content type
        assert call_kwargs["ContentType"] == "application/json"

    @given(params=sync_audit_params_strategy())
    @settings(max_examples=100)
    @patch("s3.s3")
    @patch("s3.cfg")
    def test_audit_entry_uses_server_side_encryption(
        self,
        mock_cfg: MagicMock,
        mock_s3: MagicMock,
        params: SyncAuditParams,
    ):
        """For any audit entry, it should use server-side encryption."""
        mock_cfg.s3_bucket_for_audit_entry_name = "test-bucket"
        mock_cfg.s3_bucket_prefix_for_partitions = "audit"
        mock_s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        audit_entry = create_sync_audit_entry(params)

        log_operation(audit_entry)

        call_kwargs = mock_s3.put_object.call_args[1]

        # Property: Uses AES256 server-side encryption (same as existing audit logs)
        assert call_kwargs["ServerSideEncryption"] == "AES256"
