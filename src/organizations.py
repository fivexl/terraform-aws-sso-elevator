from mypy_boto3_organizations import OrganizationsClient, type_defs

import config
from entities.aws import Account


def parse_account(td: type_defs.AccountTypeDef) -> Account:
    return Account.parse_obj({"id": td.get("Id"), "name": td.get("Name")})


def list_accounts(client: OrganizationsClient) -> list[Account]:
    accounts = []
    paginator = client.get_paginator("list_accounts")
    for page in paginator.paginate():
        accounts.extend(page["Accounts"])
    return [parse_account(account) for account in accounts]


def describe_account(client: OrganizationsClient, account_id: str) -> Account:
    account = client.describe_account(AccountId=account_id)["Account"]
    return parse_account(account)


def get_accounts_from_config(client: OrganizationsClient, cfg: config.Config) -> list[Account]:
    if "*" in cfg.accounts:
        accounts = list_accounts(client)
    else:
        accounts = [ac for ac in list_accounts(client) if ac.id in cfg.accounts]
    return accounts
