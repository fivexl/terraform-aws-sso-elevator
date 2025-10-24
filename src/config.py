import os
from typing import Optional

from aws_lambda_powertools import Logger
from pydantic import BaseSettings, root_validator, validator

import entities
from statement import Statement, GroupStatement

# TTL validation constants (in minutes)
MIN_TTL_MINUTES = 0  # 0 means disabled
MAX_TTL_MINUTES = 525600  # 1 year in minutes


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

    return Statement.parse_obj(
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

    return GroupStatement.parse_obj(
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

    cache_table_name: str = "sso-elevator-cache"
    cache_ttl_minutes: int = 5760  # 4 days

    good_result_emoji: str = ":large_green_circle:"

    @validator("cache_ttl_minutes")
    def validate_cache_ttl_minutes(cls, v):  # noqa: ANN001, ANN101, ANN201
        """Validate cache TTL minutes to prevent NoSQL injection.

        Args:
            v: The cache TTL minutes value

        Returns:
            Validated integer value

        Raises:
            ValueError: If the value is invalid or out of reasonable bounds
        """
        try:
            ttl_int = int(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"cache_ttl_minutes must be a valid integer, got: {v}") from e

        # Allow 0 (disabled) or values between 1 and 525600 (1 year in minutes)
        if ttl_int < MIN_TTL_MINUTES or ttl_int > MAX_TTL_MINUTES:
            raise ValueError(f"cache_ttl_minutes must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES} (1 year), got: {ttl_int}")

        return ttl_int

    waiting_result_emoji: str = ":large_yellow_circle:"
    bad_result_emoji: str = ":red_circle:"
    discarded_result_emoji: str = ":white_circle:"

    class Config:
        frozen = True

    @root_validator(pre=True)
    def get_accounts_and_permission_sets(cls, values: dict) -> dict:  # noqa: ANN101
        statements = (
            {parse_statement(st) for st in values.get("statements", [])}  # type: ignore # noqa: PGH003
            if values.get("statements") is not None
            else set()
        )

        group_statements = (
            {parse_group_statement(st) for st in values.get("group_statements", [])}  # type: ignore # noqa: PGH003
            if values.get("group_statements") is not None
            else set()
        )

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
