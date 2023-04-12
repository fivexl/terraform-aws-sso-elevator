from dataclasses import dataclass


@dataclass
class AWSAccount:
    name: str
    id: str
    organization_id: str

    @staticmethod
    def from_type_def(td: dict) -> "AWSAccount":
        return AWSAccount(
            name=td["Name"],  # type: ignore
            id=td["Id"],  # type: ignore
            organization_id=td["Arn"].split("/")[1],  # type: ignore
        )


def list_accounts(organizations_client) -> list[AWSAccount]:
    accounts = organizations_client.list_accounts()["Accounts"]
    return [AWSAccount.from_type_def(account) for account in accounts]


def describe_account(organizations_client, account_id: str) -> AWSAccount:
    account = organizations_client.describe_account(AccountId=account_id)["Account"]
    return AWSAccount.from_type_def(account)
