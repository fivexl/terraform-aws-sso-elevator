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
        response = dynamodb_client.query(
            TableName=cache_config.table_name,
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
            all_accounts.extend([Account.parse_obj(acc) for acc in accounts_data])

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
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        ttl = _get_ttl_timestamp(cache_config.ttl_minutes)
        accounts_data = [account.dict() for account in accounts]

        # Store all accounts in a single item
        dynamodb_client.put_item(
            TableName=cache_config.table_name,
            Item={
                "cache_key": {"S": CacheKey.ACCOUNTS},
                "item_id": {"S": "all"},
                "data": {"S": json.dumps(accounts_data)},
                "ttl": {"N": str(ttl)},
                "cached_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )

        logger.info(f"Cached {len(accounts)} accounts with TTL {cache_config.ttl_minutes} minutes")

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
        # Validate ARN to prevent injection attacks
        validated_arn = _validate_arn(sso_instance_arn)

        response = dynamodb_client.query(
            TableName=cache_config.table_name,
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
        permission_sets = [PermissionSet.parse_obj(ps) for ps in permission_sets_data]

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
    """
    if not cache_config.enabled:
        logger.debug("Cache is disabled, skipping cache write")
        return

    try:
        # Validate ARN to prevent injection attacks
        validated_arn = _validate_arn(sso_instance_arn)

        ttl = _get_ttl_timestamp(cache_config.ttl_minutes)
        permission_sets_data = [ps.dict() for ps in permission_sets]

        dynamodb_client.put_item(
            TableName=cache_config.table_name,
            Item={
                "cache_key": {"S": CacheKey.PERMISSION_SETS},
                "item_id": {"S": validated_arn},
                "data": {"S": json.dumps(permission_sets_data)},
                "ttl": {"N": str(ttl)},
                "cached_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )

        logger.info(f"Cached {len(permission_sets)} permission sets with TTL {cache_config.ttl_minutes} minutes")

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
