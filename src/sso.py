
from dataclasses import dataclass
from enum import Enum
import time

class RequestType(Enum):
    create = 1
    delete = 2

@dataclass
class AccountAssigmentRequestInput:
    sso_instance_arn: str
    account_id: str
    permission_set: str
    user_id: str
    request_type: RequestType


def list_sso_instances(client, logger):
    sso_instances = client.list_instances()
    logger.debug(f'All sso instances: {sso_instances}')
    if len(sso_instances['Instances']) == 0:
        raise EnvironmentError('No SSO instances found. Are you in the correct account?')
    return sso_instances


def wait_for_account_assigment_operation(logger, status_func, input, account_assigment_request):
    response_key = 'AccountAssignmentCreationStatus' if input.request_type == RequestType.create else 'AccountAssignmentDeletionStatus'
    request_id = account_assigment_request[response_key]['RequestId']
    request_key = 'AccountAssignmentCreationRequestId' if input.request_type == RequestType.create else 'AccountAssignmentDeletionRequestId'
    kwargs = {
        'InstanceArn': input.sso_instance_arn,
        request_key: request_id,
    }
    while True:
        status = status_func(**kwargs)
        if status[response_key]['Status'] == 'IN_PROGRESS':
            logger.debug('Still in progress')
            time.sleep(10)
            continue
        elif status[response_key]['Status'] == 'SUCCEEDED':
            return request_id
        elif status[response_key]['Status'] == 'FAILED':
            logger.error(status)
            raise RuntimeError


def modify_account_assigment(logger, mod_func, status_func, input: AccountAssigmentRequestInput):
    account_assigment_request = mod_func(
        InstanceArn=input.sso_instance_arn,
        TargetId=input.account_id,
        TargetType='AWS_ACCOUNT',
        PermissionSetArn=input.permission_set,
        PrincipalType='USER',
        PrincipalId=input.user_id
    )
    logger.debug(f'account_assigment_request: {account_assigment_request}')
    request_id = wait_for_account_assigment_operation(logger, status_func, input, account_assigment_request)
    return request_id


def create_account_assigment(logger, client, sso_instance_arn, account_id, permission_set, user_id):
    mod_func = getattr(client, 'create_account_assignment')
    status_func = getattr(client, 'describe_account_assignment_creation_status')
    logger.info(f'Add permission set {permission_set} for {user_id} to account {account_id}')
    input = AccountAssigmentRequestInput(sso_instance_arn, account_id, permission_set, user_id, RequestType.create)
    request_id = modify_account_assigment(logger, mod_func, status_func, input)
    return request_id


def delete_account_assigment(logger, client, sso_instance_arn, account_id, permission_set, user_id):
    mod_func = getattr(client, 'delete_account_assignment')
    status_func = getattr(client, 'describe_account_assignment_deletion_status')
    logger.info(f'Delete permission set {permission_set} for {user_id} from account {account_id}')
    input = AccountAssigmentRequestInput(sso_instance_arn, account_id, permission_set, user_id, RequestType.delete)
    request_id = modify_account_assigment(logger, mod_func, status_func, input)
    return request_id