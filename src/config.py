import os
from typing import Optional

from aws_lambda_powertools import Logger
from pydantic import BaseSettings, root_validator

import entities
from statement import Statement


def parse_statement(_dict: dict) -> Statement:
    def to_set_if_list_or_str(v: list | str) -> frozenset[str]:
        if isinstance(v, list):
            return frozenset(v)
        return frozenset([v]) if isinstance(v, str) else v

    return Statement.parse_obj(
        {
            "permission_set": to_set_if_list_or_str(_dict["PermissionSet"]),
            "resource": to_set_if_list_or_str(_dict["Resource"]),
            "approvers": to_set_if_list_or_str(_dict.get("Approvers", set())),
            "resource_type": _dict.get("ResourceType"),
            "approval_is_not_required": _dict.get("ApprovalIsNotRequired", None),
            "allow_self_approval": _dict.get("AllowSelfApproval", None),
        }
    )


class Config(BaseSettings):
    schedule_policy_arn: str
    revoker_function_arn: str
    revoker_function_name: str
    schedule_group_name: str

    post_update_to_slack: bool = False
    slack_channel_id: str
    slack_bot_token: str

    approver_renotification_initial_wait_time: int
    approver_renotification_backoff_multiplier: int

    sso_instance_arn: str

    log_level: str = "INFO"
    slack_app_log_level: str = "INFO"
    statements: frozenset[Statement]

    accounts: frozenset[str]
    permission_sets: frozenset[str]

    s3_bucket_for_audit_entry_name: str
    s3_bucket_prefix_for_partitions: str

    sso_elevator_scheduled_revocation_rule_name: str
    request_expiration_hours: int = 8

    max_permissions_duration_time: int

    class Config:
        frozen = True

    @root_validator(pre=True)
    def get_accounts_and_permission_sets(cls, values: dict) -> dict:  # noqa: ANN101
        statements = {parse_statement(st) for st in values.get("statements", [])}  # type: ignore # noqa: PGH003
        permission_sets = set()
        accounts = set()
        for statement in statements:
            permission_sets.update(statement.permission_set)
            if statement.resource_type == "Account":
                accounts.update(statement.resource)
        return values | {
            "accounts": accounts,
            "permission_sets": permission_sets,
            "statements": frozenset(statements),
        }


def get_logger(service: Optional[str] = None, level: Optional[str] = None) -> Logger:
    kwargs = {
        "json_default": entities.json_default,
        "level": level or os.environ.get("LOG_LEVEL", "INFO"),
    }
    if service:
        kwargs["service"] = service
    return Logger(**kwargs)


_config: Optional[Config] = None


def get_config() -> Config:
    global _config  # noqa: PLW0603
    if _config is None:
        _config = Config()  # type: ignore # noqa: PGH003
    return _config
