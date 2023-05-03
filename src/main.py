import copy
import datetime
import functools

import boto3
from aws_lambda_powertools import Logger
from slack_bolt import Ack, App, BoltContext
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

import access_control
import config
import dynamodb
import entities
import errors
import organizations
import schedule
import slack
import sso

logger = config.get_logger(service="main")

session = boto3.Session()
org_client = session.client("organizations")  # type: ignore
sso_client = session.client("sso-admin")  # type: ignore
identitystore_client = session.client("identitystore")  # type: ignore
schedule_client = session.client("scheduler")  # type: ignore

cfg = config.get_config()
app = App(
    process_before_response=True,
    logger=config.get_logger(service="slack", level=cfg.slack_app_log_level),
)


def lambda_handler(event, context):
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def error_handler(client: WebClient, e: Exception, logger: Logger, context: BoltContext):
    logger.exception(e)
    if isinstance(e, errors.ConfigurationError):
        text = f"<@{context['user_id']}> Your request for AWS permissions failed with error: {e}. Check logs for more details."
        client.chat_postMessage(text=text, channel=cfg.slack_channel_id)


def handle_errors(fn):
    # Default slack error handler (app.error) does not handle all exceptions. Or at least I did not find how to do it.
    # So I created this error handler.
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            client: WebClient = kwargs["client"]
            context: BoltContext = kwargs["context"]
            error_handler(client=client, e=e, logger=logger, context=context)

    return wrapper


trigger_view_map = {}
# To update the view, it is necessary to know the view_id. It is returned when the view is opened.
# But shortcut 'request_for_access' handled by two functions. The first one opens the view and the second one updates it.
# So we need to store the view_id somewhere. Since the trigger_id is unique for each request,
# and available in both functions, we can use it as a key. The value is the view_id.


def show_initial_form(client: WebClient, body: dict, ack: Ack):
    ack()
    logger.info("Showing initial form")
    logger.debug("Request body", extra={"body": body})
    trigger_id = body["trigger_id"]
    response = client.views_open(trigger_id=trigger_id, view=slack.RequestForAccessView.build())
    trigger_view_map[trigger_id] = response.data["view"]["id"]  # type: ignore
    return response


def load_select_options(client: WebClient, body: dict):
    logger.info("Loading select options for view (accounts and permission sets)")
    logger.debug("Request body", extra={"body": body})
    if "*" in cfg.accounts:
        accounts = organizations.list_accounts(org_client)
    else:
        accounts = [ac for ac in organizations.list_accounts(org_client) if ac.id in cfg.accounts]

    if "*" in cfg.permission_sets:
        permission_sets = list(sso.list_permission_sets(sso_client, cfg.sso_instance_arn))
    else:
        permission_sets = [ps for ps in sso.list_permission_sets(sso_client, cfg.sso_instance_arn) if ps.name in cfg.permission_sets]

    trigger_id = body["trigger_id"]

    view = slack.RequestForAccessView.update_with_accounts_and_permission_sets(accounts=accounts, permission_sets=permission_sets)
    return client.views_update(view_id=trigger_view_map[trigger_id], view=view)


app.shortcut("request_for_access")(
    show_initial_form,
    load_select_options,
)


@handle_errors
def handle_button_click(body: dict, client: WebClient, context: BoltContext):
    logger.info("Handling button click")
    payload = slack.ButtonClickedPayload.parse_obj(body)
    logger.info("Button click payload", extra={"payload": payload})
    approver = slack.get_user(client, id=payload.approver_slack_id)
    requester = slack.get_user(client, id=payload.request.requester_slack_id)

    decision = access_control.make_decision_on_approve_request(
        cfg.statements,
        account_id=payload.request.account_id,
        permission_set_name=payload.request.permission_set_name,
        approver_email=approver.email,
        requester_email=requester.email,
    )
    logger.info("Decision on request was made", extra={"decision": decision})

    if not decision.permit:
        return client.chat_postMessage(
            channel=payload.channel_id,
            text=f"<@{approver.id}> you can not {payload.action} this request",
            thread_ts=payload.thread_ts,
        )

    blocks = copy.deepcopy(payload.message["blocks"])
    blocks = slack.remove_blocks(blocks, block_ids=["buttons"])
    blocks.append(slack.button_click_info_block(payload.action, approver.id))
    client.chat_update(
        channel=payload.channel_id,
        ts=payload.thread_ts,
        blocks=blocks,
    )
    if payload.action == "deny":
        return

    client.chat_postMessage(
        channel=payload.channel_id,
        text="Updating permissions as requested...",
        thread_ts=payload.thread_ts,
    )

    handle_account_assignment(
        permission_set_name=payload.request.permission_set_name,
        account_id=payload.request.account_id,
        permission_duration=payload.request.permission_duration,
        approver=approver,
        requester=requester,
        reason=payload.request.reason,
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
    lazy=[handle_button_click],
)

app.action("deny")(
    ack=acknowledge_request,
    lazy=[handle_button_click],
)


@handle_errors
def handle_request_for_access_submittion(body: dict, ack: Ack, client: WebClient, logger: Logger, context: BoltContext):
    logger.info("Handling request for access submittion")
    request = slack.RequestForAccessView.parse(body)
    logger.info("View submitted", extra={"view": request})
    requester = slack.get_user(client, id=request.requester_slack_id)
    decision = access_control.make_decision_on_access_request(
        cfg.statements,
        account_id=request.account_id,
        permission_set_name=request.permission_set_name,
        requester_email=requester.email,
    )
    logger.info("Decision on request was made", extra={"decision": decision})

    account = organizations.describe_account(org_client, request.account_id)
    slack_response = client.chat_postMessage(
        blocks=slack.build_approval_request_message_blocks(
            requester_slack_id=request.requester_slack_id,
            account=account,
            role_name=request.permission_set_name,
            reason=request.reason,
            permission_duration=request.permission_duration,
            show_buttons=bool(decision.approvers),
        ),
        channel=cfg.slack_channel_id,
    )
    match decision.reason:
        case access_control.DecisionReason.ApprovalNotRequired:
            text = "Approval for this Permission Set & Account is not required. Request will be approved automatically."
        case access_control.DecisionReason.SelfApproval:
            text = "Self approval is allowed and requester is an approver. Request will be approved automatically."
        case access_control.DecisionReason.RequiresApproval:
            approvers = [slack.get_user_by_email(client, email) for email in decision.approvers]
            mention_approvers = " ".join(f"<@{approver.id}>" for approver in approvers)
            text = f"{mention_approvers} there is a request waiting for the approval."
        case access_control.DecisionReason.NoApprovers:
            text = "Nobody can approve this request."
        case access_control.DecisionReason.NoStatements:
            text = "There are no statements for this Permission Set & Account."

    client.chat_postMessage(text=text, thread_ts=slack_response["ts"], channel=cfg.slack_channel_id)
    if not decision.grant:
        return

    handle_account_assignment(
        permission_set_name=request.permission_set_name,
        account_id=request.account_id,
        permission_duration=request.permission_duration,
        approver=requester,
        requester=requester,
        reason=request.reason,
    )
    return client.chat_postMessage(
        channel=cfg.slack_channel_id,
        text=f"Permissions granted to <@{requester.id}>",
        thread_ts=slack_response["ts"],
    )


app.view(slack.RequestForAccessView.CALLBACK_ID)(
    ack=acknowledge_request,
    lazy=[handle_request_for_access_submittion],
)


@app.action("timepickeraction")
def handle_timepickeraction(ack):
    ack()


def handle_account_assignment(
    permission_set_name: str,
    account_id: str,
    permission_duration: datetime.timedelta,
    approver: entities.slack.User,
    requester: entities.slack.User,
    reason: str,
):
    logger.info("Handling account assignment")
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    permission_set = sso.get_permission_set_by_name(sso_client, sso_instance.arn, permission_set_name)
    user_principal_id = sso.get_user_principal_id_by_email(identitystore_client, sso_instance.identity_store_id, requester.email)
    account_assignment = sso.UserAccountAssignment(
        instance_arn=sso_instance.arn,
        account_id=account_id,
        permission_set_arn=permission_set.arn,
        user_principal_id=user_principal_id,
    )
    logger.info("Creating account assignment", extra={"account_assignment": account_assignment})
    account_assignment_status = sso.create_account_assignment_and_wait_for_result(
        sso_client,
        account_assignment,
    )

    dynamodb.log_operation(
        table_name=cfg.dynamodb_table_name,
        audit_entry=dynamodb.AuditEntry(
            account_id=account_id,
            role_name=permission_set.name,
            reason=reason,
            requester_slack_id=requester.id,
            requester_email=requester.email,
            approver_slack_id=approver.id,
            approver_email=approver.email,
            request_id=account_assignment_status.request_id,
            operation_type="grant",
        ),
    )

    schedule.schedule_revoke_event(
        time_delta=permission_duration,
        schedule_client=schedule_client,
        approver=approver,
        requester=requester,
        user_account_assignment=sso.UserAccountAssignment(
            instance_arn=sso_instance.arn,
            account_id=account_id,
            permission_set_arn=permission_set.arn,
            user_principal_id=user_principal_id,
        ),
    )
