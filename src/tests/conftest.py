import os

import boto3


def pytest_sessionstart(session):  # noqa: ANN201, ARG001, ANN001
    mock_env = {
        "schedule_policy_arn": "x",
        "revoker_function_arn": "x",
        "revoker_function_name": "x",
        "schedule_group_name": "x",
        "post_update_to_slack": "true",
        "slack_channel_id": "x",
        "slack_bot_token": "x",
        "sso_instance_arn": "x",
        "log_level": "DEBUG",
        "slack_app_log_level": "INFO",
        "s3_bucket_for_audit_entry_name": "x",
        "s3_bucket_prefix_for_partitions": "x",
        "sso_elevator_scheduled_revocation_rule_name": "x",
    }
    os.environ |= mock_env

    boto3.setup_default_session(region_name="us-east-1")
