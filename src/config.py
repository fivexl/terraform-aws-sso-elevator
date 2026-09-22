import json
import os
from typing import Optional

from aws_lambda_powertools import Logger
from mypy_boto3_s3 import S3Client
from mypy_boto3_ssm import SSMClient
from pydantic import field_validator, model_validator
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


def load_approval_config_from_s3(s3_client: S3Client, bucket_name: str, s3_key: str) -> dict:
    """
    Load approval configuration from S3.

    Args:
        s3_client: Boto3 S3 client
        bucket_name: Name of the S3 bucket
        s3_key: Key of the S3 object containing configuration

    Returns:
        Dictionary with 'statements' and 'group_statements' keys

    Raises:
        Exception: If S3 retrieval or JSON parsing fails
    """
    try:
        logger.info(f"Loading approval config from s3://{bucket_name}/{s3_key}")
        response = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
        content = response["Body"].read().decode("utf-8")
        config_data = json.loads(content)

        if "statements" not in config_data or "group_statements" not in config_data:
            logger.warning(f"Missing required keys in S3 config. Found keys: {list(config_data.keys())}")
            # Default to empty lists if keys are missing
            config_data.setdefault("statements", [])
            config_data.setdefault("group_statements", [])

        logger.info("Successfully loaded approval config from S3")
        return config_data

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


def get_secret_from_ssm(ssm_client: SSMClient, parameter_name: str) -> str:
    """Read one SecureString parameter's decrypted value from SSM Parameter Store.

    Used for the handful of actual secrets (the Slack bot token, currently) that Terraform
    used to push into a Lambda's plaintext environment variables -- and, before that, into
    the Terraform state file, since a value passed through a Terraform variable ends up
    there regardless of where it's ultimately written to. The parameter itself is created
    by Terraform with a placeholder value (see revoker_ssm_parameters.tf /
    attribute_syncer_ssm_parameters.tf) and its lifecycle ignores further changes to that
    value, so the real secret is set once, out of band (console or CLI), and never flows
    through Terraform at all.
    """
    response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
    return response["Parameter"]["Value"]


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
            "allowed_groups": to_set_if_list_or_str(_dict.get("AllowedGroups", set())),
            "allowed_users": to_set_if_list_or_str(_dict.get("AllowedUsers", set())),
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
            "allowed_groups": to_set_if_list_or_str(_dict.get("AllowedGroups", set())),
            "allowed_users": to_set_if_list_or_str(_dict.get("AllowedUsers", set())),
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
    # Empty for the lambdas that do not verify inbound Slack webhook signatures (revoker,
    # attribute-syncer) -- only the access-requester Lambda ever sets this to a real value.
    slack_signing_secret: str = ""

    approver_renotification_initial_wait_time: int
    approver_renotification_backoff_multiplier: int

    secondary_fallback_email_domains: list

    send_dm_if_user_not_in_channel: bool = True

    sso_instance_arn: str

    # CLI access-request path: identity read from the AWS_IAM authorizer's
    # userArn is only trusted if it matches both of these.
    # cli_expected_account_id is always the account this module is deployed
    # into (see locals.tf) — not operator-configurable, since cli_auth.py's
    # iam:GetRole check can only ever resolve against that same account
    # regardless of what this claimed, so letting them diverge was either a
    # guaranteed rejection or, worse, a cross-account impersonation path.
    # The "" default here only matters for tests/direct Config() use that
    # don't go through Terraform at all.
    cli_expected_account_id: str = ""
    cli_sso_role_name_prefix: str = "AWSReservedSSO_"

    # Defense-in-depth only, not a real access control: a direct-invoke caller
    # controls the entire event JSON, so this value is guessable, not secret
    # (it's visible via DescribeApi/Terraform state to anyone with read
    # access). It only blocks a naive/accidental direct lambda:InvokeFunction
    # that doesn't bother setting requestContext.apiId -- it does not stop a
    # deliberate forgery. The real trust boundary is the IAM policy deciding
    # who can invoke this Lambda at all; see the README's CLI section.
    cli_expected_api_id: str = ""

    @field_validator("cli_sso_role_name_prefix")
    @classmethod
    def cli_sso_role_name_prefix_must_not_be_empty(cls, value: str) -> str:  # noqa: ANN101
        # str.startswith("") is always True, so an empty prefix would
        # silently make cli_auth.extract_identity's role-name check accept
        # any role name at all instead of rejecting non-matching ones.
        if value == "":
            raise ValueError("cli_sso_role_name_prefix must not be empty — an empty prefix matches every role name")
        return value

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

    @field_validator("permission_duration_list_override")
    @classmethod
    def permission_duration_list_override_entries_must_be_hh_mm(cls, value: list) -> list:  # noqa: ANN101
        # main.py's _max_allowed_minutes parses every entry as
        # int(hours):int(minutes) on every single CLI request (it derives
        # the CLI's own duration ceiling from this same list) -- a
        # malformed entry ("8" with no colon, "1:00:00" with an extra one,
        # a non-numeric segment) raised ValueError there, reaching the
        # blanket exception handler as a 500 plus a Slack post for every
        # CLI request this deployment ever received, not just a
        # misconfigured one (found in a final pre-delivery review).
        # Validating the shape once, at config load, turns a bad Terraform
        # input into an immediate, loud failure instead of a landmine that
        # only detonates on live traffic.
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError(f"permission_duration_list_override entries must be strings, got {entry!r}")
            parts = entry.split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):  # noqa: PLR2004
                raise ValueError(f'permission_duration_list_override entries must look like "H:MM" (e.g. "01:30"), got {entry!r}')
        return value

    config_bucket_name: str = "sso-elevator-config"
    config_s3_key: str = ""
    cache_enabled: bool = True
    # Empty string, not None, when the operator hasn't set one -- same
    # "unset" sentinel this codebase already uses for cli_expected_api_id,
    # since Terraform can't pass a real null through a Lambda environment
    # variable. Lets the cache module use the operator's own KMS key for
    # the user/account/permission-set caches it writes into this same
    # bucket, instead of always hardcoding AES256 regardless of what
    # encryption this bucket's other object (approval-config.json) already
    # uses (#194 High #5, found by Andrey Devyatkin).
    config_bucket_kms_key_arn: str = ""

    good_result_emoji: str = ":large_green_circle:"

    waiting_result_emoji: str = ":large_yellow_circle:"
    bad_result_emoji: str = ":red_circle:"
    discarded_result_emoji: str = ":white_circle:"

    @model_validator(mode="before")
    @classmethod
    def get_accounts_and_permission_sets(cls, values: dict) -> dict:  # noqa: ANN101
        import boto3

        config_s3_key = values.get("config_s3_key", "")

        # Load from S3 if config_s3_key is provided
        if config_s3_key:
            s3_client = boto3.client("s3")
            config_bucket_name = values.get("config_bucket_name", "sso-elevator-config")
            config_data = load_approval_config_from_s3(s3_client, config_bucket_name, config_s3_key)
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


def _resolve_secret_from_ssm_env(env_var_name: str) -> str | None:
    """Read a Slack secret from SSM when its *_SSM_PARAMETER_NAME environment variable is
    set (opt-in per deployment -- see read_slack_secrets_from_ssm in vars.tf). Returns None
    when the environment variable isn't set at all, so the caller can leave the corresponding
    Config field at its normal (environment-variable-sourced) value.

    A failure to actually reach SSM (throttling, a KMS/IAM denial, a transient AWS issue) is
    caught and logged here rather than raised: this runs unconditionally at Config() construction
    time, for every Lambda including the revoker, whose core job (revoking access) has nothing to
    do with Slack. Letting an SSM hiccup take down Config() entirely would take the whole Lambda
    down with it -- degrading to an empty secret instead means Slack notifications fail loudly on
    their own, without blocking the security-critical work that doesn't depend on them.
    """
    parameter_name = os.environ.get(env_var_name, "")
    if not parameter_name:
        return None
    import boto3

    try:
        return get_secret_from_ssm(boto3.client("ssm"), parameter_name)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Failed to read {env_var_name} ({parameter_name}) from SSM -- continuing with an empty secret: {e}")
        return ""


def get_config() -> Config:
    global _config  # noqa: PLW0603
    if _config is None:
        overrides = {}
        slack_bot_token = _resolve_secret_from_ssm_env("SLACK_BOT_TOKEN_SSM_PARAMETER_NAME")
        if slack_bot_token is not None:
            overrides["slack_bot_token"] = slack_bot_token
        slack_signing_secret = _resolve_secret_from_ssm_env("SLACK_SIGNING_SECRET_SSM_PARAMETER_NAME")
        if slack_signing_secret is not None:
            overrides["slack_signing_secret"] = slack_signing_secret
        _config = Config(**overrides)  # type: ignore # noqa: PGH003
    return _config
