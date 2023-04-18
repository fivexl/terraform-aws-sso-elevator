from mypy_boto3_organizations import OrganizationsClient, type_defs

from entities.aws import Account


def parse_account(td: type_defs.AccountTypeDef) -> Account:
    return Account.parse_obj({"id": td.get("Id"), "name": td.get("Name")})


def list_accounts(client: OrganizationsClient) -> list[Account]:
    accounts = client.list_accounts()["Accounts"]
    return [parse_account(account) for account in accounts]


def describe_account(client: OrganizationsClient, account_id: str) -> Account:
    account = client.describe_account(AccountId=account_id)["Account"]
    return parse_account(account)
