from typing import Literal, Optional, Union

from pydantic import BaseSettings, Field, BaseModel, root_validator


class SlackConfig(BaseSettings):
    bot_token: str = Field(..., env="SLACK_BOT_TOKEN", min_length=1)
    signing_secret: str = Field(..., env="SLACK_SIGNING_SECRET", min_length=1)
    channel_id: str = Field(..., env="SLACK_CHANNEL_ID", min_length=1)


class Statement(BaseModel):
    resource_type: Literal["Account", "OU"]
    resource: list[Union[str, Literal["*"]]]
    permission_set: list[Union[str, Literal["*"]]]
    approvers: Optional[list[str]]
    approval_is_not_required: bool = False
    allow_self_approval: bool = False

    class Config:
        frozen = True

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        permission_set = values.get("PermissionSet")
        resource = values.get("Resource")
        approvers = values.get("Approvers") or []
        return {
            "resource_type": values.get("ResourceType"),
            "resource": resource if isinstance(resource, list) else [resource],
            "permission_set": permission_set if isinstance(permission_set, list) else [permission_set],
            "approvers": approvers if isinstance(approvers, list) else [approvers],
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
    statements: list[Statement]

    def get_configured_accounts(self) -> set[str]:
        available_accounts = set()
        for statement in self.statements:
            if statement.resource_type == "Account":
                available_accounts.update(statement.resource)
        return available_accounts

    def get_configured_permission_sets(self) -> set[str]:
        available_permission_sets = set()
        for statement in self.statements:
            available_permission_sets.update(statement.permission_set)
        return available_permission_sets
