import base64
import json
import os
from dataclasses import dataclass
from typing import Union
from urllib import parse as urlparse

import boto3
from aws_lambda_powertools import Logger

import config
import dynamodb
import slack
import sso

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

org_client = boto3.client("organizations")  # type: ignore
sso_client = boto3.client("sso-admin")  # type: ignore
identity_center_client = boto3.client("identitystore")  # type: ignore


def lambda_handler(event, _):
    print(f"event: {json.dumps(event)}")
    cfg = config.Config()  # type: ignore
    # Get body and headers
    if "headers" not in event:
        logger.error("Request does not contain headers. Return 400")
        return {"statusCode": 400}
    if "body" not in event:
        logger.error("Request does not contain body. Return 400")
        return {"statusCode": 400}
    request_headers = event["headers"]
    body_as_bytes = base64.b64decode(event["body"])
    body_as_string = body_as_bytes.decode("utf-8")

    # Verify request that it is coming from Slack https://api.slack.com/authentication/verifying-requests-from-slack
    # Check that required headers are present
    slack_cfg = config.SlackConfig()  # type: ignore
    try:
        slack.verify_request(request_headers, slack_cfg.signing_secret, body_as_string)
    except Exception as e:
        logger.exception(e)
        return {"statusCode": 400}

    # Parse payload
    payload = dict(
        urlparse.parse_qsl(base64.b64decode(str(event["body"])).decode("ascii"))
    )  # data comes b64 and also urlencoded name=value& pairs
    actual_payload = json.loads(payload["payload"])

    logger.debug(f"payload:{json.dumps(actual_payload)}")
    try:
        main(actual_payload, cfg, slack_cfg)
    except Exception as e:
        logger.exception(e)
        return {"statusCode": 500}

    return {"statusCode": 200}


def main(payload: dict, cfg: config.Config, slack_cfg: config.SlackConfig):
    if payload["type"] == "shortcut":
        return handle_shortcut(payload, cfg, slack_cfg)

    elif payload["type"] == "view_submission":
        return handle_view_submission(
            RequestForAccessFromSlack.from_view_submission(payload),
            slack_cfg,
            cfg,
        )

    elif payload["type"] == "block_actions":
        return handle_button_click(
            slack.ButtonClickedPayload.parse_obj(payload),
            cfg,
            slack_cfg,
        )
    else:
        logger.error(f"Unsupported type. payload: {payload}")
        raise ValueError(f"Unsupported type. payload: {payload}")


def handle_shortcut(payload: dict, cfg: config.Config, slack_cfg: config.SlackConfig):
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    statements = cfg.get_statements()
    avialable_accounts = config.get_accounts_from_statements(statements, org_client)
    avialable_permission_sets = config.get_permission_sets_from_statements(statements, sso_client, sso_instance.arn)
    inital_form = slack.prepare_initial_form(payload["trigger_id"], avialable_permission_sets, avialable_accounts)
    return slack.post_message("/api/views.open", inital_form, slack_cfg.bot_token)


def handle_button_click(
    payload: slack.ButtonClickedPayload,
    cfg: config.Config,
    slack_cfg: config.SlackConfig,
):
    slack_client = slack.Slack(slack_cfg.bot_token, slack_cfg.channel_id)

    approver = slack_client.get_user_by_id(payload.approver_slack_id)
    if approver is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} not found")
    elif approver.email is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} has no email")

    statements = cfg.get_statements()
    can_be_approved_by = get_approvers(
        statements,
        account_id=payload.account_id,
        permission_set_name=payload.permission_set_name,
    )

    if approver.email not in can_be_approved_by:
        slack_client.post_message(
            text=f"<@{approver.id}> you can not {payload.action} this request",
            thread_ts=payload.thread_ts,
        )
        return {}

    slack.post_message(
        "/api/chat.update",
        slack.prepare_approval_request_update(
            channel=payload.channel_id,
            ts=payload.thread_ts,
            approver=approver.id,
            action=payload.action,
            blocks=payload.message["blocks"],
        ),
        slack_cfg.bot_token,
    )

    if payload.action == "approve":
        slack_client.post_message(text="Updating permissions as requested...", thread_ts=payload.thread_ts)

        requester = slack_client.get_user_by_id(payload.requester_slack_id)
        if requester is None:
            raise ValueError(f"Requester with slack id {payload.requester_slack_id} not found")
        elif requester.email is None:
            raise ValueError(f"Requester with slack id {payload.requester_slack_id} has no email")

        sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)

        permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, payload.permission_set_name)
        if permission_set is None:
            raise ValueError(f"Permission set {payload.permission_set_name} not found")

        user_principal_id = sso.get_user_principal_id_by_email(identity_center_client, sso_instance.identity_store_id, requester.email)
        if user_principal_id is None:
            raise ValueError(f"User with email {requester.email} not found")

        account_assignment = sso.create_account_assignment_and_wait_for_result(
            sso_client,
            sso.UserAccountAssignment(
                instance_arn=sso_instance.arn,
                account_id=payload.account_id,
                permission_set_arn=permission_set.arn,
                user_principal_id=user_principal_id,
            ),
        )

        response = dynamodb.log_operation(
            logger,
            cfg.dynamodb_table_name,
            dynamodb.AuditEntry(
                role_name=payload.permission_set_name,
                account_id=payload.account_id,
                reason=payload.reason,
                requester_slack_id=requester.id,
                requester_email=requester.email,
                request_id=account_assignment.request_id,
                approver_slack_id=payload.approver_slack_id,
                approver_email=approver.email,
                operation_type="grant",
            ),
        )
        logger.debug(response)
        slack_client.post_message(
            thread_ts=payload.thread_ts,
            text="Done",
        )


@dataclass
class RequiresApproval:
    approvers: set


class ApprovalIsNotRequired:
    ...


class SelfApprovalIsAllowedAndRequesterIsApprover:
    ...


DecisionOnRequest = Union[RequiresApproval, ApprovalIsNotRequired, SelfApprovalIsAllowedAndRequesterIsApprover]


def get_affected_statements(statements: list[config.Statement], account_id: str, permission_set_name: str) -> list[config.Statement]:
    return [
        statement
        for statement in statements
        if statement.allows(
            account_id=account_id,
            permission_set_name=permission_set_name,
        )
    ]


def make_decision_on_request(
    statements: list[config.Statement],
    account_id: str,
    permission_set_name: str,
    requester_email: str,
) -> DecisionOnRequest:
    can_be_approved_by = set()
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    for statement in affected_statements:
        if statement.approval_is_not_required:
            return ApprovalIsNotRequired()

        if statement.approvers:
            if statement.allow_self_approval and requester_email in statement.approvers:
                return SelfApprovalIsAllowedAndRequesterIsApprover()

            can_be_approved_by.update(approver for approver in statement.approvers if approver != requester_email)
    return RequiresApproval(approvers=can_be_approved_by)


def get_approvers(statements: list[config.Statement], account_id: str, permission_set_name: str) -> set[str]:
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    can_be_approved_by = set()
    for statement in affected_statements:
        if statement.approvers:
            can_be_approved_by.update(statement.approvers)
    return can_be_approved_by


@dataclass(frozen=True)
class RequestForAccessFromSlack:
    permission_set_name: str
    account_id: str
    reason: str
    user_id: str

    @staticmethod
    def from_view_submission(body: dict) -> "RequestForAccessFromSlack":
        return RequestForAccessFromSlack(
            permission_set_name=body["view"]["state"]["values"]["select_role"]["selected_role"]["selected_option"]["value"],
            account_id=body["view"]["state"]["values"]["select_account"]["selected_account"]["selected_option"]["value"],
            reason=body["view"]["state"]["values"]["provide_reason"]["provided_reason"]["value"],
            user_id=body["user"]["id"],
        )


def handle_view_submission(
    request: RequestForAccessFromSlack,
    slack_cfg: config.SlackConfig,
    cfg: config.Config,
):
    slack_client = slack.Slack(slack_cfg.bot_token, slack_cfg.channel_id)
    requester = slack_client.get_user_by_id(request.user_id)
    if requester is None:
        raise ValueError(f"Requester with slack id {request.user_id} not found")
    elif requester.email is None:
        raise ValueError(f"Requester with slack id {request.user_id} has no email")

    statements = cfg.get_statements()
    decision_on_request = make_decision_on_request(
        statements=statements,
        account_id=request.account_id,
        requester_email=requester.email,
        permission_set_name=request.permission_set_name,
    )

    # show request

    approval_request_kwargs = {
        "channel": slack_cfg.channel_id,
        "requester_slack_id": request.user_id,
        "account_id": request.account_id,
        "role_name": request.permission_set_name,
        "reason": request.reason,
    }
    if isinstance(decision_on_request, RequiresApproval):
        logger.info("RequiresApproval")

        _, slack_response = slack.post_message(
            api_path="/api/chat.postMessage",
            message=slack.prepare_approval_request(**approval_request_kwargs),
            token=slack_cfg.bot_token,
        )

        approvers = [slack_client.get_user_by_email(email) for email in decision_on_request.approvers]
        approvers_slack_ids = [f"<@{approver.id}>" for approver in approvers if approver is not None]

        slack_client.post_message(
            thread_ts=slack_response["ts"],
            text=" ".join(approvers_slack_ids) + " there is a request waiting for the approval",
        )
        return

    elif isinstance(decision_on_request, ApprovalIsNotRequired):
        logger.info("ApprovalIsNotRequired")

        _, slack_response = slack.post_message(
            api_path="/api/chat.postMessage",
            message=slack.prepare_approval_request(**approval_request_kwargs, show_buttons=False),
            token=slack_cfg.bot_token,
        )

        slack_client.post_message(
            thread_ts=slack_response["ts"],
            text="Approval for this Permission Set & Account is not required. Request will be approved automatically.",
        )

        sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
        user_principal_id = sso.get_user_principal_id_by_email(identity_center_client, sso_instance.identity_store_id, requester.email)
        if user_principal_id is None:
            raise ValueError(f"SSO User with email {requester.email} not found")

        permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, request.permission_set_name)
        if permission_set is None:
            raise ValueError(f"Permission set {request.permission_set_name} not found")

        account_assignment = sso.create_account_assignment_and_wait_for_result(
            sso_client,
            sso.UserAccountAssignment(
                instance_arn=sso_instance.arn,
                account_id=request.account_id,
                permission_set_arn=permission_set.arn,
                user_principal_id=user_principal_id,
            ),
        )
        response = dynamodb.log_operation(
            logger,
            cfg.dynamodb_table_name,
            dynamodb.AuditEntry(
                role_name=request.permission_set_name,
                account_id=request.account_id,
                reason=request.reason,
                requester_slack_id=request.user_id,
                requester_email=requester.email,
                request_id=account_assignment.request_id,
                approver_slack_id="ApprovalIsNotRequired",
                approver_email="ApprovalIsNotRequired",
                operation_type="grant",
            ),
        )
        logger.debug(response)

        slack_client.post_message(
            thread_ts=slack_response["ts"],
            text="Done",
        )

    elif isinstance(decision_on_request, SelfApprovalIsAllowedAndRequesterIsApprover):
        logger.info("SelfApprovalIsAllowedAndRequesterIsApprover")

        _, slack_response = slack.post_message(
            api_path="/api/chat.postMessage",
            message=slack.prepare_approval_request(**approval_request_kwargs, show_buttons=False),
            token=slack_cfg.bot_token,
        )

        slack_client.post_message(
            thread_ts=slack_response["ts"],
            text="Self approval is allowed and requester is an approver. Request will be approved automatically.",
        )

        sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
        logger.info(f"SSO Instance: arn:{sso_instance.arn} store_id:{sso_instance.identity_store_id}")
        user_principal_id = sso.get_user_principal_id_by_email(identity_center_client, sso_instance.identity_store_id, requester.email)
        if user_principal_id is None:
            raise ValueError(f"SSO User with email {requester.email} not found")

        permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, request.permission_set_name)
        if permission_set is None:
            raise ValueError(f"Permission set {request.permission_set_name} not found")

        account_assignment = sso.create_account_assignment_and_wait_for_result(
            sso_client,
            sso.UserAccountAssignment(
                instance_arn=sso_instance.arn,
                account_id=request.account_id,
                permission_set_arn=permission_set.arn,
                user_principal_id=user_principal_id,
            ),
        )
        response = dynamodb.log_operation(
            logger,
            cfg.dynamodb_table_name,
            dynamodb.AuditEntry(
                role_name=request.permission_set_name,
                account_id=request.account_id,
                reason=request.reason,
                requester_slack_id=request.user_id,
                requester_email=requester.email,
                request_id=account_assignment.request_id,
                approver_slack_id=request.account_id,
                approver_email=requester.email,
                operation_type="SelfApproveGrant",
            ),
        )
        logger.debug(response)

        slack_client.post_message(
            thread_ts=slack_response["ts"],
            text="Done",
        )
