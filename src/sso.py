from __future__ import annotations

import datetime
import os
import time
from dataclasses import dataclass
from typing import Callable, Generator, Optional, TypeVar

from aws_lambda_powertools import Logger
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_sso_admin import SSOAdminClient, type_defs

import entities
import errors

T = TypeVar("T")

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)


@dataclass
class AccountAssignmentStatus:
    status: str
    request_id: str
    failure_reason: Optional[str]
    target_id: str
    target_type: str
    permission_set_arn: str
    principal_type: str
    principal_id: str
    created_date: Optional[str]

    @staticmethod
    def from_type_def(d: type_defs.AccountAssignmentOperationStatusTypeDef) -> AccountAssignmentStatus:
        return AccountAssignmentStatus(
            status=d["Status"],  # type: ignore
            request_id=d["RequestId"],  # type: ignore
            failure_reason=d.get("FailureReason"),  # type: ignore
            target_id=d["TargetId"],  # type: ignore
            target_type=d["TargetType"],  # type: ignore
            permission_set_arn=d["PermissionSetArn"],  # type: ignore
            principal_type=d["PrincipalType"],  # type: ignore
            principal_id=d["PrincipalId"],  # type: ignore
            created_date=d.get("CreatedDate"),  # type: ignore
        )

    @staticmethod
    def is_in_progress(status: AccountAssignmentStatus) -> bool:
        return status.status == "IN_PROGRESS"

    @staticmethod
    def is_ready(status: AccountAssignmentStatus) -> bool:
        return status.status == "SUCCEEDED"

    @staticmethod
    def is_failed(status: AccountAssignmentStatus) -> bool:
        return status.status == "FAILED"


@dataclass
class UserAccountAssignment:
    instance_arn: str
    account_id: str
    permission_set_arn: str
    user_principal_id: str

    def as_dict(self) -> dict:
        return {
            "InstanceArn": self.instance_arn,
            "TargetId": self.account_id,
            "PermissionSetArn": self.permission_set_arn,
            "PrincipalId": self.user_principal_id,
            "TargetType": "AWS_ACCOUNT",
            "PrincipalType": "USER",
        }


def create_account_assignment(client: SSOAdminClient, assignment: UserAccountAssignment) -> AccountAssignmentStatus:
    response = client.create_account_assignment(**assignment.as_dict())
    return AccountAssignmentStatus.from_type_def(response["AccountAssignmentCreationStatus"])


def delete_account_assignment(client: SSOAdminClient, assignment: UserAccountAssignment) -> AccountAssignmentStatus:
    response = client.delete_account_assignment(**assignment.as_dict())
    return AccountAssignmentStatus.from_type_def(response["AccountAssignmentDeletionStatus"])


def describe_account_assignment_creation_status(client: SSOAdminClient, assignment: UserAccountAssignment, request_id):
    response = client.describe_account_assignment_creation_status(
        InstanceArn=assignment.instance_arn,
        AccountAssignmentCreationRequestId=request_id,
    )
    return AccountAssignmentStatus.from_type_def(response["AccountAssignmentCreationStatus"])


def describe_account_assignment_deletion_status(client: SSOAdminClient, assignment: UserAccountAssignment, request_id):
    response = client.describe_account_assignment_deletion_status(
        InstanceArn=assignment.instance_arn,
        AccountAssignmentDeletionRequestId=request_id,
    )
    return AccountAssignmentStatus.from_type_def(response["AccountAssignmentDeletionStatus"])


def retry_while(
    fn: Callable[[], T],
    condition: Callable[[T], bool],
    retry_period_seconds: int = 1,
    timeout_seconds: int = 20,
) -> T:
    # If timeout_seconds -1, then retry forever.
    start = datetime.datetime.now()

    def is_timeout(timeout_seconds: int) -> bool:
        if timeout_seconds == -1:
            return False
        return datetime.datetime.now() - start >= datetime.timedelta(seconds=timeout_seconds)

    while True:
        response = fn()
        if is_timeout(timeout_seconds):
            return response

        if condition(response):
            time.sleep(retry_period_seconds)
            continue
        else:
            return response


def create_account_assignment_and_wait_for_result(client: SSOAdminClient, assignment: UserAccountAssignment) -> AccountAssignmentStatus:
    response = create_account_assignment(client, assignment)
    if AccountAssignmentStatus.is_ready(response):
        return response
    else:

        def fn():
            return describe_account_assignment_creation_status(client, assignment, response.request_id)

        result = retry_while(fn, condition=AccountAssignmentStatus.is_in_progress, timeout_seconds=-1)
    logger.info(f"Account assignment creation result: {result}")
    if AccountAssignmentStatus.is_failed(result):
        logger.error(f"Failed to create account assignment: {result}")
        raise errors.FailedToUpdatePermissions("Failed to create account assignment.")
    return result


def delete_account_assignment_and_wait_for_result(client: SSOAdminClient, assignment: UserAccountAssignment):
    response = delete_account_assignment(client, assignment)
    if AccountAssignmentStatus.is_ready(response):
        return response
    else:

        def fn():
            return describe_account_assignment_deletion_status(client, assignment, response.request_id)

        result = retry_while(fn, condition=AccountAssignmentStatus.is_in_progress, timeout_seconds=-1)
    logger.info(f"Account assignment deletion result: {result}")
    return result


@dataclass
class IAMIdentityCenterInstance:
    """IAM Identity Center Instance

    Attributes:
        arn (str): ARN of the IAM Identity Center Instance
        identity_store_id (str): ID of the Identity Store
    """

    arn: str
    identity_store_id: str

    @staticmethod
    def from_instance_metadata_type_def(td: type_defs.InstanceMetadataTypeDef) -> "IAMIdentityCenterInstance":
        return IAMIdentityCenterInstance(
            arn=td["InstanceArn"],  # type: ignore
            identity_store_id=td["IdentityStoreId"],  # type: ignore
        )


def list_sso_instances(client: SSOAdminClient) -> list[IAMIdentityCenterInstance]:
    """List all IAM Identity Center Instances

    Returns:
        list[IAMIdentityCenterInstance]: List of IAM Identity Center Instances
    """
    instances: list[IAMIdentityCenterInstance] = []
    paginator = client.get_paginator("list_instances")
    for page in paginator.paginate():
        instances.extend(IAMIdentityCenterInstance.from_instance_metadata_type_def(instance) for instance in page["Instances"])
    return instances


def describe_sso_instance(client: SSOAdminClient, instance_arn: str) -> IAMIdentityCenterInstance:
    """Describe IAM Identity Center Instance

    Args:
        instance_arn (str): ARN of the IAM Identity Center Instance

    Returns:
        IAMIdentityCenterInstance: IAM Identity Center Instance
    """
    sso_instances = list_sso_instances(client)
    return next(instance for instance in sso_instances if instance.arn == instance_arn)


@dataclass
class AccountAssignment:
    account_id: str
    permission_set_arn: str
    principal_id: str
    principal_type: str

    @staticmethod
    def from_type_def(td: type_defs.AccountAssignmentTypeDef) -> AccountAssignment:
        return AccountAssignment(
            account_id=td["AccountId"],  # type: ignore
            permission_set_arn=td["PermissionSetArn"],  # type: ignore
            principal_id=td["PrincipalId"],  # type: ignore
            principal_type=td["PrincipalType"],  # type: ignore
        )


def list_user_account_assignments(
    client: SSOAdminClient,
    instance_arn: str,
    account_ids: list[str],
    permission_set_arns: list[str],
) -> list["AccountAssignment"]:
    paginator = client.get_paginator("list_account_assignments")
    account_assignments: list[AccountAssignment] = []

    for account_id in account_ids:
        for permission_set_arn in permission_set_arns:
            for page in paginator.paginate(
                InstanceArn=instance_arn,
                AccountId=account_id,
                PermissionSetArn=permission_set_arn,
            ):
                for account_assignment in page["AccountAssignments"]:
                    aa = AccountAssignment.from_type_def(account_assignment)
                    if aa.principal_type == "USER":
                        account_assignments.append(aa)
    return account_assignments

def parse_permission_set(td: type_defs.DescribePermissionSetResponseTypeDef) -> entities.aws.PermissionSet:
    ps = td.get("PermissionSet", {})
    return entities.aws.PermissionSet.parse_obj(
        {
            "name": ps.get("Name"),
            "arn": ps.get("PermissionSetArn"),
            "description": ps.get("Description"),
        }
    )


def describe_permission_set(client: SSOAdminClient, sso_instance_arn: str, permission_set_arn: str) -> entities.aws.PermissionSet:
    td = client.describe_permission_set(InstanceArn=sso_instance_arn, PermissionSetArn=permission_set_arn)
    return parse_permission_set(td)


def get_permission_set_by_name(client: SSOAdminClient, sso_instance_arn: str, permission_set_name: str) -> entities.aws.PermissionSet:
    if ps := next(
        (permission_set for permission_set in list_permission_sets(client, sso_instance_arn) if permission_set.name == permission_set_name),
        None,
    ):
        return ps
    raise errors.NotFound(f"Permission set with name {permission_set_name} not found")


def list_permission_sets_arns(client: SSOAdminClient, sso_instance_arn: str) -> Generator[str, None, None]:
    paginator = client.get_paginator("list_permission_sets")
    for page in paginator.paginate(InstanceArn=sso_instance_arn):
        yield from page["PermissionSets"]


def list_permission_sets(client: SSOAdminClient, sso_instance_arn: str) -> Generator[entities.aws.PermissionSet, None, None]:
    for permission_set_arn in list_permission_sets_arns(client, sso_instance_arn):
        yield describe_permission_set(client, sso_instance_arn, permission_set_arn)


def get_user_principal_id_by_email(client: IdentityStoreClient, identity_store_id: str, email: str) -> str:
    response = client.list_users(IdentityStoreId=identity_store_id)
    for user in response["Users"]:
        for user_email in user.get("Emails", []):
            if user_email.get("Value") == email:
                return user["UserId"]

    raise errors.NotFound(f"AWS SSO User with email {email} not found")


def get_user_emails(client: IdentityStoreClient, identity_store_id: str, user_id: str) -> list[str]:
    user = client.describe_user(
        IdentityStoreId=identity_store_id,
        UserId=user_id,
    )
    return [email["Value"] for email in user["Emails"] if "Value" in email]
