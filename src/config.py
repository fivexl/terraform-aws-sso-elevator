from pydantic import BaseSettings, root_validator

from access_control import Statement


def parse_statement(_dict: dict) -> Statement:
    def to_set_if_list_or_str(v):
        if isinstance(v, list):
            return frozenset(v)
        return frozenset([v]) if isinstance(v, str) else v

    return Statement.parse_obj(
        {
            "permission_set": to_set_if_list_or_str(_dict["PermissionSet"]),
            "resource": to_set_if_list_or_str(_dict["Resource"]),
            "approvers": to_set_if_list_or_str(_dict.get("Approvers", set())),
            "resource_type": _dict.get("ResourceType"),
            "approval_is_not_required": _dict.get("ApprovalIsNotRequired", False),
            "allow_self_approval": _dict.get("AllowSelfApproval", False),
        }
    )


class Config(BaseSettings):
    schedule_policy_arn: str
    revoker_function_arn: str
    revoker_function_name: str

    post_update_to_slack: bool = False
    slack_channel_id: str
    slack_bot_token: str

    dynamodb_table_name: str
    sso_instance_arn: str

    log_level: str = "INFO"
    slack_app_log_level: str = "INFO"
    statements: frozenset[Statement]

    accounts: frozenset[str]
    permission_sets: frozenset[str]

    class Config:
        frozen = True

    @root_validator(pre=True)
    def get_accounts_and_permission_sets(cls, values: dict):
        statements = {parse_statement(st) for st in values["statements"]}  # type: ignore
        permission_sets = set()
        accounts = set()
        for statement in statements:
            permission_sets.update(statement.permission_set)
            if statement.resource_type == "Account":
                accounts.update(statement.resource)
        return values | {"accounts": accounts, "permission_sets": permission_sets, "statements": frozenset(statements)}
