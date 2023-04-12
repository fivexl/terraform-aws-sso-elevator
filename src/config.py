from typing import Literal, Optional, Union

from pydantic import BaseSettings, Field, BaseModel, root_validator


class SlackConfig(BaseSettings):
    bot_token: str = Field(..., env="SLACK_BOT_TOKEN", min_length=1)
    signing_secret: str = Field(..., env="SLACK_SIGNING_SECRET", min_length=1)
    channel_id: str = Field(..., env="SLACK_CHANNEL_ID", min_length=1)

class Statement(BaseModel):
    resource_type: Literal["Account", "OU"]
    resource: frozenset[Union[str, Literal["*"]]]
    permission_set: frozenset[Union[str, Literal["*"]]]
    approvers: Optional[frozenset[str]]
    approval_is_not_required: bool = False
    allow_self_approval: bool = False

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        def to_set_if_list_or_str(v):
            if isinstance(v, list):
                return frozenset(v)
            return frozenset([v]) if isinstance(v, str) else v

        return {
            "permission_set": to_set_if_list_or_str(values["PermissionSet"]),
            "resource": to_set_if_list_or_str(values["Resource"]),
            "approvers": to_set_if_list_or_str(values.get("Approvers", set())),
            "resource_type": values.get("ResourceType"),
            "approval_is_not_required": values.get("ApprovalIsNotRequired", False),
            "allow_self_approval": values.get("AllowSelfApproval", False),
        }

    def allows(self, account_id: str, permission_set_name: str) -> bool:
        account_match = account_id in self.resource or "*" in self.resource
        permission_set_match = permission_set_name in self.permission_set or "*" in self.permission_set
        return account_match and permission_set_match


class Config(BaseSettings):
    post_update_to_slack: bool = False

    dynamodb_table_name: str
    sso_instance_arn: str

    log_level: str = "INFO"
    statements: frozenset[Statement]

    accounts: frozenset[str]
    permission_sets: frozenset[str]

    class Config:
        frozen = True

    @root_validator(pre=True)
    def get_accounts_and_permission_sets(cls, values: dict):
        statements = {Statement.parse_obj(st) for st in values["statements"]}  # type: ignore
        permission_sets = set()
        accounts = set()
        for statement in statements:
            permission_sets.update(statement.permission_set)
            if statement.resource_type == "Account":
                accounts.update(statement.resource)
        return values | {"accounts": accounts, "permission_sets": permission_sets}
