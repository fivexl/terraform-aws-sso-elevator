import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Union
from aws_lambda_powertools.utilities.parser.pydantic import BaseModel, root_validator

import boto3
from aws_lambda_powertools import Logger
import jmespath as jp
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

import config
import dynamodb
import slack
import sso
import organizations

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

session = boto3.Session()
org_client = session.client("organizations")  # type: ignore
sso_client = session.client("sso-admin")  # type: ignore
identity_center_client = session.client("identitystore")  # type: ignore
schedule_client = session.client("scheduler")  # type: ignore

cfg = config.Config()  # type: ignore
slack_cfg = config.SlackConfig()  # type: ignore
slack_client = slack.Slack(slack_cfg.bot_token, slack_cfg.channel_id)

app = App(process_before_response=True, logger=logger)


def acknowledge_request(ack):
    ack()


def lambda_handler(event, context):
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def handle_request_for_access(body, client: WebClient):
    configured_accounts = cfg.get_configured_accounts()
    if "*" in configured_accounts:
        accounts = organizations.list_accounts(org_client)
    else:
        accounts = [ac for ac in organizations.list_accounts(org_client) if ac.id in configured_accounts]

    configured_permission_sets = cfg.get_configured_permission_sets()
    if "*" in configured_permission_sets:
        permission_sets = sso.list_permission_sets(sso_client, cfg.sso_instance_arn)
    else:
        permission_sets = [ps for ps in sso.list_permission_sets(sso_client, cfg.sso_instance_arn) if ps.name in configured_permission_sets]

    inital_form = slack.prepare_initial_form(body["trigger_id"], list(permission_sets), accounts)
    return client.views_open(**inital_form)


app.shortcut("request_for_access")(
    acknowledge_request,
    handle_request_for_access,
)


def handle_button_click(
    payload: slack.ButtonClickedPayload,
    approver: slack.Slack.User,
) -> bool:
    can_be_approved_by = get_approvers(
        cfg.statements,
        account_id=payload.account_id,
        permission_set_name=payload.permission_set_name,
    )

    if approver.email not in can_be_approved_by:
        slack_client.post_message(
            text=f"<@{approver.id}> you can not {payload.action} this request",
            thread_ts=payload.thread_ts,
        )
        return False

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
    return True


def handle_approve(body):
    payload = slack.ButtonClickedPayload.parse_obj(body)

    approver = slack_client.get_user_by_id(payload.approver_slack_id)
    if approver is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} not found")
    elif approver.email is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} has no email")
    if not handle_button_click(payload, approver):
        return

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

    account_assignment = sso.create_account_assignment_and_wait_for_result(
        sso_client,
        sso.UserAccountAssignment(
            instance_arn=sso_instance.arn,
            account_id=payload.account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=user_principal_id,
        ),
    )
    # schedule.create_schedule_for_revoker(
    #     lambda_arn=cfg.revoker_function_arn,
    #     lambda_name = cfg.revoker_function_name,
    #     time_delta = time_delta,
    #     schedule_client = schedule_client,
    #     sso_instance_arn = sso_instance.arn,
    #     account_id = payload.account_id,
    #     permission_set_arn = permission_set.arn,
    #     user_principal_id = user_principal_id,
    #     requester_slack_id= requester.id,
    #     requester_email = requester.email,
    #     approver_slack_id = payload.approver_slack_id,
    #     approver_email = approver.email,
    # )
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


app.action("approve")(
    ack=acknowledge_request,
    lazy=[handle_approve],
)


def handle_deny(body, logger):
    logger.info(body)
    payload = slack.ButtonClickedPayload.parse_obj(body)

    approver = slack_client.get_user_by_id(payload.approver_slack_id)
    if approver is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} not found")
    elif approver.email is None:
        raise ValueError(f"Approver with slack id {payload.approver_slack_id} has no email")
    handle_button_click(payload, approver)


app.action("deny")(
    ack=acknowledge_request,
    lazy=[handle_deny],
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


class RequestForAccess(BaseModel):
    permission_set_name: str
    account_id: str
    reason: str
    user_id: str

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        return {
            "permission_set_name": jp.search("view.state.values.select_role.selected_role.selected_option.value", values),
            "account_id": jp.search("view.state.values.select_account.selected_account.selected_option.value", values),
            "reason": jp.search("view.state.values.provide_reason.provided_reason.value", values),
            "user_id": jp.search("user.id", values),
        }

    class Config:
        frozen = True


def handle_request_for_access_submittion(ack, body):
    ack()
    request = RequestForAccess.parse_obj(body)

    requester = slack_client.get_user_by_id(request.user_id)
    if requester is None:
        raise ValueError(f"Requester with slack id {request.user_id} not found")
    elif requester.email is None:
        raise ValueError(f"Requester with slack id {request.user_id} has no email")

    decision_on_request = make_decision_on_request(
        statements=cfg.statements,
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
        # schedule.create_schedule_for_revoker(
        #     lambda_arn=cfg.revoker_function_arn,
        #     lambda_name = cfg.revoker_function_name,
        #     time_delta = time_delta,
        #     schedule_client = schedule_client,
        #     sso_instance_arn = sso_instance.arn,
        #     account_id = request.account_id,
        #     permission_set_arn = permission_set.arn,
        #     user_principal_id = user_principal_id,
        #     requester_slack_id = request.user_id,
        #     requester_email = requester.email,
        #     approver_slack_id="ApprovalIsNotRequired",
        #     approver_email="ApprovalIsNotRequired",
        # )
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

        # schedule.create_schedule_for_revoker(
        #     lambda_arn=cfg.revoker_function_arn,
        #     lambda_name = cfg.revoker_function_name,
        #     time_delta = time_delta,
        #     schedule_client = schedule_client,
        #     sso_instance_arn = sso_instance.arn,
        #     account_id = request.account_id,
        #     permission_set_arn = permission_set.arn,
        #     user_principal_id = user_principal_id,
        #     requester_slack_id = request.user_id,
        #     requester_email = requester.email,
        #     approver_slack_id = request.user_id,
        #     approver_email = requester.email,
        # )

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


app.view("request_for_access_submitted")(
    ack=acknowledge_request,
    lazy=[handle_request_for_access_submittion],
)

if __name__ == "__main__":
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    SocketModeHandler(app, logger=logger).start()
