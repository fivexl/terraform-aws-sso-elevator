"""Cache module for caching AWS accounts and permission sets in DynamoDB."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

import config
from entities.aws import Account, PermissionSet

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

logger = config.get_logger(service="cache")

T = TypeVar("T")

# Regex pattern for validating AWS ARN format
# SSO instance ARN format: arn:{partition}:sso:::instance/{instance-id}
# Partition format: "aws" optionally followed by hyphen-separated segments (e.g., aws-us-gov, aws-cn, aws-iso-b)
# This pattern is future-proof for new AWS partitions while preventing injection attacks
# instance-id is alphanumeric, underscores, and hyphens
# Limited to max 5 partition segments to prevent catastrophic backtracking while being future-proof
ARN_PATTERN = re.compile(r"^arn:aws(?:-[a-z0-9]+){0,5}:sso:::\w+/[\w-]+$")

# Maximum ARN length to prevent excessively long input
MAX_ARN_LENGTH = 1024

# TTL validation constants (in minutes)
MIN_TTL_MINUTES = 1
MAX_TTL_MINUTES = 525600  # 1 year in minutes

# Maximum size for serialized data to prevent excessively large payloads (in bytes)
MAX_DATA_SIZE = 400 * 1024  # DynamoDB item size limit is 400KB

# Pattern for validating DynamoDB table names
# DynamoDB table names must be 3-255 characters, contain only alphanumeric, underscore, hyphen, and period
TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,255}$")


@dataclass
class CacheConfig:
    """Configuration for the cache."""

    table_name: str
    ttl_minutes: int
    enabled: bool

    @staticmethod
    def from_config(cfg: config.Config) -> "CacheConfig":
        """Create a CacheConfig from the application config."""
        return CacheConfig(
            table_name=cfg.cache_table_name,
            ttl_minutes=cfg.cache_ttl_minutes,
            enabled=cfg.cache_ttl_minutes > 0,
        )


class CacheKey:
    """Constants for cache keys."""

    ACCOUNTS = "accounts"
    PERMISSION_SETS = "permission_sets"


def _get_ttl_timestamp(ttl_minutes: int) -> int:
    """Calculate the TTL timestamp for DynamoDB.

    Args:
        ttl_minutes: TTL in minutes

    Returns:
        Unix timestamp when the item should expire

    Raises:
        ValueError: If ttl_minutes is invalid
    """
    # Validate and sanitize input to prevent NoSQL injection
    try:
        validated_ttl_minutes = int(ttl_minutes)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid TTL minutes value: {ttl_minutes}") from e

    # Ensure ttl_minutes is within reasonable bounds
    # Minimum: 1 minute, Maximum: 1 year (525,600 minutes)
    if validated_ttl_minutes < MIN_TTL_MINUTES or validated_ttl_minutes > MAX_TTL_MINUTES:
        raise ValueError(f"TTL minutes must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES}, got: {validated_ttl_minutes}")

    current_time = int(time.time())
    ttl_timestamp = current_time + (validated_ttl_minutes * 60)

    # Validate the calculated timestamp is reasonable
    if ttl_timestamp <= current_time:
        raise ValueError("Calculated TTL timestamp is not in the future")

    return ttl_timestamp


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

    # Check against ARN pattern
    if not ARN_PATTERN.match(arn):
        raise ValueError(f"Invalid SSO instance ARN format: {arn}")

    # Additional length check to prevent excessively long input
    if len(arn) > MAX_ARN_LENGTH:
        raise ValueError("ARN exceeds maximum length")

    return arn


def _validate_table_name(table_name: str) -> str:
    """Validate DynamoDB table name to prevent injection attacks.

    Args:
        table_name: Table name to validate

    Returns:
        Validated table name

    Raises:
        ValueError: If the table name format is invalid
    """
    if not isinstance(table_name, str):
        raise ValueError(f"Table name must be a string, got {type(table_name)}")

    # Check against table name pattern
    if not TABLE_NAME_PATTERN.match(table_name):
        raise ValueError(f"Invalid DynamoDB table name format: {table_name}")

    return table_name


def _sanitize_json_data(data: list[dict[str, Any]]) -> str:
    """Sanitize and validate data before storing in DynamoDB.

    This function ensures that the data being stored is safe and doesn't
    contain any potentially malicious content.

    Args:
        data: List of dictionaries to serialize

    Returns:
        JSON string of the sanitized data

    Raises:
        ValueError: If the data is invalid or too large
    """
    if not isinstance(data, list):
        raise ValueError(f"Data must be a list, got {type(data)}")

    # Serialize to JSON to ensure data is JSON-serializable
    # This also provides a layer of sanitization as json.dumps will only
    # serialize safe data types (str, int, float, bool, None, list, dict)
    try:
        serialized = json.dumps(data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Data contains non-serializable content: {e}") from e

    # Validate size to prevent excessively large payloads
    data_size = len(serialized.encode("utf-8"))
    if data_size > MAX_DATA_SIZE:
        raise ValueError(f"Data size ({data_size} bytes) exceeds maximum allowed size ({MAX_DATA_SIZE} bytes)")

    return serialized


def _is_cache_valid(item: dict[str, Any]) -> bool:
    """Check if a cached item is still valid.

    Args:
        item: DynamoDB item

    Returns:
        True if the cache is valid, False otherwise
    """
    if "ttl" not in item:
        return False

    # Handle DynamoDB type descriptor format
    ttl_value = item["ttl"]
    if isinstance(ttl_value, dict) and "N" in ttl_value:
        ttl = int(ttl_value["N"])
    else:
        ttl = int(ttl_value)

    current_time = int(time.time())
    return current_time < ttl


def get_cached_accounts(
    dynamodb_client: DynamoDBClient,
    cache_config: CacheConfig,
) -> Optional[list[Account]]:
    """Get cached accounts from DynamoDB.

    Args:
        dynamodb_client: DynamoDB client
        cache_config: Cache configuration

    Returns:
        List of cached accounts or None if cache miss or invalid
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache lookup")
        return None

    try:
        # Validate table name to prevent injection attacks
        validated_table_name = _validate_table_name(cache_config.table_name)

        response = dynamodb_client.query(
            TableName=validated_table_name,
            KeyConditionExpression="cache_key = :cache_key",
            ExpressionAttributeValues={
                ":cache_key": {"S": CacheKey.ACCOUNTS},
            },
        )

        items = response.get("Items", [])
        if not items:
            logger.info("Cache miss for accounts")
            return None

        # Combine all account items (there might be multiple items due to pagination)
        all_accounts = []
        for item in items:
            if not _is_cache_valid(item):
                logger.info("Cache expired for accounts")
                return None

            accounts_data = json.loads(item["data"]["S"])
            all_accounts.extend([Account.model_validate(acc) for acc in accounts_data])

        logger.info(f"Cache hit for accounts, found {len(all_accounts)} accounts")
        return all_accounts

    except Exception as e:
        logger.warning(f"Failed to get cached accounts: {e}", extra={"error": str(e)})
        return None


def set_cached_accounts(
    dynamodb_client: DynamoDBClient,
    cache_config: CacheConfig,
    accounts: list[Account],
) -> None:
    """Store accounts in DynamoDB cache.

    Args:
        dynamodb_client: DynamoDB client
        cache_config: Cache configuration
        accounts: List of accounts to cache

    Note:
        Validation errors are logged but do not raise exceptions to maintain
        graceful degradation when caching fails.
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        # Validate table name to prevent injection attacks
        validated_table_name = _validate_table_name(cache_config.table_name)

        # Validate and sanitize TTL
        ttl = _get_ttl_timestamp(cache_config.ttl_minutes)

        # Convert accounts to dictionaries and sanitize the data
        accounts_data = [account.dict() for account in accounts]
        sanitized_data = _sanitize_json_data(accounts_data)

        # Store all accounts in a single item with validated and sanitized inputs
        dynamodb_client.put_item(
            TableName=validated_table_name,
            Item={
                "cache_key": {"S": CacheKey.ACCOUNTS},
                "item_id": {"S": "all"},
                "data": {"S": sanitized_data},
                "ttl": {"N": str(ttl)},
                "cached_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )

        logger.info(f"Cached {len(accounts)} accounts with TTL {cache_config.ttl_minutes} minutes")

    except ValueError as e:
        # Log validation errors but don't raise to maintain graceful degradation
        logger.warning(f"Validation failed when caching accounts: {e}", extra={"error": str(e)})
    except Exception as e:
        logger.warning(f"Failed to cache accounts: {e}", extra={"error": str(e)})


def get_cached_permission_sets(
    dynamodb_client: DynamoDBClient,
    cache_config: CacheConfig,
    sso_instance_arn: str,
) -> Optional[list[PermissionSet]]:
    """Get cached permission sets from DynamoDB.

    Args:
        dynamodb_client: DynamoDB client
        cache_config: Cache configuration
        sso_instance_arn: SSO instance ARN (used as part of cache key)

    Returns:
        List of cached permission sets or None if cache miss or invalid
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache lookup")
        return None

    try:
        # Validate table name to prevent injection attacks
        validated_table_name = _validate_table_name(cache_config.table_name)

        # Validate ARN to prevent injection attacks
        validated_arn = _validate_arn(sso_instance_arn)

        response = dynamodb_client.query(
            TableName=validated_table_name,
            KeyConditionExpression="cache_key = :cache_key AND item_id = :item_id",
            ExpressionAttributeValues={
                ":cache_key": {"S": CacheKey.PERMISSION_SETS},
                ":item_id": {"S": validated_arn},
            },
        )

        items = response.get("Items", [])
        if not items:
            logger.info("Cache miss for permission sets")
            return None

        item = items[0]
        if not _is_cache_valid(item):
            logger.info("Cache expired for permission sets")
            return None

        permission_sets_data = json.loads(item["data"]["S"])
        permission_sets = [PermissionSet.model_validate(ps) for ps in permission_sets_data]

        logger.info(f"Cache hit for permission sets, found {len(permission_sets)} permission sets")
        return permission_sets

    except Exception as e:
        logger.warning(f"Failed to get cached permission sets: {e}", extra={"error": str(e)})
        return None


def set_cached_permission_sets(
    dynamodb_client: DynamoDBClient,
    cache_config: CacheConfig,
    sso_instance_arn: str,
    permission_sets: list[PermissionSet],
) -> None:
    """Store permission sets in DynamoDB cache.

    Args:
        dynamodb_client: DynamoDB client
        cache_config: Cache configuration
        sso_instance_arn: SSO instance ARN (used as part of cache key)
        permission_sets: List of permission sets to cache

    Note:
        Validation errors are logged but do not raise exceptions to maintain
        graceful degradation when caching fails.
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        # Validate table name to prevent injection attacks
        validated_table_name = _validate_table_name(cache_config.table_name)

        # Validate ARN to prevent injection attacks
        validated_arn = _validate_arn(sso_instance_arn)

        # Validate and sanitize TTL
        ttl = _get_ttl_timestamp(cache_config.ttl_minutes)

        # Convert permission sets to dictionaries and sanitize the data
        permission_sets_data = [ps.dict() for ps in permission_sets]
        sanitized_data = _sanitize_json_data(permission_sets_data)

        # Store permission sets with validated and sanitized inputs
        dynamodb_client.put_item(
            TableName=validated_table_name,
            Item={
                "cache_key": {"S": CacheKey.PERMISSION_SETS},
                "item_id": {"S": validated_arn},
                "data": {"S": sanitized_data},
                "ttl": {"N": str(ttl)},
                "cached_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )

        logger.info(f"Cached {len(permission_sets)} permission sets with TTL {cache_config.ttl_minutes} minutes")

    except ValueError as e:
        # Log validation errors but don't raise to maintain graceful degradation
        logger.warning(f"Validation failed when caching permission sets: {e}", extra={"error": str(e)})
    except Exception as e:
        logger.warning(f"Failed to cache permission sets: {e}", extra={"error": str(e)})


def with_cache_fallback(
    cache_getter: Callable[[], Optional[T]],
    api_getter: Callable[[], T],
    cache_setter: Callable[[T], None],
    resource_name: str,
) -> T:
    """Generic function to get data with cache fallback.

    This function attempts to get data from cache first. If cache is unavailable
    or returns None, it falls back to the API call. After a successful API call,
    it attempts to update the cache.

    Args:
        cache_getter: Function to get data from cache
        api_getter: Function to get data from API
        cache_setter: Function to set data in cache
        resource_name: Name of the resource for logging

    Returns:
        The data from cache or API
    """
    # Try cache first
    try:
        cached_data = cache_getter()
        if cached_data is not None:
            logger.info(f"Using cached {resource_name}")
            return cached_data
    except Exception as e:
        logger.warning(f"Cache lookup failed for {resource_name}, falling back to API: {e}")

    # Fallback to API
    logger.info(f"Fetching {resource_name} from API")
    api_data = api_getter()

    # Try to update cache
    try:
        cache_setter(api_data)
    except Exception as e:
        logger.warning(f"Failed to update cache for {resource_name}: {e}")

    return api_data
