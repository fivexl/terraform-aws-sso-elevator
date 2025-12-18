"""Unit tests for the cache module.

Tests cover all error handling scenarios including:
- S3 bucket doesn't exist
- Wrong bucket name
- Missing IAM permissions (simulated)
- Cache disabled (cache_enabled = false)
- Cache hit/miss scenarios
- Write failures
"""

import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import cache as cache_module
from entities.aws import Account, PermissionSet


@pytest.fixture
def cache_config_enabled():
    """Cache config with caching enabled."""
    return cache_module.CacheConfig(
        bucket_name="test-config-bucket",
        enabled=True,
    )


@pytest.fixture
def cache_config_disabled():
    """Cache config with caching disabled."""
    return cache_module.CacheConfig(
        bucket_name="test-config-bucket",
        enabled=False,
    )


@pytest.fixture
def mock_s3_client():
    """Mock S3 client."""
    client = Mock()
    client.exceptions = Mock()
    client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    client.exceptions.NoSuchBucket = type("NoSuchBucket", (Exception,), {})
    return client


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
            bucket_name="test-config-bucket",
            enabled=True,
        )
        assert config.bucket_name == "test-config-bucket"
        assert config.enabled is True

    def test_cache_config_disabled(self):
        """Test cache config when disabled."""
        config = cache_module.CacheConfig(
            bucket_name="test-config-bucket",
            enabled=False,
        )
        assert config.bucket_name == "test-config-bucket"
        assert config.enabled is False


class TestGetCachedAccounts:
    """Tests for get_cached_accounts function."""

    def test_cache_disabled_returns_none(self, mock_s3_client, cache_config_disabled):
        """When cache is disabled, should return None without calling S3."""
        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_disabled)

        assert result is None
        mock_s3_client.get_object.assert_not_called()

    def test_cache_miss_no_object(self, mock_s3_client, cache_config_enabled):
        """When object not found in cache, should return None."""
        mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchKey()

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is None
        mock_s3_client.get_object.assert_called_once()

    def test_cache_hit_valid_data(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When cache has valid data, should return accounts."""
        accounts_data = [acc.dict() for acc in sample_accounts]
        body_mock = Mock()
        body_mock.read.return_value = json.dumps(accounts_data).encode("utf-8")

        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
        }

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is not None
        assert len(result) == 2
        assert result[0].id == "111111111111"
        assert result[1].id == "222222222222"

    def test_cache_hit_returns_data(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When cache has data, should return it (no TTL check)."""
        accounts_data = [acc.dict() for acc in sample_accounts]
        body_mock = Mock()
        body_mock.read.return_value = json.dumps(accounts_data).encode("utf-8")

        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
        }

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is not None
        assert len(result) == 2

    def test_bucket_doesnt_exist(self, mock_s3_client, cache_config_enabled):
        """When bucket doesn't exist, should return None gracefully."""
        mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchBucket()

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is None

    def test_access_denied(self, mock_s3_client, cache_config_enabled):
        """When access is denied, should return None gracefully."""
        mock_s3_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "GetObject",
        )

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is None

    def test_generic_exception(self, mock_s3_client, cache_config_enabled):
        """When generic exception occurs, should return None gracefully."""
        mock_s3_client.get_object.side_effect = Exception("Something went wrong")

        result = cache_module.get_cached_accounts(mock_s3_client, cache_config_enabled)

        assert result is None


class TestSetCachedAccounts:
    """Tests for set_cached_accounts function."""

    def test_cache_disabled_no_write(self, mock_s3_client, cache_config_disabled, sample_accounts):
        """When cache is disabled, should not write to S3."""
        cache_module.set_cached_accounts(mock_s3_client, cache_config_disabled, sample_accounts)

        mock_s3_client.put_object.assert_not_called()

    def test_successful_write(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When cache is enabled, should write to S3."""
        cache_module.set_cached_accounts(mock_s3_client, cache_config_enabled, sample_accounts)

        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args
        assert call_args[1]["Bucket"] == "test-config-bucket"
        assert call_args[1]["Key"] == "accounts.json"
        assert call_args[1]["ContentType"] == "application/json"

    def test_bucket_doesnt_exist_during_write(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When bucket doesn't exist during write, should fail gracefully."""
        mock_s3_client.put_object.side_effect = mock_s3_client.exceptions.NoSuchBucket()

        # Should not raise exception
        cache_module.set_cached_accounts(mock_s3_client, cache_config_enabled, sample_accounts)

    def test_access_denied_during_write(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When access is denied during write, should fail gracefully."""
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )

        # Should not raise exception
        cache_module.set_cached_accounts(mock_s3_client, cache_config_enabled, sample_accounts)

    def test_generic_exception_during_write(self, mock_s3_client, cache_config_enabled, sample_accounts):
        """When generic exception occurs during write, should fail gracefully."""
        mock_s3_client.put_object.side_effect = Exception("Something went wrong")

        # Should not raise exception
        cache_module.set_cached_accounts(mock_s3_client, cache_config_enabled, sample_accounts)


class TestGetCachedPermissionSets:
    """Tests for get_cached_permission_sets function."""

    def test_cache_disabled_returns_none(self, mock_s3_client, cache_config_disabled):
        """When cache is disabled, should return None without calling S3."""
        result = cache_module.get_cached_permission_sets(
            mock_s3_client,
            cache_config_disabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None
        mock_s3_client.get_object.assert_not_called()

    def test_cache_miss_no_object(self, mock_s3_client, cache_config_enabled):
        """When object not found in cache, should return None."""
        mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchKey()

        result = cache_module.get_cached_permission_sets(
            mock_s3_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is None

    def test_cache_hit_valid_data(self, mock_s3_client, cache_config_enabled, sample_permission_sets):
        """When cache has valid data, should return permission sets."""
        ps_data = [ps.dict() for ps in sample_permission_sets]
        body_mock = Mock()
        body_mock.read.return_value = json.dumps(ps_data).encode("utf-8")

        mock_s3_client.get_object.return_value = {
            "Body": body_mock,
        }

        result = cache_module.get_cached_permission_sets(
            mock_s3_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
        )

        assert result is not None
        assert len(result) == 2
        assert result[0].name == "AdministratorAccess"


class TestSetCachedPermissionSets:
    """Tests for set_cached_permission_sets function."""

    def test_cache_disabled_no_write(self, mock_s3_client, cache_config_disabled, sample_permission_sets):
        """When cache is disabled, should not write to S3."""
        cache_module.set_cached_permission_sets(
            mock_s3_client,
            cache_config_disabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
            sample_permission_sets,
        )

        mock_s3_client.put_object.assert_not_called()

    def test_successful_write(self, mock_s3_client, cache_config_enabled, sample_permission_sets):
        """When cache is enabled, should write to S3."""
        cache_module.set_cached_permission_sets(
            mock_s3_client,
            cache_config_enabled,
            "arn:aws:sso:::instance/ssoins-1111111111111111",
            sample_permission_sets,
        )

        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args
        assert call_args[1]["Bucket"] == "test-config-bucket"
        assert "permission_sets/" in call_args[1]["Key"]


class TestCacheResilience:
    """Tests for with_cache_resilience function."""

    def test_api_success_returns_api_data(self, sample_accounts):
        """When API succeeds, should return API data (parallel execution)."""
        cache_getter = Mock(return_value=sample_accounts)
        api_getter = Mock(return_value=sample_accounts)
        cache_setter = Mock()

        result = cache_module.with_cache_resilience(
            cache_getter=cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )

        assert result == sample_accounts
        cache_getter.assert_called_once()
        api_getter.assert_called_once()

    def test_api_success_with_no_cache_updates_cache(self, sample_accounts):
        """When API succeeds and no cache, should update cache."""
        cache_getter = Mock(return_value=None)
        api_getter = Mock(return_value=sample_accounts)
        cache_setter = Mock()

        result = cache_module.with_cache_resilience(
            cache_getter=cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )

        assert result == sample_accounts
        cache_setter.assert_called_once_with(sample_accounts)

    def test_api_failure_returns_cached_data(self, sample_accounts):
        """When API fails but cache has data, should return cached data."""
        cache_getter = Mock(return_value=sample_accounts)
        api_getter = Mock(side_effect=Exception("API error"))
        cache_setter = Mock()

        result = cache_module.with_cache_resilience(
            cache_getter=cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )

        assert result == sample_accounts
        cache_setter.assert_not_called()

    def test_both_fail_raises_exception(self):
        """When both API and cache fail, should raise exception."""
        cache_getter = Mock(side_effect=ValueError("Cache error"))
        api_getter = Mock(side_effect=ValueError("API error"))
        cache_setter = Mock()

        with pytest.raises(ValueError, match="API error"):
            cache_module.with_cache_resilience(
                cache_getter=cache_getter,
                api_getter=api_getter,
                cache_setter=cache_setter,
                resource_name="test",
            )
