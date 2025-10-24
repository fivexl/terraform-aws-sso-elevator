"""Unit tests for the cache module.

Tests cover all error handling scenarios including:
- DynamoDB table doesn't exist
- Wrong table name
- Missing IAM permissions (simulated)
- Cache disabled (cache_ttl_minutes = 0)
- Cache hit/miss/expired scenarios
- Write failures
"""

import json
import time
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import cache as cache_module
from entities.aws import Account, PermissionSet


@pytest.fixture
def cache_config_enabled():
    """Cache config with caching enabled."""
    return cache_module.CacheConfig(
        table_name="test-cache-table",
        ttl_minutes=60,
        enabled=True,
    )


@pytest.fixture
def cache_config_disabled():
    """Cache config with caching disabled."""
    return cache_module.CacheConfig(
        table_name="test-cache-table",
        ttl_minutes=0,
        enabled=False,
    )


@pytest.fixture
def mock_dynamodb_client():
    """Mock DynamoDB client."""
    return Mock()


@pytest.fixture
def sample_accounts():
    """Sample accounts for testing."""
    return [
        Account(id="111111111111", name="Test Account 1"),
        Account(id="222222222222", name="Test Account 2"),
    ]


@pytest.fixture
def sample_permission_sets():
    """Sample permission sets for testing."""
    return [
        PermissionSet(
            arn="arn:aws:sso:::permissionSet/ssoins-1111111111111111/ps-1111111111111111",
            name="AdministratorAccess",
            description="Administrator access permission set",
        ),
        PermissionSet(
            arn="arn:aws:sso:::permissionSet/ssoins-1111111111111111/ps-2222222222222222",
            name="ReadOnlyAccess",
            description="Read-only access permission set",
        ),
    ]


class TestCacheConfig:
    """Tests for CacheConfig."""

    def test_cache_config_enabled(self):
        """Test cache config when enabled."""
        config = cache_module.CacheConfig(
            table_name="test-table",
            ttl_minutes=60,
            enabled=True,
        )
        assert config.table_name == "test-table"
        assert config.ttl_minutes == 60
        assert config.enabled is True

    def test_cache_config_disabled(self):
        """Test cache config when disabled."""
        config = cache_module.CacheConfig(
            table_name="test-table",
            ttl_minutes=0,
            enabled=False,
        )
        assert config.table_name == "test-table"
        assert config.ttl_minutes == 0
        assert config.enabled is False


class TestValidateArn:
    """Tests for _validate_arn function."""

    def test_valid_sso_instance_arn(self):
        """Test validation with a valid SSO instance ARN."""
        valid_arn = "arn:aws:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_with_hyphens(self):
        """Test validation with a valid SSO instance ARN containing hyphens."""
        valid_arn = "arn:aws:sso:::instance/ssoins-test-123-abc"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_govcloud(self):
        """Test validation with a valid GovCloud SSO instance ARN."""
        valid_arn = "arn:aws-us-gov:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_china(self):
        """Test validation with a valid China SSO instance ARN."""
        valid_arn = "arn:aws-cn:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_iso(self):
        """Test validation with a valid ISO SSO instance ARN."""
        valid_arn = "arn:aws-iso:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_isob(self):
        """Test validation with a valid ISOB SSO instance ARN."""
        valid_arn = "arn:aws-iso-b:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_valid_sso_instance_arn_future_partition(self):
        """Test validation accepts future AWS partition formats."""
        # Hypothetical future partition like aws-eu-gov or aws-jp
        valid_arn = "arn:aws-future-partition:sso:::instance/ssoins-1111111111111111"
        result = cache_module._validate_arn(valid_arn)
        assert result == valid_arn

    def test_invalid_arn_not_string(self):
        """Test validation fails when ARN is not a string."""
        with pytest.raises(ValueError, match="ARN must be a string"):
            cache_module._validate_arn(123)  # type: ignore[arg-type]

    def test_invalid_arn_wrong_partition_format(self):
        """Test validation fails with partition that doesn't start with 'aws'."""
        invalid_arn = "arn:azure:sso:::instance/ssoins-1111111111111111"
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn(invalid_arn)

    def test_invalid_arn_wrong_service(self):
        """Test validation fails with wrong AWS service."""
        invalid_arn = "arn:aws:s3:::bucket/mybucket"
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn(invalid_arn)

    def test_invalid_arn_wrong_format(self):
        """Test validation fails with malformed ARN."""
        invalid_arn = "not-an-arn"
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn(invalid_arn)

    def test_invalid_arn_with_injection_attempt(self):
        """Test validation prevents injection attempts."""
        injection_arn = "arn:aws:sso:::instance/test' OR '1'='1"
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn(injection_arn)

    def test_invalid_arn_too_long(self):
        """Test validation fails when ARN exceeds maximum length."""
        long_arn = "arn:aws:sso:::instance/" + "a" * 2000
        with pytest.raises(ValueError, match="ARN exceeds maximum length"):
            cache_module._validate_arn(long_arn)

    def test_invalid_arn_empty_string(self):
        """Test validation fails with empty string."""
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn("")

    def test_invalid_arn_with_special_chars(self):
        """Test validation fails with special characters."""
        invalid_arn = "arn:aws:sso:::instance/test$special%chars"
        with pytest.raises(ValueError, match="Invalid SSO instance ARN format"):
            cache_module._validate_arn(invalid_arn)

    def test_invalid_arn_none(self):
        """Test validation fails with None."""
        with pytest.raises(ValueError, match="ARN must be a string"):
            cache_module._validate_arn(None)  # type: ignore[arg-type]


class TestGetCachedAccounts:
    """Tests for get_cached_accounts function."""

    def test_cache_disabled_returns_none(self, mock_dynamodb_client, cache_config_disabled):
        """When cache is disabled, should return None without calling DynamoDB."""
        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_disabled)

        assert result is None
        mock_dynamodb_client.query.assert_not_called()

    def test_cache_miss_no_items(self, mock_dynamodb_client, cache_config_enabled):
        """When no items found in cache, should return None."""
        mock_dynamodb_client.query.return_value = {"Items": []}

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None
        mock_dynamodb_client.query.assert_called_once()

    def test_cache_hit_valid_data(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When cache has valid data, should return accounts."""
        future_ttl = int(time.time()) + 3600  # 1 hour in future
        accounts_data = [acc.dict() for acc in sample_accounts]

        mock_dynamodb_client.query.return_value = {
            "Items": [
                {
                    "cache_key": {"S": "accounts"},
                    "item_id": {"S": "all"},
                    "data": {"S": json.dumps(accounts_data)},
                    "ttl": {"N": str(future_ttl)},
                }
            ]
        }

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is not None
        assert len(result) == 2
        assert result[0].id == "111111111111"
        assert result[1].id == "222222222222"

    def test_cache_expired_data(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When cache data is expired, should return None."""
        past_ttl = int(time.time()) - 3600  # 1 hour in past
        accounts_data = [acc.dict() for acc in sample_accounts]

        mock_dynamodb_client.query.return_value = {
            "Items": [
                {
                    "cache_key": {"S": "accounts"},
                    "item_id": {"S": "all"},
                    "data": {"S": json.dumps(accounts_data)},
                    "ttl": {"N": str(past_ttl)},
                }
            ]
        }

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None

    def test_table_does_not_exist(self, mock_dynamodb_client, cache_config_enabled):
        """When DynamoDB table doesn't exist, should return None and log warning."""
        mock_dynamodb_client.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}, "Query"
        )

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None

    def test_wrong_table_name(self, mock_dynamodb_client, cache_config_enabled):
        """When table name is wrong, should return None and log warning."""
        mock_dynamodb_client.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}, "Query"
        )

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None

    def test_access_denied(self, mock_dynamodb_client, cache_config_enabled):
        """When access is denied (missing IAM permissions), should return None."""
        mock_dynamodb_client.query.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}}, "Query"
        )

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None

    def test_generic_exception(self, mock_dynamodb_client, cache_config_enabled):
        """When any exception occurs, should return None and not crash."""
        mock_dynamodb_client.query.side_effect = Exception("Something went wrong")

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None

    def test_malformed_data(self, mock_dynamodb_client, cache_config_enabled):
        """When data is malformed, should return None and not crash."""
        future_ttl = int(time.time()) + 3600

        mock_dynamodb_client.query.return_value = {
            "Items": [
                {
                    "cache_key": {"S": "accounts"},
                    "item_id": {"S": "all"},
                    "data": {"S": "invalid json"},
                    "ttl": {"N": str(future_ttl)},
                }
            ]
        }

        result = cache_module.get_cached_accounts(mock_dynamodb_client, cache_config_enabled)

        assert result is None


class TestSetCachedAccounts:
    """Tests for set_cached_accounts function."""

    def test_cache_disabled_no_write(self, mock_dynamodb_client, cache_config_disabled, sample_accounts):
        """When cache is disabled, should not write to DynamoDB."""
        cache_module.set_cached_accounts(mock_dynamodb_client, cache_config_disabled, sample_accounts)

        mock_dynamodb_client.put_item.assert_not_called()

    def test_successful_write(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When cache is enabled, should write accounts to DynamoDB."""
        cache_module.set_cached_accounts(mock_dynamodb_client, cache_config_enabled, sample_accounts)

        mock_dynamodb_client.put_item.assert_called_once()
        call_args = mock_dynamodb_client.put_item.call_args[1]
        assert call_args["TableName"] == "test-cache-table"
        assert "Item" in call_args
        assert call_args["Item"]["cache_key"]["S"] == "accounts"

    def test_table_does_not_exist_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When table doesn't exist during write, should not crash."""
        mock_dynamodb_client.put_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}, "PutItem"
        )

        # Should not raise exception
        cache_module.set_cached_accounts(mock_dynamodb_client, cache_config_enabled, sample_accounts)

    def test_access_denied_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When access is denied during write, should not crash."""
        mock_dynamodb_client.put_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}}, "PutItem"
        )

        # Should not raise exception
        cache_module.set_cached_accounts(mock_dynamodb_client, cache_config_enabled, sample_accounts)

    def test_generic_exception_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_accounts):
        """When any exception occurs during write, should not crash."""
        mock_dynamodb_client.put_item.side_effect = Exception("Something went wrong")

        # Should not raise exception
        cache_module.set_cached_accounts(mock_dynamodb_client, cache_config_enabled, sample_accounts)


class TestGetCachedPermissionSets:
    """Tests for get_cached_permission_sets function."""

    def test_cache_disabled_returns_none(self, mock_dynamodb_client, cache_config_disabled):
        """When cache is disabled, should return None without calling DynamoDB."""
        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_disabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None
        mock_dynamodb_client.query.assert_not_called()

    def test_cache_miss_no_items(self, mock_dynamodb_client, cache_config_enabled):
        """When no items found in cache, should return None."""
        mock_dynamodb_client.query.return_value = {"Items": []}

        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None

    def test_cache_hit_valid_data(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When cache has valid data, should return permission sets."""
        future_ttl = int(time.time()) + 3600
        ps_data = [ps.dict() for ps in sample_permission_sets]

        mock_dynamodb_client.query.return_value = {
            "Items": [
                {
                    "cache_key": {"S": "permission_sets"},
                    "item_id": {"S": "arn:aws:sso:::instance/ssoins-1111111111111111"},
                    "data": {"S": json.dumps(ps_data)},
                    "ttl": {"N": str(future_ttl)},
                }
            ]
        }

        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is not None
        assert len(result) == 2
        assert result[0].name == "AdministratorAccess"

    def test_cache_expired_data(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When cache data is expired, should return None."""
        past_ttl = int(time.time()) - 3600
        ps_data = [ps.dict() for ps in sample_permission_sets]

        mock_dynamodb_client.query.return_value = {
            "Items": [
                {
                    "cache_key": {"S": "permission_sets"},
                    "item_id": {"S": "arn:aws:sso:::instance/ssoins-1111111111111111"},
                    "data": {"S": json.dumps(ps_data)},
                    "ttl": {"N": str(past_ttl)},
                }
            ]
        }

        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None

    def test_table_does_not_exist(self, mock_dynamodb_client, cache_config_enabled):
        """When DynamoDB table doesn't exist, should return None."""
        mock_dynamodb_client.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}, "Query"
        )

        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None

    def test_invalid_arn_format(self, mock_dynamodb_client, cache_config_enabled):
        """When ARN format is invalid, should return None and not call DynamoDB."""
        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "invalid-arn-format",
        )

        assert result is None
        mock_dynamodb_client.query.assert_not_called()

    def test_arn_injection_attempt(self, mock_dynamodb_client, cache_config_enabled):
        """When ARN contains injection attempt, should return None and not call DynamoDB."""
        result = cache_module.get_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/test' OR '1'='1",
        )

        assert result is None
        mock_dynamodb_client.query.assert_not_called()


class TestSetCachedPermissionSets:
    """Tests for set_cached_permission_sets function."""

    def test_cache_disabled_no_write(self, mock_dynamodb_client, cache_config_disabled, sample_permission_sets):
        """When cache is disabled, should not write to DynamoDB."""
        cache_module.set_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_disabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
            sample_permission_sets,
        )

        mock_dynamodb_client.put_item.assert_not_called()

    def test_successful_write(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When cache is enabled, should write permission sets to DynamoDB."""
        cache_module.set_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
            sample_permission_sets,
        )

        mock_dynamodb_client.put_item.assert_called_once()
        call_args = mock_dynamodb_client.put_item.call_args[1]
        assert call_args["TableName"] == "test-cache-table"
        assert call_args["Item"]["cache_key"]["S"] == "permission_sets"

    def test_table_does_not_exist_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When table doesn't exist during write, should not crash."""
        mock_dynamodb_client.put_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}, "PutItem"
        )

        # Should not raise exception
        cache_module.set_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
            sample_permission_sets,
        )

    def test_invalid_arn_format_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When ARN format is invalid during write, should not crash and not call DynamoDB."""
        cache_module.set_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "invalid-arn-format",
            sample_permission_sets,
        )

        # Should not call DynamoDB due to validation failure
        mock_dynamodb_client.put_item.assert_not_called()

    def test_arn_injection_attempt_on_write(self, mock_dynamodb_client, cache_config_enabled, sample_permission_sets):
        """When ARN contains injection attempt during write, should not crash and not call DynamoDB."""
        cache_module.set_cached_permission_sets(
            mock_dynamodb_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/test' OR '1'='1",
            sample_permission_sets,
        )

        # Should not call DynamoDB due to validation failure
        mock_dynamodb_client.put_item.assert_not_called()


class TestWithCacheFallback:
    """Tests for with_cache_fallback function."""

    def test_cache_hit_returns_cached_data(self):
        """When cache has data, should return cached data without calling API."""
        cached_data = [Account(id="111111111111", name="Cached Account")]
        cache_getter = Mock(return_value=cached_data)
        api_getter = Mock()
        cache_setter = Mock()

        result = cache_module.with_cache_fallback(cache_getter, api_getter, cache_setter, "accounts")

        assert result == cached_data
        cache_getter.assert_called_once()
        api_getter.assert_not_called()
        cache_setter.assert_not_called()

    def test_cache_miss_calls_api_and_updates_cache(self):
        """When cache misses, should call API and update cache."""
        api_data = [Account(id="111111111111", name="API Account")]
        cache_getter = Mock(return_value=None)
        api_getter = Mock(return_value=api_data)
        cache_setter = Mock()

        result = cache_module.with_cache_fallback(cache_getter, api_getter, cache_setter, "accounts")

        assert result == api_data
        cache_getter.assert_called_once()
        api_getter.assert_called_once()
        cache_setter.assert_called_once_with(api_data)

    def test_cache_error_falls_back_to_api(self):
        """When cache lookup fails, should fall back to API."""
        api_data = [Account(id="111111111111", name="API Account")]
        cache_getter = Mock(side_effect=Exception("Cache error"))
        api_getter = Mock(return_value=api_data)
        cache_setter = Mock()

        result = cache_module.with_cache_fallback(cache_getter, api_getter, cache_setter, "accounts")

        assert result == api_data
        api_getter.assert_called_once()

    def test_cache_setter_error_does_not_affect_result(self):
        """When cache setter fails, should still return API data."""
        api_data = [Account(id="111111111111", name="API Account")]
        cache_getter = Mock(return_value=None)
        api_getter = Mock(return_value=api_data)
        cache_setter = Mock(side_effect=Exception("Cache write error"))

        result = cache_module.with_cache_fallback(cache_getter, api_getter, cache_setter, "accounts")

        assert result == api_data
        api_getter.assert_called_once()

    def test_api_error_propagates(self):
        """When API call fails, should propagate the error."""
        cache_getter = Mock(return_value=None)
        api_getter = Mock(side_effect=Exception("API error"))
        cache_setter = Mock()

        with pytest.raises(Exception, match="API error"):
            cache_module.with_cache_fallback(cache_getter, api_getter, cache_setter, "accounts")


class TestCacheKeyConstants:
    """Tests for CacheKey constants."""

    def test_cache_key_constants(self):
        """Test cache key constants are defined correctly."""
        assert cache_module.CacheKey.ACCOUNTS == "accounts"
        assert cache_module.CacheKey.PERMISSION_SETS == "permission_sets"


class TestTTLHelpers:
    """Tests for TTL helper functions."""

    def test_get_ttl_timestamp(self):
        """Test TTL timestamp calculation."""
        ttl_minutes = 60
        before_time = int(time.time()) + (ttl_minutes * 60)

        ttl = cache_module._get_ttl_timestamp(ttl_minutes)

        after_time = int(time.time()) + (ttl_minutes * 60)

        # TTL should be between before_time and after_time
        assert before_time <= ttl <= after_time

    def test_is_cache_valid_returns_true_for_valid_cache(self):
        """Test cache validity check for valid cache."""
        future_ttl = int(time.time()) + 3600
        item = {"ttl": str(future_ttl)}

        assert cache_module._is_cache_valid(item) is True

    def test_is_cache_valid_returns_false_for_expired_cache(self):
        """Test cache validity check for expired cache."""
        past_ttl = int(time.time()) - 3600
        item = {"ttl": str(past_ttl)}

        assert cache_module._is_cache_valid(item) is False

    def test_is_cache_valid_returns_false_for_missing_ttl(self):
        """Test cache validity check when TTL is missing."""
        item = {}

        assert cache_module._is_cache_valid(item) is False

    def test_get_ttl_timestamp_validates_input_type(self):
        """Test that _get_ttl_timestamp validates input type to prevent injection."""
        import pytest

        # Test with non-numeric string
        with pytest.raises(ValueError, match="Invalid TTL minutes value"):
            cache_module._get_ttl_timestamp("not_a_number")  # type: ignore[arg-type]

        # Test with None
        with pytest.raises(ValueError, match="Invalid TTL minutes value"):
            cache_module._get_ttl_timestamp(None)  # type: ignore[arg-type]

    def test_get_ttl_timestamp_validates_bounds(self):
        """Test that _get_ttl_timestamp validates bounds to prevent injection."""
        import pytest

        # Test with negative value
        with pytest.raises(
            ValueError, match=f"TTL minutes must be between {cache_module.MIN_TTL_MINUTES} and {cache_module.MAX_TTL_MINUTES}"
        ):
            cache_module._get_ttl_timestamp(-1)

        # Test with zero (should fail since minimum is 1)
        with pytest.raises(
            ValueError, match=f"TTL minutes must be between {cache_module.MIN_TTL_MINUTES} and {cache_module.MAX_TTL_MINUTES}"
        ):
            cache_module._get_ttl_timestamp(0)

        # Test with value over max (more than 1 year in minutes)
        with pytest.raises(
            ValueError, match=f"TTL minutes must be between {cache_module.MIN_TTL_MINUTES} and {cache_module.MAX_TTL_MINUTES}"
        ):
            cache_module._get_ttl_timestamp(cache_module.MAX_TTL_MINUTES + 1)

    def test_get_ttl_timestamp_with_valid_values(self):
        """Test that _get_ttl_timestamp works with valid values."""
        # Test with minimum valid value
        ttl = cache_module._get_ttl_timestamp(cache_module.MIN_TTL_MINUTES)
        assert ttl > int(time.time())

        # Test with maximum valid value
        ttl = cache_module._get_ttl_timestamp(cache_module.MAX_TTL_MINUTES)
        assert ttl > int(time.time())

        # Test with default value (5760 = 4 days)
        ttl = cache_module._get_ttl_timestamp(5760)
        expected = int(time.time()) + (5760 * 60)
        # Allow 2 seconds tolerance for test execution time
        assert abs(ttl - expected) <= 2
