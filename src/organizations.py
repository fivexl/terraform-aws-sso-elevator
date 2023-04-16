from entities.aws import Account


def parse_account(td: dict) -> Account:
    return Account.parse_obj({"id": td.get("Id"), "name": td.get("Name")})


def list_accounts(organizations_client) -> list[Account]:
    accounts = organizations_client.list_accounts()["Accounts"]
    return [parse_account(account) for account in accounts]


def describe_account(organizations_client, account_id: str) -> Account:
    account = organizations_client.describe_account(AccountId=account_id)["Account"]
    return parse_account(account)
