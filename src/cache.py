"""Cache module for caching AWS accounts and permission sets in S3."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

import config
from entities.aws import Account, PermissionSet

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = config.get_logger(service="cache")

T = TypeVar("T")

# Regex pattern for validating AWS ARN format
ARN_PATTERN = re.compile(r"^arn:aws(?:-[a-z0-9]+){0,5}:sso:::\w+/[\w-]+$")

# Maximum ARN length to prevent excessively long input
MAX_ARN_LENGTH = 1024

# Maximum size for serialized data to prevent excessively large payloads (in bytes).
# Sized for the accounts/permission-sets caches, which are genuinely small
# (kilobytes) for any real org -- a company doesn't have thousands of AWS
# accounts or permission sets.
MAX_DATA_SIZE = 5 * 1024 * 1024  # 5MB limit for S3 objects

# The users cache needs a much higher ceiling than MAX_DATA_SIZE (#194 High
# #4, found by Andrey Devyatkin): a representative ListUsers record is ~445
# bytes, so MAX_DATA_SIZE's 5MB is crossed at just 11,616 users -- above
# that, set_cached_users silently failed to write anything at all (caught,
# logged as a warning, cache left empty), so the fallback this cache exists
# to provide didn't exist on exactly the large directories most likely to
# hit a throttle in the first place. 50MB comfortably covers a directory of
# roughly 100k+ users; S3 itself supports objects up to 5TB, so there's no
# real ceiling being worked around here, just headroom against a runaway
# payload.
MAX_USERS_DATA_SIZE = 50 * 1024 * 1024  # 50MB limit for the users cache specifically

# Pattern for validating S3 bucket names
BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

# with_cache_resilience waits on this before treating the cache lookup as
# failed (#194 High #4, found by Andrey Devyatkin): the cache read used to
# have no timeout at all, so a stalled S3 GetObject call blocked the whole
# request even after the live API call -- running in parallel on a separate
# thread -- had already returned successfully. A few seconds is generous for
# a same-region S3 read; anything slower than that is not a cache worth
# waiting on when a real answer is already in hand.
CACHE_LOOKUP_TIMEOUT_SECONDS = 5


@dataclass
class CacheConfig:
    """Configuration for the cache."""

    bucket_name: str
    enabled: bool
    # "" (not None) when unset, matching cfg.config_bucket_kms_key_arn's own
    # sentinel -- see that field's docstring in config.py.
    kms_key_arn: str = ""

    @staticmethod
    def from_config(cfg: config.Config) -> "CacheConfig":
        """Create a CacheConfig from the application config."""
        return CacheConfig(
            bucket_name=cfg.config_bucket_name,
            enabled=cfg.cache_enabled,
            kms_key_arn=cfg.config_bucket_kms_key_arn,
        )


class CacheKey:
    """Constants for cache keys."""

    ACCOUNTS = "accounts.json"
    PERMISSION_SETS_PREFIX = "permission_sets/"
    USERS_PREFIX = "users/"


# IAM Identity Store IDs are documented as "d-" followed by 10 lowercase
# hex characters, but that's not a versioned public contract -- matched
# loosely (a bounded alphanumeric/hyphen string) rather than pinned to
# that exact shape, the same way _validate_arn exists to reject garbage
# input before it becomes an S3 key, not to enforce AWS's own format.
IDENTITY_STORE_ID_PATTERN = re.compile(r"^[\w-]{1,64}$")


def _validate_identity_store_id(identity_store_id: str) -> str:
    """Validate and sanitize an Identity Store ID used as part of a cache key.

    Args:
        identity_store_id: Identity Store ID to validate

    Returns:
        Validated identity_store_id string

    Raises:
        ValueError: If the identity_store_id format is invalid
    """
    if not isinstance(identity_store_id, str):
        raise ValueError(f"identity_store_id must be a string, got {type(identity_store_id)}")

    if not IDENTITY_STORE_ID_PATTERN.match(identity_store_id):
        raise ValueError(f"Invalid identity_store_id format: {identity_store_id}")

    return identity_store_id


def _validate_arn(arn: str) -> str:
    """Validate and sanitize SSO instance ARN.

    Args:
        arn: SSO instance ARN to validate

    Returns:
        Validated ARN string

    Raises:
        ValueError: If the ARN format is invalid
    """
    if not isinstance(arn, str):
        raise ValueError(f"ARN must be a string, got {type(arn)}")

    if not ARN_PATTERN.match(arn):
        raise ValueError(f"Invalid SSO instance ARN format: {arn}")

    if len(arn) > MAX_ARN_LENGTH:
        raise ValueError("ARN exceeds maximum length")

    return arn


def _validate_bucket_name(bucket_name: str) -> str:
    """Validate S3 bucket name to prevent injection attacks.

    Args:
        bucket_name: Bucket name to validate

    Returns:
        Validated bucket name

    Raises:
        ValueError: If the bucket name format is invalid
    """
    if not isinstance(bucket_name, str):
        raise ValueError(f"Bucket name must be a string, got {type(bucket_name)}")

    if not BUCKET_NAME_PATTERN.match(bucket_name):
        raise ValueError(f"Invalid S3 bucket name format: {bucket_name}")

    return bucket_name


def _sanitize_json_data(data: list[dict[str, Any]], max_size: int = MAX_DATA_SIZE) -> str:
    """Sanitize and validate data before storing in S3.

    Args:
        data: List of dictionaries to serialize
        max_size: Maximum allowed serialized size in bytes -- defaults to
            MAX_DATA_SIZE (sized for the small accounts/permission-sets
            caches); set_cached_users passes MAX_USERS_DATA_SIZE instead,
            since a real user directory can be orders of magnitude larger.

    Returns:
        JSON string of the sanitized data

    Raises:
        ValueError: If the data is invalid or too large
    """
    if not isinstance(data, list):
        raise ValueError(f"Data must be a list, got {type(data)}")

    try:
        serialized = json.dumps(data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Data contains non-serializable content: {e}") from e

    data_size = len(serialized.encode("utf-8"))
    if data_size > max_size:
        raise ValueError(f"Data size ({data_size} bytes) exceeds maximum allowed size ({max_size} bytes)")

    return serialized


def _encryption_kwargs(cache_config: CacheConfig) -> dict[str, str]:
    """put_object kwargs for whichever encryption this bucket is actually
    configured for.

    An explicit ServerSideEncryption on a PUT overrides the bucket's own
    default (documented S3 behaviour) -- hardcoding "AES256" here regardless
    of cache_config.kms_key_arn would silently ignore an operator's own KMS
    key even when they've already set one up for this exact bucket (the
    Terraform-managed approval-config.json object in it already honours
    config_bucket_kms_key_arn; #194 High #5, found by Andrey Devyatkin).
    Falls back to plain AES256 -- the same default this bucket and every
    write here already used -- when no key is configured."""
    if cache_config.kms_key_arn:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": cache_config.kms_key_arn}
    return {"ServerSideEncryption": "AES256"}


def get_cached_accounts(
    s3_client: S3Client,
    cache_config: CacheConfig,
) -> Optional[list[Account]]:
    """Get cached accounts from S3.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration

    Returns:
        List of cached accounts or None if cache miss
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache lookup")
        return None

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)

        response = s3_client.get_object(
            Bucket=validated_bucket_name,
            Key=CacheKey.ACCOUNTS,
        )

        accounts_data = json.loads(response["Body"].read().decode("utf-8"))
        accounts = [Account.model_validate(acc) for acc in accounts_data]

        logger.info(f"Retrieved {len(accounts)} accounts from cache")
        return accounts

    except s3_client.exceptions.NoSuchKey:
        logger.info("Cache miss for accounts - no cached data found")
        return None
    except Exception as e:
        logger.warning(f"Failed to get cached accounts: {e}", extra={"error": str(e)})
        return None


def set_cached_accounts(
    s3_client: S3Client,
    cache_config: CacheConfig,
    accounts: list[Account],
) -> None:
    """Store accounts in S3 cache.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration
        accounts: List of accounts to cache
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)

        accounts_data = [account.dict() for account in accounts]
        sanitized_data = _sanitize_json_data(accounts_data)

        s3_client.put_object(
            Bucket=validated_bucket_name,
            Key=CacheKey.ACCOUNTS,
            Body=sanitized_data.encode("utf-8"),
            ContentType="application/json",
            **_encryption_kwargs(cache_config),
        )

        logger.info(f"Cached {len(accounts)} accounts")

    except ValueError as e:
        logger.warning(f"Validation failed when caching accounts: {e}", extra={"error": str(e)})
    except Exception as e:
        logger.warning(f"Failed to cache accounts: {e}", extra={"error": str(e)})


def get_cached_permission_sets(
    s3_client: S3Client,
    cache_config: CacheConfig,
    sso_instance_arn: str,
) -> Optional[list[PermissionSet]]:
    """Get cached permission sets from S3.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration
        sso_instance_arn: SSO instance ARN (used as part of cache key)

    Returns:
        List of cached permission sets or None if cache miss
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache lookup")
        return None

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)
        validated_arn = _validate_arn(sso_instance_arn)

        arn_hash = validated_arn.replace(":", "_").replace("/", "_")
        key = f"{CacheKey.PERMISSION_SETS_PREFIX}{arn_hash}.json"

        response = s3_client.get_object(
            Bucket=validated_bucket_name,
            Key=key,
        )

        permission_sets_data = json.loads(response["Body"].read().decode("utf-8"))
        permission_sets = [PermissionSet.model_validate(ps) for ps in permission_sets_data]

        logger.info(f"Retrieved {len(permission_sets)} permission sets from cache")
        return permission_sets

    except s3_client.exceptions.NoSuchKey:
        logger.info("Cache miss for permission sets - no cached data found")
        return None
    except Exception as e:
        logger.warning(f"Failed to get cached permission sets: {e}", extra={"error": str(e)})
        return None


def set_cached_permission_sets(
    s3_client: S3Client,
    cache_config: CacheConfig,
    sso_instance_arn: str,
    permission_sets: list[PermissionSet],
) -> None:
    """Store permission sets in S3 cache.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration
        sso_instance_arn: SSO instance ARN (used as part of cache key)
        permission_sets: List of permission sets to cache
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)
        validated_arn = _validate_arn(sso_instance_arn)

        permission_sets_data = [ps.dict() for ps in permission_sets]
        sanitized_data = _sanitize_json_data(permission_sets_data)

        arn_hash = validated_arn.replace(":", "_").replace("/", "_")
        key = f"{CacheKey.PERMISSION_SETS_PREFIX}{arn_hash}.json"

        s3_client.put_object(
            Bucket=validated_bucket_name,
            Key=key,
            Body=sanitized_data.encode("utf-8"),
            ContentType="application/json",
            **_encryption_kwargs(cache_config),
        )

        logger.info(f"Cached {len(permission_sets)} permission sets")

    except ValueError as e:
        logger.warning(f"Validation failed when caching permission sets: {e}", extra={"error": str(e)})
    except Exception as e:
        logger.warning(f"Failed to cache permission sets: {e}", extra={"error": str(e)})


def get_cached_users(
    s3_client: S3Client,
    cache_config: CacheConfig,
    identity_store_id: str,
) -> Optional[list[dict[str, Any]]]:
    """Get cached Identity Store users (raw dicts, the same shape sso.list_users
    returns under its "Users" key) from S3.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration
        identity_store_id: Identity Store ID (used as part of cache key)

    Returns:
        List of cached user dicts or None if cache miss
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache lookup")
        return None

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)
        validated_identity_store_id = _validate_identity_store_id(identity_store_id)

        key = f"{CacheKey.USERS_PREFIX}{validated_identity_store_id}.json"

        response = s3_client.get_object(
            Bucket=validated_bucket_name,
            Key=key,
        )

        users = json.loads(response["Body"].read().decode("utf-8"))

        # Minimal shape validation (#194 High #4 residual, found by Andrey
        # Devyatkin): unlike get_cached_accounts/get_cached_permission_sets,
        # which each call .model_validate(...) and degrade to a clean cache
        # miss on a shape mismatch, this used to trust json.loads' raw
        # output completely. A malformed cached blob (a corrupted write, a
        # manual edit, anything) reached find_email_by_username's
        # user.get("UserName", ...) / user["UserId"] unguarded -- raising
        # AttributeError or KeyError, caught by neither ClientError nor
        # BotoCoreError below, surfacing as a 500 plus a Slack post on
        # every single request until the bad object was fixed or deleted
        # by hand. Not a full pydantic model like the other two caches
        # (users aren't cached through one at all, by design -- the raw
        # Identity Store dict shape is passed through as-is), just enough
        # structural validation to guarantee what find_email_by_username
        # and its callers actually dereference unconditionally exists.
        if not isinstance(users, list) or not all(isinstance(u, dict) and "UserId" in u for u in users):
            logger.warning("Cached users data has an unexpected shape -- treating as a cache miss")
            return None

        logger.info(f"Retrieved {len(users)} users from cache")
        return users

    except s3_client.exceptions.NoSuchKey:
        logger.info("Cache miss for users - no cached data found")
        return None
    except Exception as e:
        logger.warning(f"Failed to get cached users: {e}", extra={"error": str(e)})
        return None


def set_cached_users(
    s3_client: S3Client,
    cache_config: CacheConfig,
    identity_store_id: str,
    users: list[dict[str, Any]],
) -> None:
    """Store Identity Store users (raw dicts) in S3 cache.

    Args:
        s3_client: S3 client
        cache_config: Cache configuration
        identity_store_id: Identity Store ID (used as part of cache key)
        users: List of user dicts to cache
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        validated_bucket_name = _validate_bucket_name(cache_config.bucket_name)
        validated_identity_store_id = _validate_identity_store_id(identity_store_id)

        sanitized_data = _sanitize_json_data(users, max_size=MAX_USERS_DATA_SIZE)

        key = f"{CacheKey.USERS_PREFIX}{validated_identity_store_id}.json"

        s3_client.put_object(
            Bucket=validated_bucket_name,
            Key=key,
            Body=sanitized_data.encode("utf-8"),
            ContentType="application/json",
            **_encryption_kwargs(cache_config),
        )

        logger.info(f"Cached {len(users)} users")

    except ValueError as e:
        logger.warning(f"Validation failed when caching users: {e}", extra={"error": str(e)})
    except Exception as e:
        logger.warning(f"Failed to cache users: {e}", extra={"error": str(e)})


def _compute_data_hash(data: T) -> str:
    """Compute SHA256 hash of data for comparison.

    Args:
        data: Data to hash (list of Account or PermissionSet objects)

    Returns:
        Hex string of SHA256 hash
    """
    try:
        # Convert to JSON string with sorted keys for consistent hashing
        json_str = json.dumps([item.dict() if hasattr(item, "dict") else item for item in data], sort_keys=True)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()
    except Exception as e:
        logger.warning(f"Failed to compute hash: {e}")
        # Return a random value to force cache update on error
        return ""


def _compare_data(api_data: T, cached_data: T, compare_func: Optional[Callable[[T, T], bool]]) -> bool:
    """Compare API data with cached data using hash comparison.

    Args:
        api_data: Data from API
        cached_data: Data from cache
        compare_func: Optional custom comparison function

    Returns:
        True if data matches, False otherwise
    """
    if compare_func:
        return compare_func(api_data, cached_data)

    # Default comparison: compute and compare hashes
    try:
        api_hash = _compute_data_hash(api_data)
        cache_hash = _compute_data_hash(cached_data)
        return api_hash == cache_hash and api_hash != ""
    except Exception:
        # If comparison fails, assume data is different
        return False


def _update_cache_if_needed(
    api_data: T,
    cached_data: Optional[T],
    cache_setter: Callable[[T], None],
    resource_name: str,
    compare_func: Optional[Callable[[T, T], bool]],
) -> None:
    """Update cache if API data differs from cached data.

    Args:
        api_data: Data from API
        cached_data: Data from cache (or None)
        cache_setter: Function to update cache
        resource_name: Name of resource for logging
        compare_func: Optional custom comparison function
    """
    # An empty API result must not silently overwrite a real, non-empty
    # cache (#194 High #4, found by Andrey Devyatkin): the only guard here
    # used to be "is api_data not None", not a sanity check -- a genuinely
    # empty ListUsers/ListAccounts/ListPermissionSets response (a pagination
    # fluke, a transient AWS-side bug, anything) was treated as an
    # authoritative "there are now zero of these" and wrote right over a
    # good prior snapshot, destroying the exact fallback this cache exists
    # to provide. A live deployment actually using SSO Elevator always has
    # at least one real account/permission-set/user, so an empty result
    # while a non-empty cache already exists is far more likely to be a bug
    # on the API side than genuine ground truth -- skip the write and keep
    # the last known-good snapshot instead.
    if not api_data and cached_data:
        logger.warning(
            f"API returned an empty {resource_name} result while a non-empty cache already exists -- "
            "not overwriting the cache with what's more likely a transient bug than genuine ground truth"
        )
        return

    if cached_data is not None:
        data_matches = _compare_data(api_data, cached_data, compare_func)

        if not data_matches:
            logger.info(f"API data differs from cache for {resource_name}, updating cache")
            try:
                cache_setter(api_data)
            except Exception as e:
                logger.warning(f"Failed to update cache for {resource_name}: {e}")
        else:
            logger.debug(f"API data matches cache for {resource_name}, no update needed")
    else:
        # No cached data, store API result
        logger.info(f"No cached data for {resource_name}, storing API result")
        try:
            cache_setter(api_data)
        except Exception as e:
            logger.warning(f"Failed to cache {resource_name}: {e}")


def with_cache_resilience(
    cache_getter: Callable[[], Optional[T]],
    api_getter: Callable[[], T],
    cache_setter: Callable[[T], None],
    resource_name: str,
    compare_func: Optional[Callable[[T, T], bool]] = None,
) -> T:
    """Get data with cache resilience using parallel API and cache calls.

    This function calls both the API and cache in parallel. If the API call succeeds,
    it compares the result with cached data and updates the cache if different.
    If the API call fails, it falls back to cached data.

    Args:
        cache_getter: Function to get data from cache
        api_getter: Function to get data from API
        cache_setter: Function to set data in cache
        resource_name: Name of the resource for logging
        compare_func: Optional function to compare API and cached data (returns True if equal)

    Returns:
        The data from API (if successful) or cache (if API fails)

    Raises:
        Exception: If both API and cache fail
    """
    import concurrent.futures

    cached_data = None
    api_data = None
    api_error = None

    # Execute API and cache calls in parallel. Not a `with` block: ThreadPoolExecutor.__exit__
    # calls shutdown(wait=True) unconditionally, which would block this
    # function until the cache thread actually finishes regardless of the
    # timeout below -- exactly the "a stalled S3 read blocks a request whose
    # live call already succeeded" bug (#194 High #4, found live by Andrey
    # Devyatkin) the timeout exists to prevent. shutdown(wait=False) below
    # lets this function return as soon as it has an answer; the abandoned
    # cache thread runs to completion (or its own boto3-level timeout) in
    # the background and is then discarded -- a bounded, self-resolving cost
    # against every such call otherwise blocking for the same duration.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    cache_future = executor.submit(cache_getter)
    api_future = executor.submit(api_getter)

    # Get cache result -- bounded by CACHE_LOOKUP_TIMEOUT_SECONDS, not
    # unbounded: a concurrent.futures.TimeoutError here is just another
    # cache-lookup failure to the except Exception below (it's a plain
    # Exception subclass), so the API result -- likely already sitting
    # ready on api_future by now -- is still returned normally.
    try:
        cached_data = cache_future.result(timeout=CACHE_LOOKUP_TIMEOUT_SECONDS)
        if cached_data is not None:
            logger.debug(f"Cache data available for {resource_name}")
    except Exception as e:
        logger.warning(f"Cache lookup failed for {resource_name}: {e}")

    # Get API result
    try:
        api_data = api_future.result()
        logger.info(f"Successfully fetched {resource_name} from API")
    except Exception as e:
        api_error = e
        logger.warning(f"API call failed for {resource_name}: {e}")

    executor.shutdown(wait=False)

    # If API succeeded, update cache if needed and return API data
    if api_data is not None:
        _update_cache_if_needed(api_data, cached_data, cache_setter, resource_name, compare_func)
        return api_data

    # If API failed, fall back to cache
    if cached_data is not None:
        logger.warning(f"API failed for {resource_name}, using cached data as fallback")
        return cached_data

    # Both API and cache failed
    logger.error(f"Both API and cache failed for {resource_name}")
    raise api_error or Exception(f"Failed to retrieve {resource_name} from both API and cache")
