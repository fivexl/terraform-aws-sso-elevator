from mypy_boto3_organizations import OrganizationsClient, type_defs
from mypy_boto3_s3 import S3Client

import cache as cache_module
import config
from entities.aws import Account

logger = config.get_logger(service="organizations")


def parse_account(td: type_defs.AccountTypeDef) -> Account:
    return Account.model_validate({"id": td.get("Id"), "name": td.get("Name")})


def is_management_account(account_id: str, management_account_id: str | None) -> bool:
    """True if ``account_id`` is the org management account (matches API ``MasterAccountId``)."""
    if not management_account_id or not account_id:
        return False
    return account_id.strip() == management_account_id.strip()


def get_management_account_id(client: OrganizationsClient) -> str | None:
    """Return the management account ID via ``DescribeOrganization`` (``MasterAccountId`` field)."""
    try:
        resp = client.describe_organization()
        mid = (resp.get("Organization") or {}).get("MasterAccountId")
        if mid and isinstance(mid, str) and mid.strip():
            return mid.strip()
    except Exception as e:
        logger.exception(f"Failed to describe organization: {e}")
    return None


def list_accounts(client: OrganizationsClient) -> list[Account]:
    accounts = []
    paginator = client.get_paginator("list_accounts")
    for page in paginator.paginate():
        accounts.extend(page["Accounts"])
    return [parse_account(account) for account in accounts]


def list_accounts_with_cache(
    org_client: OrganizationsClient,
    s3_client: S3Client,
    cfg: config.Config,
) -> list[Account]:
    """List all accounts with cache resilience.

    This function calls both the Organizations API and S3 cache in parallel.
    If the API call succeeds, it compares with cached data and updates if different.
    If the API call fails, it falls back to cached data.

    Args:
        org_client: Organizations client
        s3_client: S3 client for cache
        cfg: Application configuration

    Returns:
        List of all accounts
    """
    cache_config = cache_module.CacheConfig.from_config(cfg)

    return cache_module.with_cache_resilience(
        cache_getter=lambda: cache_module.get_cached_accounts(s3_client, cache_config),
        api_getter=lambda: list_accounts(org_client),
        cache_setter=lambda accounts: cache_module.set_cached_accounts(s3_client, cache_config, accounts),
        resource_name="accounts",
    )


def describe_account(client: OrganizationsClient, account_id: str) -> Account:
    account = client.describe_account(AccountId=account_id)["Account"]
    return parse_account(account)


def get_accounts_from_config(client: OrganizationsClient, cfg: config.Config) -> list[Account]:
    if "*" in cfg.accounts:
        accounts = list_accounts(client)
    else:
        accounts = [ac for ac in list_accounts(client) if ac.id in cfg.accounts]
    return accounts


def get_accounts_from_config_with_cache(
    org_client: OrganizationsClient,
    s3_client: S3Client,
    cfg: config.Config,
) -> list[Account]:
    """Get accounts from config with cache resilience.

    This function calls both the Organizations API and S3 cache in parallel.
    If the API call succeeds, it compares with cached data and updates if different.
    If the API call fails, it falls back to cached data.

    Args:
        org_client: Organizations client
        s3_client: S3 client for cache
        cfg: Application configuration

    Returns:
        List of accounts based on config
    """
    all_accounts = list_accounts_with_cache(org_client, s3_client, cfg)

    if "*" in cfg.accounts:
        return all_accounts
    else:
        return [ac for ac in all_accounts if ac.id in cfg.accounts]
