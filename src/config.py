import json
import os
from typing import Optional

from aws_lambda_powertools import Logger
from mypy_boto3_s3 import S3Client
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


def load_approval_config_from_s3(s3_client: S3Client, bucket_name: str, s3_key: str) -> tuple[dict, str | None]:
    """
    Load approval configuration from S3.

    Args:
        s3_client: Boto3 S3 client
        bucket_name: Name of the S3 bucket
        s3_key: Key of the S3 object containing configuration

    Returns:
        Tuple of (config dict with 'statements' and 'group_statements' keys, ETag string or None)

    Raises:
        Exception: If S3 retrieval or JSON parsing fails
    """
    try:
        logger.info(f"Loading approval config from s3://{bucket_name}/{s3_key}")
        response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        etag = response.get("ETag")
        content = response["Body"].read().decode("utf-8")
        config_data = json.loads(content)

        if "statements" not in config_data or "group_statements" not in config_data:
            logger.warning(f"Missing required keys in S3 config. Found keys: {list(config_data.keys())}")
            # Default to empty lists if keys are missing
            config_data.setdefault("statements", [])
            config_data.setdefault("group_statements", [])

        logger.info("Successfully loaded approval config from S3")
        return config_data, etag

    except s3_client.exceptions.NoSuchKey:
        logger.error(f"S3 object not found: s3://{bucket_name}/{s3_key}")
        raise
    except s3_client.exceptions.NoSuchBucket:
        logger.error(f"S3 bucket not found: {bucket_name}")
        raise
    except Exception as e:
        logger.error(
            f"Failed to load approval config from S3: {e}",
            exc_info=True,
        )
        raise


_initial_config_etag: str | None = None


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
            "approver_groups": to_set_if_list_or_str(_dict.get("ApproverGroups", set())),
            "resource_type": _dict.get("ResourceType"),
            "approval_is_not_required": _dict.get("ApprovalIsNotRequired"),
            "allow_self_approval": _dict.get("AllowSelfApproval"),
            "required_group_membership": to_set_if_list_or_str(_dict.get("RequiredGroupMembership", set())),
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
            "approver_groups": to_set_if_list_or_str(_dict.get("ApproverGroups", set())),
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
    identity_store_id: str = ""

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

    config_bucket_name: str = "sso-elevator-config"
    config_s3_key: str = ""
    cache_enabled: bool = True

    good_result_emoji: str = ":large_green_circle:"
    allow_anyone_to_end_session_early: bool = False

    waiting_result_emoji: str = ":large_yellow_circle:"
    bad_result_emoji: str = ":red_circle:"
    discarded_result_emoji: str = ":white_circle:"

    # Status badges for Slack messages
    pending_status: str = ":hourglass_flowing_sand: *AWAITING APPROVAL*"
    granted_status: str = ":white_check_mark: *GRANTED*"
    denied_status: str = ":x: *DENIED*"
    timed_out_status: str = ":clock1: *TIMED OUT*"
    access_ended_status: str = ":checkered_flag: *SESSION COMPLETE*"

    @model_validator(mode="before")
    @classmethod
    def get_accounts_and_permission_sets(cls, values: dict) -> dict:  # noqa: ANN101
        global _initial_config_etag  # noqa: PLW0603
        import boto3

        config_s3_key = values.get("config_s3_key", "")

        # Load from S3 if config_s3_key is provided
        if config_s3_key:
            s3_client = boto3.client("s3")
            config_bucket_name = values.get("config_bucket_name", "sso-elevator-config")
            config_data, etag = load_approval_config_from_s3(s3_client, config_bucket_name, config_s3_key)
            _initial_config_etag = etag
            statements_raw = config_data.get("statements")
            group_statements_raw = config_data.get("group_statements")
        else:
            # Fallback to environment variables
            statements_raw = values.get("statements")
            if statements_raw is not None and isinstance(statements_raw, str):
                statements_raw = json.loads(statements_raw)
            group_statements_raw = values.get("group_statements")
            if group_statements_raw is not None and isinstance(group_statements_raw, str):
                group_statements_raw = json.loads(group_statements_raw)

        # Parse statements
        if statements_raw is not None:
            statements = {parse_statement(st) for st in statements_raw}  # type: ignore # noqa: PGH003
        else:
            statements = set()

        # Parse group_statements
        if group_statements_raw is not None:
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
_config_etag: Optional[str] = None


def get_config() -> Config:
    global _config, _config_etag  # noqa: PLW0603
    if _config is None:
        _config = Config()  # type: ignore # noqa: PGH003
        _config_etag = _initial_config_etag
    return _config


def check_and_refresh_config(s3_client: S3Client) -> Config:
    """Check S3 ETag, reload config if changed."""
    global _config, _config_etag  # noqa: PLW0603

    if _config is None:
        return get_config()

    config_s3_key = os.environ.get("CONFIG_S3_KEY", "")
    if not config_s3_key:
        return _config

    bucket = os.environ.get("CONFIG_BUCKET_NAME", "sso-elevator-config")

    try:
        response = s3_client.head_object(Bucket=bucket, Key=config_s3_key)
        current_etag = response.get("ETag")

        if current_etag and current_etag != _config_etag:
            logger.info(f"Config changed (ETag: {_config_etag} -> {current_etag}), reloading")
            _config = None  # Reset to force reload
            _config = get_config()
            _config_etag = current_etag
    except Exception as e:
        logger.warning(f"Failed to check config freshness: {e}")

    return _config
