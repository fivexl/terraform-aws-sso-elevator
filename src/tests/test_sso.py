import os
import sys

import boto3
import pytest

sys.path.append("../sso-elevator")

import sso

client = boto3.client("sso-admin")


@pytest.fixture
def instance_arn() -> str:
    return os.environ["TEST_INSTANCE_ARN"]


@pytest.fixture
def account_id() -> str:
    return os.environ["TEST_ACCOUNT_ID"]


@pytest.fixture
def permission_set_arn() -> str:
    return os.environ["TEST_PERMISSION_SET_ARN"]


@pytest.fixture
def user_principal_id() -> str:
    return os.environ["TEST_USER_PRINCIPAL_ID"]


@pytest.fixture
def invalid_user_principal_id() -> str:
    return "11111fa111-1a111111-1111-1111-fa1f-1111111f1111"


@pytest.fixture
def account_assignment(
    instance_arn: str, account_id: str, permission_set_arn: str, user_principal_id: str
) -> sso.UserAccountAssignment:
    return sso.UserAccountAssignment(
        instance_arn=instance_arn,
        account_id=account_id,
        permission_set_arn=permission_set_arn,
        user_principal_id=user_principal_id,
    )


@pytest.fixture
def invalid_account_assignment(
    instance_arn: str, account_id: str, permission_set_arn: str, invalid_user_principal_id: str
) -> sso.UserAccountAssignment:
    return sso.UserAccountAssignment(
        instance_arn=instance_arn,
        account_id=account_id,
        permission_set_arn=permission_set_arn,
        user_principal_id=invalid_user_principal_id,
    )


def test_create_account_assignment_and_wait_for_result(account_assignment: sso.UserAccountAssignment):
    account_assignment_status = sso.create_account_assignment_and_wait_for_result(client, account_assignment)
    assert account_assignment_status.status == "SUCCEEDED"


def test_delete_account_assignment_and_wait_for_result(account_assignment: sso.UserAccountAssignment):
    account_assignment_status = sso.delete_account_assignment_and_wait_for_result(client, account_assignment)
    assert account_assignment_status.status == "SUCCEEDED"


def test_create_account_assignment_and_wait_for_result_with_imput_error(
    invalid_account_assignment: sso.UserAccountAssignment,
):
    account_assignment_status = sso.create_account_assignment_and_wait_for_result(client, invalid_account_assignment)
    assert account_assignment_status.status == "FAILED"


def test_delete_account_assignment_and_wait_for_result_with_imput_error(
    invalid_account_assignment: sso.UserAccountAssignment,
):
    account_assignment_status = sso.delete_account_assignment_and_wait_for_result(client, invalid_account_assignment)
    assert account_assignment_status.status == "FAILED"
