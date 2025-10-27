import os
from typing import Optional

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities import parameters
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json
import entities
from statement import Statement, GroupStatement


def get_logger(service: Optional[str] = None, level: Optional[str] = None) -> Logger:
    kwargs = {
        "json_default": entities.json_default,
        "level": level or os.environ.get("LOG_LEVEL", "INFO"),
    }
    if service:
        kwargs["service"] = service
    return Logger(**kwargs)


logger = get_logger(service="config")


def parse_statement(_dict: dict) -> Statement:
    def to_set_if_list_or_str(v: list | str) -> frozenset[str]:
        if isinstance(v, list):
            return frozenset(v)
        return frozenset([v]) if isinstance(v, str) else v

    return Statement.model_validate(
        {
            "permission_set": to_set_if_list_or_str(_dict["PermissionSet"]),
            "resource": to_set_if_list_or_str(_dict["Resource"]),
            "approvers": to_set_if_list_or_str(_dict.get("Approvers", set())),
            "resource_type": _dict.get("ResourceType"),
            "approval_is_not_required": _dict.get("ApprovalIsNotRequired"),
            "allow_self_approval": _dict.get("AllowSelfApproval"),
        }
    )


def parse_group_statement(_dict: dict) -> GroupStatement:
    def to_set_if_list_or_str(v: list | str) -> frozenset[str]:
        if isinstance(v, list):
            return frozenset(v)
        return frozenset([v]) if isinstance(v, str) else v

    return GroupStatement.model_validate(
        {
            "resource": to_set_if_list_or_str(_dict["Resource"]),
            "approvers": to_set_if_list_or_str(_dict.get("Approvers", set())),
            "approval_is_not_required": _dict.get("ApprovalIsNotRequired"),
            "allow_self_approval": _dict.get("AllowSelfApproval"),
        }
    )


def get_groups_from_statements(statements: set[GroupStatement]) -> frozenset[str]:
    return frozenset(group for statement in statements for group in statement.resource)


class Config(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    schedule_policy_arn: str
    revoker_function_arn: str
    revoker_function_name: str
    schedule_group_name: str

    post_update_to_slack: bool = False
    slack_channel_id: str
    slack_bot_token: str

    approver_renotification_initial_wait_time: int
    approver_renotification_backoff_multiplier: int

    secondary_fallback_email_domains: list

    send_dm_if_user_not_in_channel: bool = True

    sso_instance_arn: str

    log_level: str = "INFO"
    slack_app_log_level: str = "INFO"
    statements_secret_arn: Optional[str] = None
    group_statements_secret_arn: Optional[str] = None
    statements: frozenset[Statement]
    group_statements: frozenset[GroupStatement]

    accounts: frozenset[str]
    permission_sets: frozenset[str]
    groups: frozenset[str]

    s3_bucket_for_audit_entry_name: str
    s3_bucket_prefix_for_partitions: str

    sso_elevator_scheduled_revocation_rule_name: str
    request_expiration_hours: int = 8

    max_permissions_duration_time: int
    permission_duration_list_override: list

    config_bucket_name: str = "sso-elevator-config"
    cache_enabled: bool = True

    good_result_emoji: str = ":large_green_circle:"

    waiting_result_emoji: str = ":large_yellow_circle:"
    bad_result_emoji: str = ":red_circle:"
    discarded_result_emoji: str = ":white_circle:"

    @model_validator(mode="before")
    @classmethod
    def get_accounts_and_permission_sets(cls, values: dict) -> dict:  # noqa: ANN101
        # Fetch from Secrets Manager if set
        statements_secret_arn = values.get("statements_secret_arn")
        group_statements_secret_arn = values.get("group_statements_secret_arn")
        
        if statements_secret_arn and values.get("statements") is None:
            statements_data = parameters.get_secret(statements_secret_arn, transform="json")
            values["statements"] = statements_data
        
        if group_statements_secret_arn and values.get("group_statements") is None:
            group_statements_data = parameters.get_secret(group_statements_secret_arn, transform="json")
            values["group_statements"] = group_statements_data
        
        # Parse statements - handle both JSON string and list
        statements_raw = values.get("statements")
        if statements_raw is not None:
            if isinstance(statements_raw, str):
                statements_raw = json.loads(statements_raw)
            statements = {parse_statement(st) for st in statements_raw}  # type: ignore # noqa: PGH003
        else:
            statements = set()

        # Parse group_statements - handle both JSON string and list
        group_statements_raw = values.get("group_statements")
        if group_statements_raw is not None:
            if isinstance(group_statements_raw, str):
                group_statements_raw = json.loads(group_statements_raw)
            group_statements = {parse_group_statement(st) for st in group_statements_raw}  # type: ignore # noqa: PGH003
        else:
            group_statements = set()

        if not group_statements and not statements:
            logger.warning("No statements and group statements found")
        groups = get_groups_from_statements(group_statements)
        permission_sets = set()
        accounts = set()
        s3_bucket_prefix_for_partitions = values.get("s3_bucket_prefix_for_partitions", "").rstrip("/")
        for statement in statements:
            permission_sets.update(statement.permission_set)
            if statement.resource_type == "Account":
                accounts.update(statement.resource)
        return values | {
            "accounts": accounts,
            "permission_sets": permission_sets,
            "statements": frozenset(statements),
            "group_statements": frozenset(group_statements),
            "groups": groups,
            "s3_bucket_prefix_for_partitions": s3_bucket_prefix_for_partitions,
        }


_config: Optional[Config] = None


def get_config() -> Config:
    global _config  # noqa: PLW0603
    if _config is None:
        _config = Config()  # type: ignore # noqa: PGH003
    return _config
