from dataclasses import dataclass
from typing import Literal, Optional, Union

from mypy_boto3_organizations import OrganizationsClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import BaseSettings, Field

import organizations
import sso


@dataclass
class RequestForAccess:
    permission_set: sso.PermissionSet
    account: organizations.AWSAccount
    requester_email: str


class SlackConfig(BaseSettings):
    bot_token: str = Field(..., env="SLACK_BOT_TOKEN", min_length=1)
    signing_secret: str = Field(..., env="SLACK_SIGNING_SECRET", min_length=1)
    channel_id: str = Field(..., env="SLACK_CHANNEL_ID", min_length=1)


class Config(BaseSettings):
    post_update_to_slack: bool = False

    dynamodb_table_name: str
    sso_instance_arn: str

    log_level: str = "INFO"
    config: list[dict]

    def get_statements(self) -> list["Statement"]:
        return [Statement.from_dict(d) for d in self.config]


@dataclass
class Statement:
    resource_type: Literal["Account", "OU"]
    resource: list[Union[str, Literal["*"]]]
    permission_set: list[Union[str, Literal["*"]]]
    approvers: Optional[list[str]]
    approval_is_not_required: bool = False
    allow_self_approval: bool = False

    @staticmethod
    def from_dict(d: dict) -> "Statement":
        permission_set = d["PermissionSet"]
        resource = d["Resource"]
        approvers = d.get("Approvers") or []
        return Statement(
            resource_type=d["ResourceType"],
            resource=resource if isinstance(resource, list) else [resource],
            permission_set=permission_set if isinstance(permission_set, list) else [permission_set],
            approvers=approvers if isinstance(approvers, list) else [approvers],
            approval_is_not_required=d.get("ApprovalIsNotRequired", False),
            allow_self_approval=d.get("AllowSelfApproval", False),
        )

    # TODO type hint
    def allows(self, account_id: str, permission_set_name: str) -> bool:
        account_match = account_id in self.resource or "*" in self.resource
        permission_set_match = permission_set_name in self.permission_set or "*" in self.permission_set
        return account_match and permission_set_match


# TODO: there is no logs if account is not found
def get_accounts_from_statements(statements: list[Statement], org_client: OrganizationsClient) -> list[organizations.AWSAccount]:
    all_accounts = organizations.list_accounts(org_client)
    avialable_accounts = []
    for statement in statements:
        if statement.resource_type == "Account":
            if "*" in statement.resource:
                return all_accounts
            for resource in statement.resource:
                avialable_accounts.extend(
                    account for account in all_accounts if resource == account.id and account not in avialable_accounts
                )
    return avialable_accounts


# TODO: there is no error if ps is not found, type hint is not correct
def get_permission_sets_from_statements(
    statements: list[Statement], sso_client: SSOAdminClient, sso_instance_arn: str
) -> list[sso.PermissionSet]:
    all_permission_sets = list(sso.list_permission_sets(sso_client, sso_instance_arn))
    avialable_permission_sets = []
    for statement in statements:
        if "*" in statement.permission_set:
            return all_permission_sets  # TODO: check if it is correct  # type: ignore
        for permission_set in statement.permission_set:
            if permission_set not in avialable_permission_sets:
                avialable_permission_sets.extend(
                    ps for ps in all_permission_sets if permission_set == ps.name and ps not in avialable_permission_sets
                )
    return avialable_permission_sets
