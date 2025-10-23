import json
import os
from unittest.mock import patch

import boto3

MOCK_STATEMENTS = [
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

MOCK_GROUP_STATEMENTS = [
    {
        "Resource": ["11111111-2222-3333-4444-555555555555"],
        "Approvers": ["email@domen.com"],
        "AllowSelfApproval": True,
    },
]


def mock_get_secret(secret_name: str, transform: str = None):  # noqa: ANN201, ARG001
    """Mock function for parameters.get_secret() to return test data"""
    if "statements-secret" in secret_name or secret_name == "arn:aws:secretsmanager:us-east-1:123456789012:secret:statements":
        return MOCK_STATEMENTS
    elif "group-statements-secret" in secret_name or secret_name == "arn:aws:secretsmanager:us-east-1:123456789012:secret:group_statements":
        return MOCK_GROUP_STATEMENTS
    return None


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
        "statements_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:statements",
        "group_statements_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:group_statements",
    }
    os.environ |= mock_env

    boto3.setup_default_session(region_name="us-east-1")

    patcher = patch("aws_lambda_powertools.utilities.parameters.get_secret", side_effect=mock_get_secret)
    patcher.start()
