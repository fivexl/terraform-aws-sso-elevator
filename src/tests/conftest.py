import json
import os
from unittest.mock import MagicMock

import boto3
import pytest


def pytest_sessionstart(session):  # noqa: ANN201, ARG001, ANN001
    mock_env = {
        "schedule_policy_arn": "x",
        "revoker_function_arn": "x",
        "revoker_function_name": "x",
        "schedule_group_name": "x",
        "post_update_to_slack": "true",
        "send_dm_if_user_not_in_channel": "true",
        "slack_channel_id": "x",
        "slack_bot_token": "x",
        "sso_instance_arn": "x",
        "log_level": "DEBUG",
        "slack_app_log_level": "INFO",
        "s3_bucket_for_audit_entry_name": "x",
        "s3_bucket_prefix_for_partitions": "x",
        "sso_elevator_scheduled_revocation_rule_name": "x",
        "request_expiration_hours": "8",
        "approver_renotification_initial_wait_time": "15",
        "approver_renotification_backoff_multiplier": "2",
        "max_permissions_duration_time": "24",
        "secondary_fallback_email_domains": json.dumps(["domen.com"]),
        "permission_duration_list_override": json.dumps(["00:25", "01:00"]),
        "config_bucket_name": "test-config-bucket",
        "cache_enabled": "true",
        "statements": json.dumps(
            [
                {
                    "ResourceType": "Account",
                    "Resource": ["*"],
                    "PermissionSet": "*",
                    "Approvers": [
                        "email@domen.com",
                    ],
                    "AllowSelfApproval": True,
                }
            ]
        ),
        "group_statements": json.dumps(
            [
                {
                    "Resource": ["11111111-2222-3333-4444-555555555555"],
                    "Approvers": ["email@domen.com"],
                    "AllowSelfApproval": True,
                },
            ]
        ),
    }
    os.environ |= mock_env

    boto3.setup_default_session(region_name="us-east-1")


@pytest.fixture
def mock_s3_approval_config():
    """Returns a mock S3 client that returns approval configuration."""
    return {
        "statements": [
            {
                "ResourceType": "Account",
                "Resource": ["111111111111"],
                "PermissionSet": "AdministratorAccess",
                "Approvers": "example@gmail.com",
            }
        ],
        "group_statements": [
            {
                "Resource": ["11e111e1-e111-11ee-e111-1e11e1ee11e1"],
                "Approvers": "example@gmail.com",
                "AllowSelfApproval": True,
            }
        ],
    }


@pytest.fixture
def mock_s3_client(mock_s3_approval_config):
    """Returns a mock S3 client that returns approval configuration."""
    mock_client = MagicMock()
    mock_response = {"Body": MagicMock(read=lambda: json.dumps(mock_s3_approval_config).encode("utf-8"))}
    mock_client.get_object.return_value = mock_response
    mock_client.exceptions = MagicMock()
    mock_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    mock_client.exceptions.NoSuchBucket = type("NoSuchBucket", (Exception,), {})
    return mock_client
