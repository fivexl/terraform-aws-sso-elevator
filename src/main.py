import copy
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Union

import boto3
import jmespath as jp
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.parser.pydantic import BaseModel, root_validator
from slack_bolt import Ack, App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

import config
import dynamodb
import organizations
import schedule
import slack
import sso

log_level = os.environ.get("LOG_LEVEL", "DEBUG")
logger = Logger(level=log_level)

session = boto3.Session()
org_client = session.client("organizations")  # type: ignore
sso_client = session.client("sso-admin")  # type: ignore
identity_center_client = session.client("identitystore")  # type: ignore
schedule_client = session.client("scheduler")  # type: ignore

cfg = config.Config()  # type: ignore
app = App(process_before_response=True, logger=logger)


def lambda_handler(event, context):
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


trigger_view_map = {}
# To update the view, it is necessary to know the view_id. It is returned when the view is opened.
# But shortcut 'request_for_access' handled by two functions. The first one opens the view and the second one updates it.
# So we need to store the view_id somewhere. Since the trigger_id is unique for each request,
# and available in both functions, we can use it as a key. The value is the view_id.


def show_initial_form(client: WebClient, body: dict, ack: Ack):
    ack()
    trigger_id = body["trigger_id"]
    response = client.views_open(trigger_id=trigger_id, view=slack.SLACK_REQUEST_FOR_ACCESS_FORM)
    trigger_view_map[trigger_id] = response.data["view"]["id"]  # type: ignore
    return response


def load_select_options(client: WebClient, body: dict):
    if "*" in cfg.accounts:
        accounts = organizations.list_accounts(org_client)
    else:
        accounts = [ac for ac in organizations.list_accounts(org_client) if ac.id in cfg.accounts]

    if "*" in cfg.permission_sets:
        permission_sets = list(sso.list_permission_sets(sso_client, cfg.sso_instance_arn))
    else:
        permission_sets = [ps for ps in sso.list_permission_sets(sso_client, cfg.sso_instance_arn) if ps.name in cfg.permission_sets]

    trigger_id = body["trigger_id"]

    view = copy.deepcopy(slack.SLACK_REQUEST_FOR_ACCESS_FORM)
    blocks = slack.remove_blocks(view.blocks, block_ids=["loading"])
    view.blocks = slack.insert_blocks(
        blocks=blocks,
        blocks_to_insert=[
            slack.select_account_input_block(accounts),
            slack.select_permission_set_input_block(permission_sets),
        ],  # type: ignore
        after_block_id="provide_reason",
    )
    return client.views_update(view_id=trigger_view_map[trigger_id], view=view)


app.shortcut("request_for_access")(
    show_initial_form,
    load_select_options,
)


def handle_button_click(
    client: WebClient, payload: slack.ButtonClickedPayload, approver: slack.SlackUser, requester: slack.SlackUser
) -> bool:
    can_be_approved_by = get_approvers(
        cfg.statements,
        account_id=payload.account_id,
        permission_set_name=payload.permission_set_name,
        requester_email=requester.email,
    )

    if approver.email not in can_be_approved_by:
        client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not {payload.action} this request",
            thread_ts=payload.thread_ts,
        )
        return False
    blocks = copy.deepcopy(payload.message["blocks"])
    blocks = slack.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack.button_click_info_block(payload.action, approver.id))
    client.chat_update(
        channel=payload.channel_id,
        ts=payload.thread_ts,
        blocks=blocks,
    )
    return True


def handle_approve(client: WebClient, body: dict):
    payload = slack.ButtonClickedPayload.parse_obj(body)

    approver = slack.get_user(client, id=payload.approver_slack_id)
    requester = slack.get_user(client, id=payload.requester_slack_id)
    if not handle_button_click(client, payload, approver, requester):
        return
    client.chat_postMessage(
        channel=payload.channel_id,
        text="Updating permissions as requested...",
        thread_ts=payload.thread_ts,
    )

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
    if account_assignment.status == "FAILED":
            client.chat_postMessage(
                channel=payload.channel_id,
                text=f"Unable to update permissions:  {account_assignment.failure_reason}",
                thread_ts=payload.thread_ts,
            )
            raise Exception(f"Account assignment failed: {account_assignment.failure_reason}")
    
    schedule.create_schedule_for_revoker(
        time_delta=payload.permission_duration,
        schedule_client=schedule_client,
        account_id=payload.account_id,
        permission_set_arn=permission_set.arn,
        user_principal_id=user_principal_id,
        requester_slack_id=requester.id,
        requester_email=requester.email,
        approver_slack_id=payload.approver_slack_id,
        approver_email=approver.email,
    )
    dynamodb.log_operation(
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
    return client.chat_postMessage(
        channel=payload.channel_id,
        text=f"Permissions granted to <@{requester.id}>",
        thread_ts=payload.thread_ts,
    )


def acknowledge_request(ack: Ack):
    ack()


app.action("approve")(
    ack=acknowledge_request,
    lazy=[handle_approve],
)


def handle_deny(client: WebClient, body: dict, logger: Logger):
    logger.info(body)
    payload = slack.ButtonClickedPayload.parse_obj(body)
    requester = slack.get_user(client, id=payload.requester_slack_id)
    approver = slack.get_user(client, id=payload.approver_slack_id)
    handle_button_click(client, payload, approver=approver, requester=requester)


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


def get_affected_statements(statements: frozenset[config.Statement], account_id: str, permission_set_name: str) -> list[config.Statement]:
    return [
        statement
        for statement in statements
        if statement.allows(
            account_id=account_id,
            permission_set_name=permission_set_name,
        )
    ]


def make_decision_on_request(
    statements: frozenset[config.Statement],
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


def get_approvers(statements: frozenset[config.Statement], account_id: str, permission_set_name: str, requester_email: str) -> set[str]:
    affected_statements = get_affected_statements(statements, account_id, permission_set_name)
    can_be_approved_by = set()
    for statement in affected_statements:
        if statement.approvers:
            if requester_email in statement.approvers:
                if not statement.allow_self_approval:
                    can_be_approved_by.update(statement.approvers - {requester_email})
            else:
                can_be_approved_by.update(statement.approvers)
    return can_be_approved_by


class RequestForAccess(BaseModel):
    permission_set_name: str
    account_id: str
    reason: str
    user_id: str
    permission_duration: timedelta

    @root_validator(pre=True)
    def validate_payload(cls, values: dict):
        hhmm = jp.search("view.state.values.timepicker.timepickeraction.selected_time", values)
        return {
            "permission_duration": slack.timepicker_str_to_timedelta(hhmm),
            "permission_set_name": jp.search(
                "view.state.values.select_permission_set.selected_permission_set.selected_option.value", values
            ),
            "account_id": jp.search("view.state.values.select_account.selected_account.selected_option.value", values),
            "reason": jp.search("view.state.values.provide_reason.provided_reason.value", values),
            "user_id": jp.search("user.id", values),
        }

    class Config:
        frozen = True


def handle_request_for_access_submittion(client: WebClient, body: dict, ack: Ack):
    ack()
    request = RequestForAccess.parse_obj(body)
    requester = slack.get_user(client, id=request.user_id)
    decision_on_request = make_decision_on_request(
        statements=cfg.statements,
        account_id=request.account_id,
        requester_email=requester.email,
        permission_set_name=request.permission_set_name,
    )
    account = organizations.describe_account(org_client, request.account_id)
    approval_request_kwargs = {
        "requester_slack_id": request.user_id,
        "account": account,
        "role_name": request.permission_set_name,
        "reason": request.reason,
        "permission_duration": request.permission_duration,
    }
    if isinstance(decision_on_request, RequiresApproval):
        logger.info("RequiresApproval")
        slack_response = client.chat_postMessage(
            blocks=slack.prepare_approval_request_blocks(**approval_request_kwargs),
            channel=cfg.slack_channel_id,
        )
        approvers = [slack.get_user_by_email(client, email) for email in decision_on_request.approvers]
        approvers_slack_ids = [f"<@{approver.id}>" for approver in approvers]
        text = (
            " ".join(approvers_slack_ids) + " there is a request waiting for the approval"
            if approvers_slack_ids
            else "Nobody can approve this request."
        )

        return client.chat_postMessage(
            text=text,
            thread_ts=slack_response["ts"],
            channel=cfg.slack_channel_id,
        )

    elif isinstance(decision_on_request, ApprovalIsNotRequired):
        logger.info("ApprovalIsNotRequired")
        slack_response = client.chat_postMessage(
            blocks=slack.prepare_approval_request_blocks(**approval_request_kwargs, show_buttons=False),
            channel=cfg.slack_channel_id,
        )
        client.chat_postMessage(
            text="Approval for this Permission Set & Account is not required. Request will be approved automatically.",
            thread_ts=slack_response["ts"],
            channel=cfg.slack_channel_id,
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

        if account_assignment.status == "FAILED":
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"Unable to update permissions:  {account_assignment.failure_reason}",
                thread_ts=slack_response["ts"],
            )
            raise Exception(f"Account assignment failed: {account_assignment.failure_reason}")

        schedule.create_schedule_for_revoker(
            time_delta=request.permission_duration,
            schedule_client=schedule_client,
            account_id=request.account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=user_principal_id,
            requester_slack_id=request.user_id,
            requester_email=requester.email,
            approver_slack_id="ApprovalIsNotRequired",
            approver_email="ApprovalIsNotRequired",
        )
        dynamodb.log_operation(
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
        return client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=f"Permissions granted to <@{requester.id}>",
            thread_ts=slack_response["ts"],
        )

    elif isinstance(decision_on_request, SelfApprovalIsAllowedAndRequesterIsApprover):
        logger.info("SelfApprovalIsAllowedAndRequesterIsApprover")
        slack_response = client.chat_postMessage(
            blocks=slack.prepare_approval_request_blocks(**approval_request_kwargs, show_buttons=False),
            channel=cfg.slack_channel_id,
        )
        client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text="Self approval is allowed and requester is an approver. Request will be approved automatically.",
            thread_ts=slack_response["ts"],
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

        if account_assignment.status == "FAILED":
            client.chat_postMessage(
                channel=cfg.slack_channel_id,
                text=f"Unable to update permissions:  {account_assignment.failure_reason}",
                thread_ts=slack_response["ts"],
            )
            raise Exception(f"Account assignment failed: {account_assignment.failure_reason}")

        schedule.create_schedule_for_revoker(
            time_delta=request.permission_duration,
            schedule_client=schedule_client,
            account_id=request.account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=user_principal_id,
            requester_slack_id=request.user_id,
            requester_email=requester.email,
            approver_slack_id=request.user_id,
            approver_email=requester.email,
        )
        dynamodb.log_operation(
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
        return client.chat_postMessage(
            channel=cfg.slack_channel_id,
            text=f"Permissions granted to <@{requester.id}>",
            thread_ts=slack_response["ts"],
        )


app.view("request_for_access_submitted")(
    ack=acknowledge_request,
    lazy=[handle_request_for_access_submittion],
)
