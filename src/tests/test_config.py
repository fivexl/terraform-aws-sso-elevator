import json
import os
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from pydantic import ValidationError

import config

from . import strategies

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
    except ValidationError as e:
        raise AssertionError("Statement parsing failed unexpectedly") from e


@given(strategies.group_statement_dict())
@settings(max_examples=50, suppress_health_check=(HealthCheck.too_slow,))
@example({}).xfail(raises=KeyError, reason="Empty dict is not a valid group_statement")
@example(VALID_GROUP_STATEMENT_DICT)
def test_parse_group_statement(dict_group_statement: dict):
    try:
        config.parse_group_statement(dict_group_statement)
    except ValidationError as e:
        raise AssertionError("Group statement parsing failed unexpectedly") from e


def config_dict(
    statements: SearchStrategy = strategies.jsonstr(st.lists(strategies.statement_dict())),  # noqa: B008
    group_statements: SearchStrategy = strategies.jsonstr(st.lists(strategies.group_statement_dict())),  # noqa: B008
    secondary_fallback_email_domains: SearchStrategy = strategies.jsonstr(  # noqa: B008
        st.lists(strategies.json_safe_text, max_size=10, min_size=1)  # noqa: B008
    ),
    permission_duration_list_override: SearchStrategy = strategies.jsonstr(  # noqa: B008
        st.lists(strategies.json_safe_text, max_size=10, min_size=1)  # noqa: B008
    ),
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
    os.environ.clear()  # noqa: B003
    # Convert all values to strings as os.environ expects
    os.environ.update({k: str(v) for k, v in dict_config.items()})
    config.Config()  # type: ignore[call-arg]


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
        permission_duration_list_override_as_json=False,
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


# TTL validation tests removed - cache no longer uses TTL


def test_load_approval_config_from_s3_success(mock_s3_client, mock_s3_approval_config):
    """Test successful S3 retrieval and JSON parsing."""
    result = config.load_approval_config_from_s3(mock_s3_client, "test-bucket", "config/approval-config.json")

    assert result == mock_s3_approval_config
    mock_s3_client.get_object.assert_called_once_with(Bucket="test-bucket", Key="config/approval-config.json")


def test_load_approval_config_from_s3_no_such_key(mock_s3_client):
    """Test S3 NoSuchKey error handling."""
    mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchKey("Key not found")

    with pytest.raises(mock_s3_client.exceptions.NoSuchKey):
        config.load_approval_config_from_s3(mock_s3_client, "test-bucket", "config/missing.json")


def test_load_approval_config_from_s3_no_such_bucket(mock_s3_client):
    """Test S3 NoSuchBucket error handling."""
    mock_s3_client.get_object.side_effect = mock_s3_client.exceptions.NoSuchBucket("Bucket not found")

    with pytest.raises(mock_s3_client.exceptions.NoSuchBucket):
        config.load_approval_config_from_s3(mock_s3_client, "missing-bucket", "config/approval-config.json")


def test_load_approval_config_from_s3_invalid_json(mock_s3_client):
    """Test invalid JSON error handling."""
    mock_response = {"Body": MagicMock(read=lambda: b"invalid json {{")}
    mock_s3_client.get_object.return_value = mock_response

    with pytest.raises(json.JSONDecodeError):
        config.load_approval_config_from_s3(mock_s3_client, "test-bucket", "config/approval-config.json")


def test_load_approval_config_from_s3_missing_keys(mock_s3_client):
    """Test missing keys in JSON structure - should default to empty lists."""
    incomplete_config = {"statements": []}
    mock_response = {"Body": MagicMock(read=lambda: json.dumps(incomplete_config).encode("utf-8"))}
    mock_s3_client.get_object.return_value = mock_response

    result = config.load_approval_config_from_s3(mock_s3_client, "test-bucket", "config/approval-config.json")

    assert result["statements"] == []
    assert result["group_statements"] == []


def test_config_with_s3_loaded_configuration(mock_s3_client, monkeypatch):
    """Test Config class initialization with S3-loaded configuration."""
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service: mock_s3_client if service == "s3" else MagicMock())

    config_dict = valid_config_dict(
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False,
    )
    config_dict["config_s3_key"] = "config/approval-config.json"
    config_dict["config_bucket_name"] = "test-bucket"
    # Remove statements and group_statements as they should come from S3
    del config_dict["statements"]
    del config_dict["group_statements"]

    cfg = config.Config(**config_dict)

    assert len(cfg.statements) > 0
    assert len(cfg.group_statements) > 0
    mock_s3_client.get_object.assert_called_once()


def test_config_statement_parsing_with_s3(mock_s3_client, monkeypatch):
    """Verify statement parsing still works correctly with S3-loaded data."""
    import boto3

    s3_config = {
        "statements": [VALID_STATEMENT_DICT],
        "group_statements": [VALID_GROUP_STATEMENT_DICT],
    }
    mock_response = {"Body": MagicMock(read=lambda: json.dumps(s3_config).encode("utf-8"))}
    mock_s3_client.get_object.return_value = mock_response

    monkeypatch.setattr(boto3, "client", lambda service: mock_s3_client if service == "s3" else MagicMock())

    config_dict = valid_config_dict(
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False,
    )
    config_dict["config_s3_key"] = "config/approval-config.json"
    del config_dict["statements"]
    del config_dict["group_statements"]

    cfg = config.Config(**config_dict)

    # Verify statements were parsed correctly
    assert len(cfg.statements) == 1
    statement = list(cfg.statements)[0]
    assert "AdministratorAccess" in statement.permission_set
    assert "111111111111" in statement.resource


def test_config_group_statement_parsing_with_s3(mock_s3_client, monkeypatch):
    """Verify group_statement parsing still works correctly with S3-loaded data."""
    import boto3

    s3_config = {
        "statements": [VALID_STATEMENT_DICT],
        "group_statements": [VALID_GROUP_STATEMENT_DICT],
    }
    mock_response = {"Body": MagicMock(read=lambda: json.dumps(s3_config).encode("utf-8"))}
    mock_s3_client.get_object.return_value = mock_response

    monkeypatch.setattr(boto3, "client", lambda service: mock_s3_client if service == "s3" else MagicMock())

    config_dict = valid_config_dict(
        secondary_fallback_email_domains_as_json=False,
        permission_duration_list_override_as_json=False,
    )
    config_dict["config_s3_key"] = "config/approval-config.json"
    del config_dict["statements"]
    del config_dict["group_statements"]

    cfg = config.Config(**config_dict)

    # Verify group_statements were parsed correctly
    assert len(cfg.group_statements) == 1
    group_statement = list(cfg.group_statements)[0]
    assert "11e111e1-e111-11ee-e111-1e11e1ee11e1" in group_statement.resource
    assert group_statement.allow_self_approval is True
