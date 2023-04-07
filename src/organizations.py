from dataclasses import dataclass

from mypy_boto3_organizations import OrganizationsClient, type_defs


@dataclass
class AWSAccount:
    """AWS Account

    Attributes:
        name (str): Name of the AWS Account
        id (str): ID of the AWS Account
        organization_unit_id (str): ID of the OU the AWS Account is in
    """

    name: str
    id: str
    organization_id: str

    @staticmethod
    def from_type_def(td: type_defs.AccountTypeDef) -> "AWSAccount":
        return AWSAccount(
            name=td["Name"],  # type: ignore
            id=td["Id"],  # type: ignore
            organization_id=td["Arn"].split("/")[1],  # type: ignore
        )


def list_accounts(client: OrganizationsClient) -> list[AWSAccount]:
    accounts = client.list_accounts()["Accounts"]
    return [AWSAccount.from_type_def(account) for account in accounts]


def describe_account(client: OrganizationsClient, account_id: str) -> AWSAccount:
    account = client.describe_account(AccountId=account_id)["Account"]
    return AWSAccount.from_type_def(account)
