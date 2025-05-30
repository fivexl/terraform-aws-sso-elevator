import json
import os

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from pydantic import ValidationError

import config

from . import strategies

# ruff: noqa
VALID_STATEMENT_DICT = {
    "ResourceType": "Account",
    "Resource": ["111111111111"],
    "PermissionSet": "AdministratorAccess",
    "Approvers": "example@gmail.com",
}
VALID_GROUP_STATEMENT_DICT = {
    "Resource": ["11e111e1-e111-11ee-e111-1e11e1ee11e1"],
    "Approvers": "example@gmail.com",
    "AllowSelfApproval": True,
}


@given(strategies.statement_dict())
@settings(max_examples=50, suppress_health_check=(HealthCheck.too_slow,))
@example({}).xfail(raises=KeyError, reason="Empty dict is not a valid statement")
@example(VALID_STATEMENT_DICT)
def test_parse_statement(
    dict_statement: dict,
):
    try:
        config.parse_statement(dict_statement)
    except ValidationError:
        assert False


@given(strategies.group_statement_dict())
@settings(max_examples=50, suppress_health_check=(HealthCheck.too_slow,))
@example({}).xfail(raises=KeyError, reason="Empty dict is not a valid group_statement")
@example(VALID_GROUP_STATEMENT_DICT)
def test_parse_group_statement(dict_group_statement: dict):
    try:
        config.parse_group_statement(dict_group_statement)
    except ValidationError:
        assert False


def config_dict(
    statements: SearchStrategy = strategies.jsonstr(st.lists(strategies.statement_dict())),
    group_statements: SearchStrategy = strategies.jsonstr(st.lists(strategies.group_statement_dict())),
    secondary_fallback_email_domains: SearchStrategy = strategies.jsonstr(st.lists(strategies.json_safe_text, max_size=10, min_size=1)),
    permission_duration_list_override: SearchStrategy = strategies.jsonstr(st.lists(strategies.json_safe_text, max_size=10, min_size=1))
):
    return st.fixed_dictionaries(
        {
            "schedule_policy_arn": strategies.json_safe_text,
            "revoker_function_arn": strategies.json_safe_text,
            "revoker_function_name": strategies.json_safe_text,
            "schedule_group_name": strategies.json_safe_text,
            "slack_channel_id": strategies.json_safe_text,
            "slack_bot_token": strategies.json_safe_text,
            "sso_instance_arn": strategies.json_safe_text,
            "s3_bucket_for_audit_entry_name": strategies.json_safe_text,
            "s3_bucket_prefix_for_partitions": strategies.json_safe_text,
            "sso_elevator_scheduled_revocation_rule_name": strategies.json_safe_text,
            "log_level": st.one_of(st.just("INFO"), st.just("DEBUG"), st.just("WARNING"), st.just("ERROR"), st.just("CRITICAL")),
            "post_update_to_slack": strategies.str_bool,
            "send_dm_if_user_not_in_channel": strategies.str_bool,
            "statements": statements,
            "group_statements": group_statements,
            "request_expiration_hours": st.integers(min_value=0, max_value=24),
            "approver_renotification_initial_wait_time": st.integers(min_value=0, max_value=60),
            "approver_renotification_backoff_multiplier": st.integers(min_value=0, max_value=10),
            "max_permissions_duration_time": st.integers(min_value=0, max_value=24),
            "secondary_fallback_email_domains": secondary_fallback_email_domains,
            "permission_duration_list_override": permission_duration_list_override,
        }
    )


def valid_config_dict(
    statements_as_json: bool = True,
    group_statements_as_json: bool = True,
    secondary_fallback_email_domains_as_json: bool = True,
    permission_duration_list_override_as_json: bool = True,
):
    if statements_as_json:
        statements = json.dumps([VALID_STATEMENT_DICT])
    else:
        statements = [VALID_STATEMENT_DICT]

    if group_statements_as_json:
        group_statements = json.dumps([VALID_GROUP_STATEMENT_DICT])
    else:
        group_statements = [VALID_GROUP_STATEMENT_DICT]

    if secondary_fallback_email_domains_as_json:
        secondary_fallback_email_domains = json.dumps(["domen.com"])
    else:
        secondary_fallback_email_domains = ["domen.com"]

    if permission_duration_list_override_as_json:
        permission_duration_list_override = json.dumps(["00:01", "00:15"])
    else:
        permission_duration_list_override = ["00:01", "00:15"]

    return {
        "schedule_policy_arn": "x",
        "revoker_function_arn": "x",
        "revoker_function_name": "x",
        "schedule_group_name": "x",
        "slack_channel_id": "x",
        "slack_bot_token": "x",
        "sso_instance_arn": "x",
        "log_level": "INFO",
        "post_update_to_slack": "False",
        "send_dm_if_user_not_in_channel": "True",
        "statements": statements,
        "group_statements": group_statements,
        "s3_bucket_for_audit_entry_name": "x",
        "s3_bucket_prefix_for_partitions": "x",
        "sso_elevator_scheduled_revocation_rule_name": "x",
        "request_expiration_hours": "8",
        "approver_renotification_initial_wait_time": "15",
        "approver_renotification_backoff_multiplier": "2",
        "max_permissions_duration_time": "24",
        "secondary_fallback_email_domains": secondary_fallback_email_domains,
        "permission_duration_list_override": permission_duration_list_override,
    }


@given(config_dict())
@example(valid_config_dict())
@example({}).xfail(raises=ValidationError, reason="Empty dict is not a valid config")
@example(valid_config_dict() | {"post_update_to_slack": "x"}).xfail(raises=ValidationError, reason="Invalid bool")
@example(valid_config_dict() | {"send_dm_if_user_not_in_channel": "x"}).xfail(raises=ValidationError, reason="Invalid bool")
@settings(max_examples=50, suppress_health_check=(HealthCheck.too_slow,))
def test_config_load_environment_variables(dict_config: dict):
    os.environ = dict_config
    config.Config()  # type: ignore


@given(
    config_dict(
        statements=st.lists(strategies.statement_dict(), max_size=20),
        group_statements=st.lists(strategies.group_statement_dict(), max_size=20),
        secondary_fallback_email_domains=st.lists(strategies.json_safe_text, max_size=10, min_size=1),
        permission_duration_list_override=st.lists(strategies.json_safe_text, max_size=10, min_size=1),
    )
)
@settings(max_examples=50, suppress_health_check=(HealthCheck.too_slow,))
@example(
    valid_config_dict(
        statements_as_json=False,
        group_statements_as_json=False,
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False,
    )
)
@example(
    valid_config_dict(
        statements_as_json=False,
        group_statements_as_json=False,
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False
    )
    | {"post_update_to_slack": "x"}
).xfail(raises=ValidationError, reason="Invalid bool")
@example(
    valid_config_dict(
        statements_as_json=False,
        group_statements_as_json=False,
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False,
    )
    | {"send_dm_if_user_not_in_channel": "x"}
).xfail(raises=ValidationError, reason="Invalid bool")
def test_config_init(dict_config: dict):
    config.Config(**dict_config)
