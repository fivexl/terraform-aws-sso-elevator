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


@pytest.fixture
def sample_users():
    """Sample Identity Store user dicts, the same raw shape sso.list_users
    returns under its "Users" key -- unlike accounts/permission sets, users
    aren't cached through a pydantic model."""
    return [
        {"UserId": "u-1", "UserName": "alice@example.com", "Emails": [{"Value": "alice@example.com", "Primary": True}]},
        {"UserId": "u-2", "UserName": "bob@example.com", "Emails": [{"Value": "bob@example.com", "Primary": True}]},
    ]


class TestGetCachedUsers:
    """Tests for get_cached_users function (#193 item 2)."""

    def test_cache_disabled_returns_none(self, mock_s3_client, cache_config_disabled):
        """When cache is disabled, should return None without calling S3."""
        result = cache_module.get_cached_users(mock_s3_client, cache_config_disabled, "d-1234567890")

        assert result is None
        mock_s3_client.get_object.assert_not_called()

    def test_cache_miss_no_object(self, mock_s3_client, cache_config_enabled):
        """When object not found in cache, should return None."""
        mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchKey()

        result = cache_module.get_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890")

        assert result is None
        mock_s3_client.get_object.assert_called_once()

    def test_cache_hit_valid_data(self, mock_s3_client, cache_config_enabled, sample_users):
        """When cache has valid data, should return the raw user dicts."""
        body_mock = Mock()
        body_mock.read.return_value = json.dumps(sample_users).encode("utf-8")
        mock_s3_client.get_object.return_value = {"Body": body_mock}

        result = cache_module.get_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890")

        assert result == sample_users

    def test_invalid_identity_store_id_returns_none(self, mock_s3_client, cache_config_enabled):
        """An identity_store_id that fails validation must degrade to a
        cache miss, the same as any other lookup failure -- not raise and
        take the whole request down."""
        result = cache_module.get_cached_users(mock_s3_client, cache_config_enabled, "not valid! id")

        assert result is None
        mock_s3_client.get_object.assert_not_called()

    def test_generic_exception(self, mock_s3_client, cache_config_enabled):
        """When generic exception occurs, should return None gracefully."""
        mock_s3_client.get_object.side_effect = Exception("Something went wrong")

        result = cache_module.get_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890")

        assert result is None


class TestSetCachedUsers:
    """Tests for set_cached_users function (#193 item 2)."""

    def test_cache_disabled_no_write(self, mock_s3_client, cache_config_disabled, sample_users):
        """When cache is disabled, should not write to S3."""
        cache_module.set_cached_users(mock_s3_client, cache_config_disabled, "d-1234567890", sample_users)

        mock_s3_client.put_object.assert_not_called()

    def test_successful_write(self, mock_s3_client, cache_config_enabled, sample_users):
        """When cache is enabled, should write to S3, keyed by identity_store_id."""
        cache_module.set_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890", sample_users)

        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args
        assert call_args[1]["Bucket"] == "test-config-bucket"
        assert call_args[1]["Key"] == "users/d-1234567890.json"
        assert call_args[1]["ContentType"] == "application/json"

    def test_generic_exception_during_write(self, mock_s3_client, cache_config_enabled, sample_users):
        """When generic exception occurs during write, should fail gracefully."""
        mock_s3_client.put_object.side_effect = Exception("Something went wrong")

        # Should not raise exception
        cache_module.set_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890", sample_users)

    def test_a_payload_over_the_generic_cache_limit_still_writes(self, mock_s3_client, cache_config_enabled):
        """Regression test (#194 High #4, found live by Andrey Devyatkin):
        a representative user record is ~445 bytes, so the generic
        MAX_DATA_SIZE (5MB, sized for the small accounts/permission-sets
        caches) is crossed at just 11,616 users -- above that, this used to
        silently fail to write anything at all, so the fallback this cache
        exists to provide didn't exist on exactly the directories large
        enough to need it. set_cached_users must use the much higher
        MAX_USERS_DATA_SIZE instead."""
        # One record is ~50 bytes; 150,000 of them (~7.5MB) comfortably
        # exceeds the old 5MB MAX_DATA_SIZE while staying under the new
        # 50MB MAX_USERS_DATA_SIZE.
        many_users = [{"UserId": f"u-{i}", "UserName": f"user{i}@example.com"} for i in range(150_000)]

        cache_module.set_cached_users(mock_s3_client, cache_config_enabled, "d-1234567890", many_users)

        mock_s3_client.put_object.assert_called_once()


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

    def test_empty_api_result_does_not_overwrite_a_good_cache(self, sample_accounts):
        """Regression test (#194 High #4, found live by Andrey Devyatkin):
        the only guard before writing API data to the cache used to be "is
        it not None", not a sanity check on its content. A genuinely empty
        API response (a pagination fluke, a transient AWS-side bug -- a real
        deployment actually using this module always has at least one real
        account/permission-set/user) must not silently overwrite a real,
        non-empty cache, destroying the fallback this cache exists to
        provide. The empty result is still returned to the caller, though --
        this is a cache-write safeguard, not a change to what's reported as
        the current live answer."""
        cache_getter = Mock(return_value=sample_accounts)
        api_getter = Mock(return_value=[])
        cache_setter = Mock()

        result = cache_module.with_cache_resilience(
            cache_getter=cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )

        assert result == []
        cache_setter.assert_not_called()

    def test_empty_api_result_still_writes_when_cache_was_already_empty(self):
        """Companion to the test above: an empty result is only suspicious
        relative to a non-empty cache. With no prior cache at all, storing
        the (still possibly-correct) empty result is the same "no cached
        data, store API result" path as any other first-ever write."""
        cache_getter = Mock(return_value=None)
        api_getter = Mock(return_value=[])
        cache_setter = Mock()

        result = cache_module.with_cache_resilience(
            cache_getter=cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )

        assert result == []
        cache_setter.assert_called_once_with([])

    def test_a_stalled_cache_read_does_not_block_an_already_successful_api_call(self, sample_accounts, monkeypatch):
        """Regression test (#194 High #4, found live by Andrey Devyatkin):
        the cache-read side of the parallel lookup used to have no timeout
        at all, so a stalled S3 read blocked the whole call even after the
        API call -- running concurrently on its own thread -- had already
        returned successfully. Patches the timeout down to keep this test
        fast rather than actually waiting out the real default."""
        import time

        monkeypatch.setattr(cache_module, "CACHE_LOOKUP_TIMEOUT_SECONDS", 0.05)

        def slow_cache_getter():
            time.sleep(0.5)
            return sample_accounts

        api_getter = Mock(return_value=sample_accounts)
        cache_setter = Mock()

        start = time.monotonic()
        result = cache_module.with_cache_resilience(
            cache_getter=slow_cache_getter,
            api_getter=api_getter,
            cache_setter=cache_setter,
            resource_name="test",
        )
        elapsed = time.monotonic() - start

        assert result == sample_accounts
        assert elapsed < 0.5, "should not have waited out the full slow cache read once the API call succeeded"
