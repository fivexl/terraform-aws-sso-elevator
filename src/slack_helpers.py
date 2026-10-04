import datetime
import json
import time
from datetime import timedelta, timezone
from typing import Annotated, Literal, Optional, TypeVar, Union

import jmespath as jp
import slack_sdk.errors
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_sso_admin import SSOAdminClient
from pydantic import Field, TypeAdapter, ValidationError, model_validator
from slack_sdk import WebClient
from slack_sdk.models.blocks import (
    Block,
    DividerBlock,
    InputBlock,
    MarkdownTextObject,
    Option,
    PlainTextInputElement,
    PlainTextObject,
    SectionBlock,
    StaticSelectElement,
)
from slack_sdk.models.views import View

import access_control
import config
import entities
import sso
from entities import BaseModel
from errors import PostGrantError, ShownOnRequest

# ruff: noqa: ANN102, PGH003

logger = config.get_logger(service="slack")
cfg = config.get_config()


class RequestForAccess(BaseModel):
    kind: Literal["account"] = "account"
    permission_set_name: str
    account_id: str
    # Display-only, set once the account is described at intake; it rides in the
    # button value so later states can render the title without another AWS call.
    account_name: str = ""
    reason: str
    requester_slack_id: str
    permission_duration: timedelta
    # Which intake path this came through; for "cli", the SigV4-verified ARN
    # (audit only), plus the Identity Store UserId and email verified at
    # submission. execute_decision grants against verified_user_id and approval
    # re-checks eligibility against verified_email, so neither drifts with the
    # requester's Slack profile. "NA" for "slack" requests.
    request_source: Literal["slack", "cli"] = "slack"
    verified_arn: str = "NA"
    verified_user_id: str = "NA"
    verified_email: str = "NA"


class RequestForAccessView:
    __name__ = "RequestForAccountAccessView"
    CALLBACK_ID = "request_for__account_access_submitted"

    REASON_BLOCK_ID = "provide_reason"
    REASON_ACTION_ID = "provided_reason"

    ACCOUNT_BLOCK_ID = "select_account"
    ACCOUNT_ACTION_ID = "selected_account"

    PERMISSION_SET_BLOCK_ID = "select_permission_set"
    PERMISSION_SET_ACTION_ID = "selected_permission_set"

    DURATION_BLOCK_ID = "duration_picker"
    DURATION_ACTION_ID = "duration_picker_action"

    LOADING_BLOCK_ID = "loading"

    @classmethod
    def build(cls) -> View:
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            submit=PlainTextObject(text="Request"),
            close=PlainTextObject(text="Cancel"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[
                SectionBlock(text=MarkdownTextObject(text=":wave: Hey! Please fill form below to request AWS access.")),
                DividerBlock(),
                SectionBlock(
                    block_id=cls.DURATION_BLOCK_ID,
                    text=MarkdownTextObject(text="Select the duration for which the authorization will be provided"),
                    accessory=StaticSelectElement(
                        action_id=cls.DURATION_ACTION_ID,
                        initial_option=get_max_duration_block(cfg)[0],
                        options=get_max_duration_block(cfg),
                        placeholder=PlainTextObject(text="Select duration"),
                    ),
                ),
                InputBlock(
                    block_id=cls.REASON_BLOCK_ID,
                    label=PlainTextObject(text="Why do you need access?"),
                    element=PlainTextInputElement(
                        action_id=cls.REASON_ACTION_ID,
                        placeholder=PlainTextObject(text="Reason will be saved in audit logs. Please be specific."),
                        multiline=True,
                        max_length=REASON_MAX_LENGTH,
                    ),
                ),
                DividerBlock(),
                SectionBlock(
                    text=MarkdownTextObject(
                        text="Remember to use access responsibly. All actions (AWS API calls) are being recorded.",
                    ),
                ),
                SectionBlock(
                    block_id=cls.LOADING_BLOCK_ID,
                    text=MarkdownTextObject(
                        text=":hourglass: Loading available accounts and permission sets...",
                    ),
                ),
            ],
        )

    @classmethod
    def build_select_account_input_block(cls, accounts: list[entities.aws.Account]) -> InputBlock:
        # TODO: handle case when there are more than 100 accounts
        # 99 is the limit for StaticSelectElement
        # https://slack.dev/python-slack-sdk/api-docs/slack_sdk/models/blocks/block_elements.html#:~:text=StaticSelectElement(InputInteractiveElement)%3A%0A%20%20%20%20type%20%3D%20%22static_select%22-,options_max_length%20%3D%20100,-option_groups_max_length%20%3D%20100%0A%0A%20%20%20%20%40property%0A%20%20%20%20def%20attributes(
        if len(accounts) > 99:  # noqa: PLR2004
            accounts = accounts[:99]
        sorted_accounts = sorted(accounts, key=lambda account: account.name)
        return InputBlock(
            block_id=cls.ACCOUNT_BLOCK_ID,
            label=PlainTextObject(text="Select account"),
            element=StaticSelectElement(
                action_id=cls.ACCOUNT_ACTION_ID,
                placeholder=PlainTextObject(text="Select account"),
                options=[
                    Option(text=PlainTextObject(text=f"{account.id} - {account.name}"), value=account.id) for account in sorted_accounts
                ],
            ),
        )

    @classmethod
    def build_select_permission_set_input_block(cls, permission_sets: list[entities.aws.PermissionSet]) -> InputBlock:
        sorted_permission_sets = sorted(permission_sets, key=lambda permission_set: permission_set.name)
        return InputBlock(
            block_id=cls.PERMISSION_SET_BLOCK_ID,
            label=PlainTextObject(text="Select permission set"),
            element=StaticSelectElement(
                action_id=cls.PERMISSION_SET_ACTION_ID,
                placeholder=PlainTextObject(text="Select permission set"),
                options=[
                    Option(text=PlainTextObject(text=permission_set.name), value=permission_set.name)
                    for permission_set in sorted_permission_sets
                ],
            ),
        )

    @classmethod
    def build_no_available_options_view(cls, message: str) -> View:
        # No submit button: there is nothing the user could request.
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            close=PlainTextObject(text="Close"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[SectionBlock(text=MarkdownTextObject(text=message))],
        )

    @classmethod
    def update_with_accounts_and_permission_sets(
        cls, accounts: list[entities.aws.Account], permission_sets: list[entities.aws.PermissionSet]
    ) -> View:
        view = cls.build()
        view.blocks = remove_blocks(view.blocks, block_ids=[cls.LOADING_BLOCK_ID])
        view.blocks = insert_blocks(
            blocks=view.blocks,
            blocks_to_insert=[
                cls.build_select_account_input_block(accounts),
                cls.build_select_permission_set_input_block(permission_sets),
            ],
            after_block_id=cls.REASON_BLOCK_ID,
        )
        return view

    @classmethod
    def parse(cls, obj: dict) -> RequestForAccess:
        values = jp.search("view.state.values", obj)
        hhmm = jp.search(f"{cls.DURATION_BLOCK_ID}.{cls.DURATION_ACTION_ID}.selected_option.value", values)
        hours, minutes = map(int, hhmm.split(":"))
        duration = timedelta(hours=hours, minutes=minutes)
        return RequestForAccess.model_validate(
            {
                "permission_duration": duration,
                "permission_set_name": jp.search(
                    f"{cls.PERMISSION_SET_BLOCK_ID}.{cls.PERMISSION_SET_ACTION_ID}.selected_option.value", values
                ),
                "account_id": jp.search(f"{cls.ACCOUNT_BLOCK_ID}.{cls.ACCOUNT_ACTION_ID}.selected_option.value", values),
                "reason": jp.search(f"{cls.REASON_BLOCK_ID}.{cls.REASON_ACTION_ID}.value", values),
                "requester_slack_id": jp.search("user.id", obj),
            }
        )


T = TypeVar("T", Block, dict)


def get_block_id(block: Union[Block, dict]) -> Optional[str]:
    return block["block_id"] if isinstance(block, dict) else block.block_id


def remove_blocks(blocks: list[T], block_ids: list[str]) -> list[T]:
    return [block for block in blocks if get_block_id(block) not in block_ids]


def insert_blocks(blocks: list[T], blocks_to_insert: list[Block], after_block_id: str) -> list[T]:
    index = next(i for i, block in enumerate(blocks) if get_block_id(block) == after_block_id)
    return blocks[: index + 1] + blocks_to_insert + blocks[index + 1 :]  # type: ignore


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack's mrkdwn treats specially. `&` goes
    first, or the `&` in `&lt;`/`&gt;` would be escaped a second time."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


REASON_MAX_LENGTH = 1000
REASON_TOO_LONG = f"Reason must be {REASON_MAX_LENGTH} characters or fewer"
# The whole request rides as JSON in the buttons' value, which Slack caps at 2000 characters.
_BUTTON_VALUE_MAX_LENGTH = 2000
REQUEST_TOO_LARGE = "Request is too large for Slack; shorten the reason"


def request_rejection(request: "RequestForAccess | RequestForGroupAccess") -> str | None:
    """Why the request cannot be posted, or None. Check it once account_name/group_name are set."""
    if len(request.reason) > REASON_MAX_LENGTH:
        return REASON_TOO_LONG
    if len(request.model_dump_json()) > _BUTTON_VALUE_MAX_LENGTH:
        return REQUEST_TOO_LARGE
    return None


def dm_rejection(client: WebClient, requester_slack_id: str, rejection: str) -> None:
    """The modal closed on submit, so a DM is all that is left to tell the requester."""
    logger.info(f"Rejected access request: {rejection}", extra={"requester_slack_id": requester_slack_id})
    client.chat_postMessage(channel=requester_slack_id, text=f"Your access request wasn't submitted: {rejection}.")


def format_duration(td: timedelta) -> str:
    days, minutes = divmod(int(td.total_seconds()) // 60, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} day{'s' * (days != 1)}"] if days else []
    if hours:
        parts.append(f"{hours} hour{'s' * (hours != 1)}")
    if minutes:
        parts.append(f"{minutes} min")
    return " ".join(parts) or "0 min"


def slack_time(moment: datetime.datetime) -> str:
    """Rendered by Slack in each viewer's own timezone; the fallback is UTC."""
    return f"<!date^{int(moment.timestamp())}^{{time}}|{moment.astimezone(timezone.utc):%H:%M} UTC>"


def account_subject(permission_set_name: str, account_name: str, account_id: str) -> str:
    return f"{escape_mrkdwn(permission_set_name)} → {escape_mrkdwn(account_name)} #{account_id}"


def group_subject(group_name: str) -> str:
    return f"group {escape_mrkdwn(group_name)}"


def request_subject(request: "RequestForAccess | RequestForGroupAccess") -> str:
    if isinstance(request, RequestForGroupAccess):
        return group_subject(request.group_name)
    return account_subject(request.permission_set_name, request.account_name, request.account_id)


def approved_by(approver_slack_id: str) -> str:
    return f"Approved by <@{approver_slack_id}>"


AUTO_APPROVAL_LABELS = {
    access_control.DecisionReason.SelfApproval: "Self-approval allowed",
    access_control.DecisionReason.ApprovalNotRequired: "Approval not required",
}

_SECTION_TEXT_LIMIT = 3000


def _section(block_id: str, text: str) -> dict:
    return {"type": "section", "block_id": block_id, "text": {"type": "mrkdwn", "text": text}}


def _context(block_id: str, text: str) -> dict:
    return {"type": "context", "block_id": block_id, "elements": [{"type": "mrkdwn", "text": text}]}


def _quote(reason: str) -> str:
    """Reason as a mrkdwn quote, cut to fit a section: escaping can grow it 5x.
    Only the display copy is cut -- the request keeps the full text."""
    quote = "\n".join(f">{escape_mrkdwn(line)}" for line in reason.splitlines() or [""])
    return quote if len(quote) <= _SECTION_TEXT_LIMIT else quote[: _SECTION_TEXT_LIMIT - 1] + "…"


def _buttons(request_json: str) -> dict:
    def button(action: entities.ApproverAction, style: str) -> dict:
        text = {"type": "plain_text", "text": action.name}
        return {"type": "button", "action_id": action.value, "text": text, "style": style, "value": request_json}

    return {
        "type": "actions",
        "block_id": "buttons",
        "elements": [button(entities.ApproverAction.Approve, "primary"), button(entities.ApproverAction.Discard, "danger")],
    }


class RequestCard(BaseModel):
    """What a request message shows besides its state."""

    requester_slack_id: str
    subject: str
    body: list[dict]  # reason quote, plus the secondary-domain warning when it applies
    source: dict
    duration: Optional[timedelta] = None  # pending only
    request_json: Optional[str] = None  # pending only: the button value

    @classmethod
    def for_request(cls, request: "RequestForAccess | RequestForGroupAccess", secondary_domain_used: bool = False) -> "RequestCard":
        body = [_section("reason", _quote(request.reason))]
        if secondary_domain_used:
            body.append(
                _section(
                    "warning",
                    ":warning: Matched to AWS SSO via the secondary domain fallback — "
                    f"verify this is really <@{request.requester_slack_id}> before approving.",
                )
            )
        if isinstance(request, RequestForAccess) and request.request_source == "cli":
            source = "Requested via CLI · :lock: identity verified via AWS SigV4"
        else:
            source = "Requested via Slack"
        return cls(
            requester_slack_id=request.requester_slack_id,
            subject=request_subject(request),
            body=body,
            source=_context("source", source),
            duration=request.permission_duration,
            request_json=request.model_dump_json(),
        )

    @classmethod
    def from_message(cls, message: dict, subject: str, requester_slack_id: str) -> "RequestCard":
        """For a request already posted: keeps its reason, warning and source blocks as they are."""
        blocks = message.get("blocks") or []
        return cls(
            requester_slack_id=requester_slack_id,
            subject=subject,
            body=[b for b in blocks if b.get("block_id") in ("reason", "warning")],
            source=next((b for b in blocks if b.get("block_id") == "source"), _context("source", "Requested via Slack")),
        )


class RequestState(BaseModel):
    icon: str
    word: str
    status: Optional[str] = None  # None only for Pending, which shows buttons instead

    @property
    def is_pending(self) -> bool:  # noqa: ANN101
        return self.status is None

    @property
    def is_failed(self) -> bool:  # noqa: ANN101
        return self.word == "Failed"

    @classmethod
    def pending(cls) -> "RequestState":
        return cls(icon=":closed_lock_with_key:", word="Pending")

    @classmethod
    def processing(cls, decided_by: str) -> "RequestState":
        return cls(icon=":hourglass_flowing_sand:", word="Processing", status=f"{decided_by} · granting…")

    @classmethod
    def approved(cls, decided_by: str, ends_at: datetime.datetime, auto: bool) -> "RequestState":
        word = "Auto-approved" if auto else "Approved"
        return cls(icon=":white_check_mark:", word=word, status=f"{decided_by} · access ends at {slack_time(ends_at)}")

    @classmethod
    def discarded(cls, approver_slack_id: str) -> "RequestState":
        return cls(icon=":wastebasket:", word="Discarded", status=f"Discarded by <@{approver_slack_id}>")

    @classmethod
    def expired(cls, after: timedelta) -> "RequestState":
        return cls(icon=":hourglass:", word="Expired", status=f"No decision within {format_duration(after)}")

    @classmethod
    def failed_with(cls, reason: str) -> "RequestState":
        return cls(icon=":x:", word="Failed", status=f"{reason} · details in thread")

    @classmethod
    def ended(cls, decided_by: str, ended_at: datetime.datetime) -> "RequestState":
        return cls(icon=":lock:", word="Ended", status=f"{decided_by} · access ended at {slack_time(ended_at)}")

    @classmethod
    def extended(cls, decided_by: str, newer_request: str) -> "RequestState":
        return cls(icon=":repeat:", word="Extended", status=f"{decided_by} · extended by {newer_request}")

    @classmethod
    def granted_not_scheduled(cls) -> "RequestState":
        status = "Access is live; the inconsistency check will remove it, or revoke manually · details in thread"
        return cls(icon=":warning:", word="Granted", status=status)


def build_request_message(card: RequestCard, state: RequestState) -> tuple[str, list[dict]]:
    """Every state of a request message comes from here. Returns the title (also
    the notification fallback text) and the blocks."""
    title = f"{state.icon} *{state.word} · {card.subject} for* <@{card.requester_slack_id}>"
    if state.status is None:
        if card.duration is None or card.request_json is None:
            raise ValueError("A pending request message needs the request itself")
        title += f" *for {format_duration(card.duration)}*"
        decision = _buttons(card.request_json)
    else:
        decision = _context("status", state.status)
    return title, [_section("title", title), *card.body, decision, card.source]


def update_request_message(client: WebClient, channel_id: str, ts: str, card: RequestCard, state: RequestState) -> bool:
    """Best-effort: a failed update is logged, not raised."""
    text, blocks = build_request_message(card, state)
    try:
        client.chat_update(channel=channel_id, ts=ts, blocks=blocks, text=text)
        return True
    except Exception as e:
        logger.exception(f"Failed to update request message {ts} to {state.word}: {e}")
        return False


def post_thread_reply(client: WebClient, channel_id: str, ts: str, text: str) -> bool:
    """Best-effort: a failed reply is logged, not raised."""
    try:
        client.chat_postMessage(channel=channel_id, thread_ts=ts, text=text)
        return True
    except Exception as e:
        logger.exception(f"Failed to reply in the thread of request message {ts}: {e}")
        return False


def should_dm(client: WebClient, requester_slack_id: str) -> bool:
    """Whether the requester needs DMs, being outside the channel. A failed
    membership check counts as outside: an extra DM beats a missing one."""
    if not cfg.send_dm_if_user_not_in_channel:
        return False
    try:
        return not check_if_user_is_in_channel(client, cfg.slack_channel_id, requester_slack_id)
    except Exception as e:
        logger.exception(f"Failed to check channel membership; assuming not in channel so the DM still goes out: {e}")
        return True


def send_dm(client: WebClient, user_slack_id: str, text: str) -> None:
    """Best-effort: a failed DM is logged, not raised."""
    try:
        client.chat_postMessage(
            channel=user_slack_id,
            text=f"{text}\nYou are receiving this in a DM because you are not a member of <#{cfg.slack_channel_id}>.",
        )
    except Exception as e:
        logger.exception(f"Failed to DM {user_slack_id}: {e}")


class IntakeOutcome(BaseModel):
    state: RequestState
    thread_reply: Optional[str] = None
    dm: Optional[str] = None


def intake_outcome(client: WebClient, decision: access_control.AccessRequestDecision, requester_slack_id: str) -> IntakeOutcome:
    """The state a new request is posted in, plus what to say in its thread and to the requester."""
    reasons = access_control.DecisionReason
    match decision.reason:
        case reasons.SelfApproval | reasons.ApprovalNotRequired:
            return IntakeOutcome(state=RequestState.processing(AUTO_APPROVAL_LABELS[decision.reason]))
        case reasons.RequiresApproval:
            approvers, approver_emails_not_found = find_approvers_in_slack(client, decision.approvers)  # type: ignore # noqa: PGH003
            if not approvers:
                text = "None of the approvers from configuration could be found in Slack. Check the module configuration."
                return IntakeOutcome(state=RequestState.failed_with("No approvers found in Slack"), thread_reply=text, dm=text)
            mentions = " ".join(f"<@{approver.id}>" for approver in approvers)
            text = f"{mentions}: waiting for your approval"
            if approver_emails_not_found:
                text += (
                    f"\nNote: some approvers ({', '.join(approver_emails_not_found)}) could not be found in Slack. "
                    "Check the module configuration."
                )
            return IntakeOutcome(
                state=RequestState.pending(), thread_reply=text, dm=f"Your request is waiting for approval from {mentions}."
            )
        case reasons.NoApprovers:
            text = "Nobody can approve this request."
            return IntakeOutcome(state=RequestState.failed_with("Nobody can approve this request"), thread_reply=text, dm=text)
        case reasons.NoStatements:
            text = "No statement in the configuration covers this request."
            return IntakeOutcome(state=RequestState.failed_with("No statement covers this request"), thread_reply=text, dm=text)
        case reasons.RequesterNotAllowed:
            return IntakeOutcome(
                state=RequestState.failed_with("Requester is not allowed to request this"),
                thread_reply=f"<@{requester_slack_id}> is not allowed to request this access.",
                dm="You are not allowed to request this access.",
            )
    raise ValueError(f"Unhandled decision reason: {decision.reason}")


def report_grant_outcome(  # noqa: PLR0913
    client: WebClient,
    channel_id: str,
    ts: str,
    card: RequestCard,
    decided_by: str,
    auto: bool,
    duration: timedelta,
    replaced: list,
    error: Exception | None,
    dm_requester: bool,
) -> None:
    """Shows how a grant ended on its request message, in the thread and by DM.
    Raises ShownOnRequest for a failed grant once the request shows it."""
    requester = card.requester_slack_id
    if error is None:
        ends_at = datetime.datetime.now(timezone.utc) + duration
        update_request_message(client, channel_id, ts, card, RequestState.approved(decided_by, ends_at, auto))
        # Sent even when the update failed, so a stale card is never the only signal.
        post_thread_reply(client, channel_id, ts, f"<@{requester}> access granted, ends at {slack_time(ends_at)}")
        if dm_requester:
            send_dm(client, requester, f"Access granted: {card.subject}, ends at {slack_time(ends_at)}.")
        mark_requests_extended(client, replaced, card.subject, channel_id, ts)
        return
    if isinstance(error, PostGrantError):
        text = (
            f"<@{requester}> access granted, but its automatic revocation could not be scheduled: {error}. "
            "The inconsistency check will remove it, or revoke it manually."
        )
        update_request_message(client, channel_id, ts, card, RequestState.granted_not_scheduled())
        post_thread_reply(client, channel_id, ts, text)
        if dm_requester:
            send_dm(client, requester, text)
        # The older requests' schedules may be gone already, so they no longer end when they say.
        mark_requests_extended(client, error.replaced, card.subject, channel_id, ts)
        return
    text = f"Granting access failed: {error}"
    updated = update_request_message(client, channel_id, ts, card, RequestState.failed_with("Granting access failed"))
    replied = post_thread_reply(client, channel_id, ts, text)
    if dm_requester:
        send_dm(client, requester, text)
    if updated or replied:
        raise ShownOnRequest(text) from error
    raise error


def grant_conflict_reply(approver_slack_id: str) -> str:
    return (
        f"<@{approver_slack_id}> another grant for this request is already running, please wait for its result. "
        "If no 'access granted' follows, ask the requester to submit again."
    )


def discard_request(  # noqa: PLR0913
    client: WebClient, channel_id: str, ts: str, card: RequestCard, approver_slack_id: str, requester_slack_id: str, dm_requester: bool
) -> bool:
    """Tells the requester only once the request shows Discarded; else its buttons are still live.
    Returns whether the request now shows Discarded."""
    if not update_request_message(client, channel_id, ts, card, RequestState.discarded(approver_slack_id)):
        post_thread_reply(client, channel_id, ts, f"<@{approver_slack_id}> the discard did not go through, please try again.")
        return False
    if dm_requester:
        send_dm(client, requester_slack_id, f"Your request was discarded by <@{approver_slack_id}>.")
    return True


def mark_requests_extended(client: WebClient, replaced: list, subject: str, channel_id: str, newer_ts: str) -> None:
    """A new grant replaced these revoke events' schedules: point each one's request at the newer request."""
    for event in replaced:
        if not (event.channel_id and event.message_ts):
            continue
        try:
            permalink = client.chat_getPermalink(channel=channel_id, message_ts=newer_ts)["permalink"]
            message = get_message_from_timestamp(event.channel_id, event.message_ts, client)
            if message is None:
                logger.warning("Extended request message not found", extra={"message_ts": event.message_ts})
                continue
            card = RequestCard.from_message(message, subject, event.requester.id)
            state = RequestState.extended(approved_by(event.approver.id), f"<{permalink}|newer request>")
            update_request_message(client, event.channel_id, event.message_ts, card, state)
        except Exception as e:
            logger.exception(f"Failed to mark request {event.message_ts} as extended: {e}")


def end_request_message(  # noqa: PLR0913
    client: WebClient, channel_id: str, message_ts: str, subject: str, requester_slack_id: str, approver_slack_id: str, reply: str
) -> bool:
    """Revocation: flip the request to Ended and say so in its thread. False when the message is gone."""
    message = get_message_from_timestamp(channel_id, message_ts, client)
    if message is None:
        return False
    card = RequestCard.from_message(message, subject, requester_slack_id)
    state = RequestState.ended(approved_by(approver_slack_id), datetime.datetime.now(timezone.utc))
    text, blocks = build_request_message(card, state)
    client.chat_update(channel=channel_id, ts=message_ts, blocks=blocks, text=text)
    post_thread_reply(client, channel_id, message_ts, reply)
    return True


def pending_request(message: dict) -> "RequestForAccess | RequestForGroupAccess | None":
    """The request a pending message's buttons carry; None for a message posted before they carried one."""
    buttons = next((b for b in message.get("blocks") or [] if b.get("block_id") == "buttons"), None)
    try:
        return request_adapter.validate_json(buttons["elements"][0]["value"])  # type: ignore # noqa: PGH003
    except ValidationError, KeyError, IndexError, TypeError:
        return None


OLD_REQUEST_REPLY = "This request was made before an Elevator upgrade — please request again"


def strip_buttons(client: WebClient, channel_id: str, message: dict) -> None:
    """Retire a pre-upgrade pending message, whose buttons can no longer be acted on."""
    blocks = remove_blocks(message.get("blocks") or [], block_ids=["buttons"])
    client.chat_update(channel=channel_id, ts=message["ts"], blocks=blocks, text=OLD_REQUEST_REPLY)


def check_if_user_is_in_channel(client: WebClient, channel_id: str, user_id: str) -> bool:
    logger.info(f"Checking if user {user_id} is in channel {channel_id}")

    response = client.conversations_members(channel=channel_id)

    members = jp.search("members", response.data)
    logger.debug(f"Members in channel {channel_id}: {members}")
    return user_id in members


def parse_user(user: dict) -> entities.slack.User:
    return entities.slack.User.model_validate(
        {"id": jp.search("user.id", user), "email": jp.search("user.profile.email", user), "real_name": jp.search("user.real_name", user)}
    )


def get_user(client: WebClient, id: str) -> entities.slack.User:
    response = client.users_info(user=id)
    return parse_user(response.data)  # type: ignore


def get_user_by_email(client: WebClient, email: str) -> entities.slack.User:
    logger.info(f"Getting slack user by email: {email}")
    # start is set once, before the loop, so rate-limit retries stop after
    # timeout_seconds in total and raise, instead of retrying until the Lambda times out.
    start = datetime.datetime.now(timezone.utc)
    timeout_seconds = 30
    while True:
        try:
            r = client.users_lookupByEmail(email=email)
            logger.info(f"Slack user found: {r}")
            return parse_user(r.data)  # type: ignore
        except slack_sdk.errors.SlackApiError as e:
            if e.response["error"] != "ratelimited":
                logger.exception(f"Error when getting slack user by email: {e}")
                raise
            if datetime.datetime.now(timezone.utc) - start >= datetime.timedelta(seconds=timeout_seconds):
                raise
            logger.info(f"Rate limited when getting slack user by email. Sleeping for 3 seconds. {e}")
            time.sleep(3)


def create_slack_mention_by_principal_id(
    sso_user_id: str,
    sso_client: SSOAdminClient,
    cfg: config.Config,
    identitystore_client: IdentityStoreClient,
    slack_client: WebClient,
) -> str:
    sso_instance = sso.describe_sso_instance(sso_client, cfg.sso_instance_arn)
    aws_user_emails = sso.get_user_emails(
        identitystore_client,
        sso_instance.identity_store_id,
        sso_user_id,
    )
    for email in aws_user_emails:
        try:
            return f"<@{get_user_by_email(slack_client, email).id}>"
        except Exception as e:  # noqa: BLE001
            logger.info(f"Failed to get slack user by email {email}. {e}")
    return aws_user_emails[0]


def get_message_from_timestamp(channel_id: str, message_ts: str, slack_client: slack_sdk.WebClient) -> dict | None:
    # latest + inclusive + limit=1 fetches exactly this message, however old it is.
    response = slack_client.conversations_history(channel=channel_id, latest=message_ts, inclusive=True, limit=1)
    messages = response.get("messages") or []
    return messages[0] if messages and messages[0].get("ts") == message_ts else None


# Plain text object supports only 99 options
# https://github.com/fivexl/terraform-aws-sso-elevator/issues/110
def get_max_duration_block(cfg: config.Config) -> list[Option]:
    if cfg.permission_duration_list_override:
        elements = cfg.permission_duration_list_override
        # Slack's StaticSelectElement caps at 99 options: keep the first 98
        # plus the last (typically longest) entry.
        if len(elements) > 99:  # noqa: PLR2004
            elements = elements[:98] + elements[-1:]
        return [Option(text=PlainTextObject(text=s), value=s) for s in elements]
    else:
        max_increments = min(cfg.max_permissions_duration_time * 2, 99)
        return [
            Option(text=PlainTextObject(text=f"{i // 2:02d}:{(i % 2) * 30:02d}"), value=f"{i // 2:02d}:{(i % 2) * 30:02d}")
            for i in range(1, max_increments + 1)
        ]


def find_approvers_in_slack(client: WebClient, approver_emails: list[str]) -> tuple[list[entities.slack.User], list[str]]:
    approvers = []
    approver_emails_not_found = []

    for email in approver_emails:
        try:
            approver = get_user_by_email(client, email)
            approvers.append(approver)
        except slack_sdk.errors.SlackApiError as e:
            # Only "users_not_found" means the approver is missing from Slack; any
            # other error (invalid_auth, missing_scope, an outage) is the integration
            # failing and must raise, not read as "no approvers found".
            if e.response.get("error") != "users_not_found":
                logger.exception(f"Unexpected Slack error while looking up approver {email}: {e}")
                raise
            logger.warning(f"Approver with email {email} not found in Slack")
            approver_emails_not_found.append(email)

    return approvers, approver_emails_not_found


class RequestForGroupAccess(entities.BaseModel):
    kind: Literal["group"] = "group"
    group_id: str
    # Display-only, set at intake; see RequestForAccess.account_name.
    group_name: str = ""
    reason: str
    requester_slack_id: str
    permission_duration: timedelta


# What a request button's value holds; "kind" routes the click.
Request = Annotated[Union[RequestForAccess, RequestForGroupAccess], Field(discriminator="kind")]
request_adapter: TypeAdapter[Request] = TypeAdapter(Request)


class RequestForGroupAccessView:
    __name__ = "RequestForGroupAccessView"
    CALLBACK_ID = "request_for_group_access_submitted"

    REASON_BLOCK_ID = "provide_reason"
    REASON_ACTION_ID = "provided_reason"

    GROUP_BLOCK_ID = "select_group"
    GROUP_ACTION_ID = "selected_group"

    DURATION_BLOCK_ID = "duration_picker"
    DURATION_ACTION_ID = "duration_picker_action"

    LOADING_BLOCK_ID = "loading"

    @classmethod
    def build(cls) -> View:  # noqa: ANN102
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            submit=PlainTextObject(text="Request"),
            close=PlainTextObject(text="Cancel"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[
                SectionBlock(text=MarkdownTextObject(text=":wave: Hey! Please fill form below to request access to AWS SSO group.")),
                DividerBlock(),
                SectionBlock(
                    block_id=cls.DURATION_BLOCK_ID,
                    text=MarkdownTextObject(text="Select the duration for which the access will be provided"),
                    accessory=StaticSelectElement(
                        action_id=cls.DURATION_ACTION_ID,
                        initial_option=get_max_duration_block(cfg)[0],
                        options=get_max_duration_block(cfg),
                        placeholder=PlainTextObject(text="Select duration"),
                    ),
                ),
                InputBlock(
                    block_id=cls.REASON_BLOCK_ID,
                    label=PlainTextObject(text="Why do you need access?"),
                    element=PlainTextInputElement(
                        action_id=cls.REASON_ACTION_ID,
                        placeholder=PlainTextObject(text="Reason will be saved in audit logs. Please be specific."),
                        multiline=True,
                        max_length=REASON_MAX_LENGTH,
                    ),
                ),
                DividerBlock(),
                SectionBlock(
                    text=MarkdownTextObject(
                        text="Remember to use access responsibly. All actions (AWS API calls) are being recorded.",
                    ),
                ),
                SectionBlock(
                    block_id=cls.LOADING_BLOCK_ID,
                    text=MarkdownTextObject(
                        text=":hourglass: Loading available accounts and permission sets...",
                    ),
                ),
            ],
        )

    @classmethod
    def build_no_available_options_view(cls, message: str) -> View:  # noqa: ANN102
        # No submit button: there is nothing the user could request.
        return View(
            type="modal",
            callback_id=cls.CALLBACK_ID,
            close=PlainTextObject(text="Close"),
            title=PlainTextObject(text="Get AWS access"),
            blocks=[SectionBlock(text=MarkdownTextObject(text=message))],
        )

    @classmethod
    def update_with_groups(cls, groups: list[entities.aws.SSOGroup]) -> View:  # noqa: ANN102
        view = cls.build()
        view.blocks = remove_blocks(view.blocks, block_ids=[cls.LOADING_BLOCK_ID])
        view.blocks = insert_blocks(
            blocks=view.blocks,
            blocks_to_insert=[
                cls.build_select_group_input_block(groups),
            ],
            after_block_id=cls.REASON_BLOCK_ID,
        )
        return view

    @classmethod
    def build_select_group_input_block(cls, groups: list[entities.aws.SSOGroup]) -> InputBlock:  # noqa: ANN102
        # TODO: handle case when there are more than 100 groups
        # 99 is the limit for StaticSelectElement
        # https://slack.dev/python-slack-sdk/api-docs/slack_sdk/models/blocks/block_elements.html#:~:text=StaticSelectElement(InputInteractiveElement)%3A%0A%20%20%20%20type%20%3D%20%22static_select%22-,options_max_length%20%3D%20100,-option_groups_max_length%20%3D%20100%0A%0A%20%20%20%20%40property%0A%20%20%20%20def%20attributes(
        if len(groups) > 99:  # noqa: PLR2004
            groups = groups[:99]
        sorted_groups = sorted(groups, key=lambda groups: groups.name)
        return InputBlock(
            block_id=cls.GROUP_BLOCK_ID,
            label=PlainTextObject(text="Select group"),
            element=StaticSelectElement(
                action_id=cls.GROUP_ACTION_ID,
                placeholder=PlainTextObject(text="Select group"),
                options=[Option(text=PlainTextObject(text=f"{group.name}"), value=group.id) for group in sorted_groups],
            ),
        )

    @classmethod
    def parse(cls, obj: dict) -> RequestForGroupAccess:  # noqa: ANN102
        values = jp.search("view.state.values", obj)
        hhmm = jp.search(f"{cls.DURATION_BLOCK_ID}.{cls.DURATION_ACTION_ID}.selected_option.value", values)
        hours, minutes = map(int, hhmm.split(":"))
        duration = timedelta(hours=hours, minutes=minutes)
        return RequestForGroupAccess.model_validate(
            {
                "permission_duration": duration,
                "group_id": jp.search(f"{cls.GROUP_BLOCK_ID}.{cls.GROUP_ACTION_ID}.selected_option.value", values),
                "reason": jp.search(f"{cls.REASON_BLOCK_ID}.{cls.REASON_ACTION_ID}.value", values),
                "requester_slack_id": jp.search("user.id", obj),
            }
        )


class ButtonClickedPayload(BaseModel):
    action: entities.ApproverAction
    approver_slack_id: str
    thread_ts: str
    channel_id: str
    message: dict
    request: Request

    @model_validator(mode="before")
    @classmethod
    def validate_payload(cls, values: dict) -> dict:  # noqa: ANN101
        # action_id says Approve or Discard; the value carries the request. A
        # pre-upgrade message's value is a bare word, which fails validation here.
        return {
            "action": jp.search("actions[0].action_id", values),
            "approver_slack_id": jp.search("user.id", values),
            "thread_ts": jp.search("message.ts", values),
            "channel_id": jp.search("channel.id", values),
            "message": values["message"],
            "request": json.loads(jp.search("actions[0].value", values)),
        }
