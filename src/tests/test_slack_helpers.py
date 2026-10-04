"""Tests for the request message: the one builder behind every state, the
request carried as JSON in the button value, and the click payload that reads
it back (rejecting messages posted before the redesign)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

CLI_ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin_0123456789abcdef/req@example.com"


@pytest.fixture
def slack_helpers_module():
    sys.modules.pop("slack_helpers", None)
    with (
        patch.dict("sys.modules", {}),
        patch("boto3._get_default_session") as mock_session,
        patch("sso.describe_sso_instance", return_value=MagicMock(identity_store_id="d-1234")),
    ):
        mock_session.return_value.client.return_value = MagicMock()
        import slack_helpers

        yield slack_helpers
    sys.modules.pop("slack_helpers", None)


def _account_request(sh, **overrides):  # noqa: ANN001, ANN202, ANN003
    fields = {
        "permission_set_name": "ReadOnly",
        "account_id": "222222222222",
        "account_name": "aft",
        "reason": "testing revoker",
        "requester_slack_id": "U_REQ",
        "permission_duration": timedelta(minutes=30),
    }
    return sh.RequestForAccess(**(fields | overrides))


def _group_request(sh, **overrides):  # noqa: ANN001, ANN202, ANN003
    fields = {
        "group_id": "g-1234",
        "group_name": "platform-admins",
        "reason": "debugging",
        "requester_slack_id": "U_REQ",
        "permission_duration": timedelta(hours=1),
    }
    return sh.RequestForGroupAccess(**(fields | overrides))


def _block(blocks: list[dict], block_id: str) -> dict:
    return next(b for b in blocks if b.get("block_id") == block_id)


def _click(blocks: list[dict], action_id: str = "approve") -> dict:
    buttons = _block(blocks, "buttons")
    value = next(e["value"] for e in buttons["elements"] if e["action_id"] == action_id)
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "user": {"id": "U_APPROVER"},
        "message": {"ts": "12345.6789", "blocks": blocks},
        "channel": {"id": "C123"},
    }


ENDS = datetime(2026, 10, 4, 16, 50, tzinfo=timezone.utc)


def _states(sh):  # noqa: ANN001, ANN202
    state = sh.RequestState
    return {
        "pending": state.pending(),
        "processing": state.processing(sh.approved_by("U_APPROVER")),
        "approved": state.approved(sh.approved_by("U_APPROVER"), ENDS, auto=False),
        "auto_approved": state.approved("Self-approval allowed", ENDS, auto=True),
        "discarded": state.discarded("U_APPROVER"),
        "expired": state.expired(timedelta(hours=8)),
        "failed": state.failed_with("Nobody can approve this request"),
        "ended": state.ended(sh.approved_by("U_APPROVER"), ENDS),
        "extended": state.extended(sh.approved_by("U_APPROVER"), "<https://x.slack.com/archives/C/p1|newer request>"),
        "granted_not_scheduled": state.granted_not_scheduled(),
    }


EXPECTED_TITLES = {
    "pending": ":closed_lock_with_key: *Pending · ReadOnly → aft #222222222222 for* <@U_REQ> *for 30 min*",
    "processing": ":hourglass_flowing_sand: *Processing · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "approved": ":white_check_mark: *Approved · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "auto_approved": ":white_check_mark: *Auto-approved · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "discarded": ":wastebasket: *Discarded · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "expired": ":hourglass: *Expired · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "failed": ":x: *Failed · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "ended": ":lock: *Ended · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "extended": ":repeat: *Extended · ReadOnly → aft #222222222222 for* <@U_REQ>",
    "granted_not_scheduled": ":warning: *Granted · ReadOnly → aft #222222222222 for* <@U_REQ>",
}

EXPECTED_STATUS = {
    "processing": "Approved by <@U_APPROVER> · granting…",
    "approved": "Approved by <@U_APPROVER> · access ends at <!date^1791132600^{time}|16:50 UTC>",
    "auto_approved": "Self-approval allowed · access ends at <!date^1791132600^{time}|16:50 UTC>",
    "discarded": "Discarded by <@U_APPROVER>",
    "expired": "No decision within 8 hours",
    "failed": "Nobody can approve this request · details in thread",
    "ended": "Approved by <@U_APPROVER> · access ended at <!date^1791132600^{time}|16:50 UTC>",
    "extended": "Approved by <@U_APPROVER> · extended by <https://x.slack.com/archives/C/p1|newer request>",
    "granted_not_scheduled": "Access is live; the inconsistency check will remove it, or revoke manually · details in thread",
}


@pytest.mark.parametrize("state_name", list(EXPECTED_TITLES))
def test_builder_renders_every_state(slack_helpers_module, state_name):
    sh = slack_helpers_module
    card = sh.RequestCard.for_request(_account_request(sh))
    title, blocks = sh.build_request_message(card, _states(sh)[state_name])

    assert title == EXPECTED_TITLES[state_name]
    assert [b["block_id"] for b in blocks] == ["title", "reason", "buttons" if state_name == "pending" else "status", "source"]
    assert _block(blocks, "title")["text"]["text"] == title
    assert _block(blocks, "reason")["text"]["text"] == ">testing revoker"
    assert _block(blocks, "source")["elements"][0]["text"] == "Requested via Slack"
    assert not any(b["type"] in ("divider", "header") for b in blocks)
    if state_name != "pending":
        assert _block(blocks, "status")["elements"][0]["text"] == EXPECTED_STATUS[state_name]


def test_group_title_names_the_group_without_its_id(slack_helpers_module):
    sh = slack_helpers_module
    title, _ = sh.build_request_message(sh.RequestCard.for_request(_group_request(sh)), sh.RequestState.pending())
    assert title == ":closed_lock_with_key: *Pending · group platform-admins for* <@U_REQ> *for 1 hour*"
    assert "g-1234" not in title


def test_cli_request_shows_only_its_verified_source_line(slack_helpers_module):
    sh = slack_helpers_module
    request = _account_request(
        sh, request_source="cli", verified_arn=CLI_ARN, verified_user_id="u-verified", verified_email="req@example.com"
    )
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    assert _block(blocks, "source")["elements"][0]["text"] == "Requested via CLI · :lock: identity verified via AWS SigV4"
    visible = json.dumps([b for b in blocks if b["block_id"] != "buttons"])
    for secret in (CLI_ARN, "u-verified", "req@example.com"):
        assert secret not in visible


def test_secondary_domain_warning_sits_between_reason_and_buttons(slack_helpers_module):
    sh = slack_helpers_module
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(_account_request(sh), True), sh.RequestState.pending())
    assert [b["block_id"] for b in blocks] == ["title", "reason", "warning", "buttons", "source"]
    assert "verify this is really <@U_REQ>" in _block(blocks, "warning")["text"]["text"]


def test_reason_is_escaped_quoted_and_cut_to_fit_a_section(slack_helpers_module):
    sh = slack_helpers_module
    card = sh.RequestCard.for_request(_account_request(sh, reason="<!channel> AT&T\nsecond line"))
    assert _block(card.body, "reason")["text"]["text"] == ">&lt;!channel&gt; AT&amp;T\n>second line"
    long_card = sh.RequestCard.for_request(_account_request(sh, reason="&" * 1000))
    assert len(_block(long_card.body, "reason")["text"]["text"]) <= 3000  # noqa: PLR2004


@pytest.mark.parametrize("make_request", [_account_request, _group_request])
def test_button_value_round_trips_the_request(slack_helpers_module, make_request):
    sh = slack_helpers_module
    request = make_request(sh, reason='quotes " and <!here> & newlines\nkept')
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())

    for action_id in ("approve", "discard"):
        payload = sh.ButtonClickedPayload.model_validate(_click(blocks, action_id))
        assert payload.request == request
        assert payload.action.value == action_id
    assert sh.pending_request({"blocks": blocks}) == request


def test_cli_identity_round_trips_through_the_button_value(slack_helpers_module):
    sh = slack_helpers_module
    request = _account_request(
        sh, request_source="cli", verified_arn=CLI_ARN, verified_user_id="u-verified", verified_email="req@example.com"
    )
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    payload = sh.ButtonClickedPayload.model_validate(_click(blocks))
    assert payload.request.request_source == "cli"
    assert payload.request.verified_user_id == "u-verified"
    assert payload.request.verified_email == "req@example.com"


CLI_FIELDS = {
    "request_source": "cli",
    "verified_arn": CLI_ARN,
    "verified_user_id": "11111111-2222-3333-4444-555555555555",
    "verified_email": "req@example.com",
}


def _button_value(sh, request) -> str:  # noqa: ANN001
    _, blocks = sh.build_request_message(sh.RequestCard.for_request(request), sh.RequestState.pending())
    return _block(blocks, "buttons")["elements"][0]["value"]


@pytest.mark.parametrize(
    ("make_request", "overrides"),
    [
        (_account_request, {"reason": "x" * 1000} | CLI_FIELDS),
        (_account_request, {"reason": "é" * 1000}),
        (_account_request, {"reason": "\n" * 800}),
        (_group_request, {"reason": "x" * 1000}),
    ],
)
def test_request_within_limits_is_accepted_and_fits_the_button(slack_helpers_module, make_request, overrides):
    sh = slack_helpers_module
    request = make_request(sh, **overrides)
    assert sh.request_rejection(request) is None
    assert len(_button_value(sh, request)) <= 2000  # noqa: PLR2004


def test_reason_over_the_cap_is_rejected(slack_helpers_module):
    sh = slack_helpers_module
    assert sh.request_rejection(_account_request(sh, reason="x" * 1001)) == "Reason must be 1000 characters or fewer"


@pytest.mark.parametrize(
    ("make_request", "overrides"),
    [
        # Within the reason cap, but JSON escaping doubles the quotes; long names and a verified identity add the rest.
        (
            _account_request,
            {
                "reason": '"' * 498 + "x" * 502,
                "account_name": "a-rather-long-production-account-name",
                "permission_set_name": "AWSAdministratorAccessExtended",
            }
            | CLI_FIELDS
            | {"verified_arn": CLI_ARN + "x" * 60, "verified_email": "someone.with.a.long.name@subdomain.example.com"},
        ),
        (_account_request, {"reason": "x" * 1000, "account_name": "n" * 1000}),
        (_group_request, {"reason": "x" * 1000, "group_name": "n" * 1000}),
    ],
)
def test_request_too_large_for_the_button_is_rejected(slack_helpers_module, make_request, overrides):
    sh = slack_helpers_module
    request = make_request(sh, **overrides)
    assert len(_button_value(sh, request)) > 2000  # noqa: PLR2004
    assert sh.request_rejection(request) == "Request is too large for Slack; shorten the reason"


@pytest.mark.parametrize("old_value", ["approve", "discard"])
def test_pre_upgrade_button_value_is_rejected(slack_helpers_module, old_value):
    """A message posted before the redesign carries a bare word, not a request;
    there is no text-scraping fallback, the click is rejected."""
    sh = slack_helpers_module
    body = {
        "actions": [{"action_id": old_value, "value": old_value}],
        "user": {"id": "U_APPROVER"},
        "message": {"ts": "1.2", "blocks": [{"block_id": "content", "fields": [{"text": "Requester: <@U_REQ>"}]}]},
        "channel": {"id": "C123"},
    }
    with pytest.raises(sh.ValidationError):
        sh.ButtonClickedPayload.model_validate(body)
    assert sh.pending_request({"blocks": [{"block_id": "buttons", "elements": [{"value": old_value}]}]}) is None


def test_from_message_keeps_reason_warning_and_source_blocks(slack_helpers_module):
    sh = slack_helpers_module
    request = _account_request(sh, request_source="cli", verified_user_id="u", verified_email="e@x.com")
    _, posted = sh.build_request_message(sh.RequestCard.for_request(request, True), sh.RequestState.pending())
    card = sh.RequestCard.from_message({"blocks": posted}, "ReadOnly → aft #222222222222", "U_REQ")
    _, ended = sh.build_request_message(card, sh.RequestState.ended(sh.approved_by("U_APPROVER"), ENDS))
    assert [b["block_id"] for b in ended] == ["title", "reason", "warning", "status", "source"]
    assert _block(ended, "source") == _block(posted, "source")


def test_get_message_from_timestamp_fetches_exactly_that_message(slack_helpers_module):
    sh = slack_helpers_module
    client = MagicMock()
    client.conversations_history.return_value = {"messages": [{"ts": "1.5"}]}
    assert sh.get_message_from_timestamp("C1", "1.5", client) == {"ts": "1.5"}
    client.conversations_history.assert_called_once_with(channel="C1", latest="1.5", inclusive=True, limit=1)
    assert sh.get_message_from_timestamp("C1", "1.6", client) is None


def _approved_blocks(sh) -> list[dict]:  # noqa: ANN001
    card = sh.RequestCard.for_request(_account_request(sh))
    return sh.build_request_message(card, sh.RequestState.approved(sh.approved_by("U_APPROVER"), ENDS, auto=False))[1]


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ("pending", False),
        ("approved", True),
        ("missing", False),  # deleted or unreadable: the click goes ahead as before
    ],
)
def test_decided_elsewhere_reads_whether_the_request_still_shows_its_buttons(slack_helpers_module, history, expected):
    sh = slack_helpers_module
    blocks = {
        "pending": sh.build_request_message(sh.RequestCard.for_request(_account_request(sh)), sh.RequestState.pending())[1],
        "approved": _approved_blocks(sh),
    }
    client = MagicMock()
    messages = [{"ts": "1.5", "blocks": blocks[history]}] if history in blocks else []
    client.conversations_history.return_value = {"messages": messages}
    assert sh.decided_elsewhere(client, "C1", "1.5") is expected


def test_decided_elsewhere_treats_a_failed_read_as_still_pending(slack_helpers_module):
    client = MagicMock()
    client.conversations_history.side_effect = RuntimeError("ratelimited")
    assert slack_helpers_module.decided_elsewhere(client, "C1", "1.5") is False


def test_mark_requests_extended_skips_the_newer_requests_own_message(slack_helpers_module):
    """#212: a late second click on one request replaces that request's own schedule;
    only the genuinely older request becomes Extended."""
    sh = slack_helpers_module

    def _event(ts: str) -> MagicMock:
        return MagicMock(channel_id="C1", message_ts=ts, requester=MagicMock(id="U_REQ"), approver=MagicMock(id="U_APPROVER"))

    client = MagicMock()
    client.chat_getPermalink.return_value = {"permalink": "https://x.slack.com/archives/C1/p2002"}
    client.conversations_history.return_value = {"messages": [{"ts": "100.1", "blocks": _approved_blocks(sh)}]}

    sh.mark_requests_extended(client, [_event("200.2"), _event("100.1")], "ReadOnly → aft #222222222222", "C1", "200.2")

    assert [c.kwargs["ts"] for c in client.chat_update.call_args_list] == ["100.1"]
    assert client.chat_update.call_args.kwargs["text"].startswith(":repeat: *Extended")


def test_format_duration(slack_helpers_module):
    sh = slack_helpers_module
    assert sh.format_duration(timedelta(minutes=30)) == "30 min"
    assert sh.format_duration(timedelta(hours=1)) == "1 hour"
    assert sh.format_duration(timedelta(hours=25, minutes=15)) == "1 day 1 hour 15 min"


def test_find_approvers_in_slack_treats_users_not_found_as_the_expected_case(slack_helpers_module):
    sh = slack_helpers_module
    with patch.object(
        sh, "get_user_by_email", side_effect=sh.slack_sdk.errors.SlackApiError("users_not_found", {"ok": False, "error": "users_not_found"})
    ):
        approvers, not_found = sh.find_approvers_in_slack(MagicMock(), ["gone@example.com"])
    assert approvers == []
    assert not_found == ["gone@example.com"]


def test_find_approvers_in_slack_reraises_a_broken_slack_integration_instead_of_reporting_not_found(slack_helpers_module):
    """Regression test (found in a final pre-delivery review): a Slack
    error other than "users_not_found" -- invalid_auth from a rotated bot
    token, missing_scope from a dropped OAuth scope, an outage -- must not
    be silently folded into "this approver wasn't found in Slack". That
    used to be indistinguishable from a genuine config problem (a stale
    approver email), hiding a broken integration behind
    process_access_request's ordinary "none of the approvers... could be
    found" rejection message."""
    sh = slack_helpers_module
    with patch.object(
        sh, "get_user_by_email", side_effect=sh.slack_sdk.errors.SlackApiError("invalid_auth", {"ok": False, "error": "invalid_auth"})
    ):
        with pytest.raises(sh.slack_sdk.errors.SlackApiError):
            sh.find_approvers_in_slack(MagicMock(), ["approver@example.com"])


def test_get_user_by_email_bounds_ratelimited_retries_to_the_real_timeout(slack_helpers_module):
    """Regression test (found in a final pre-delivery review): this used to
    retry a "ratelimited" response by *recursing*, and each recursive call
    recomputed its own fresh `start` timestamp -- so `now - start` could
    never reach timeout_seconds no matter how long retries had actually
    been running, and a sustained ratelimited response looped effectively
    forever. `start` must be computed once, outside the retry loop, so the
    30s budget is measured against when the FIRST attempt began, not each
    individual retry."""
    sh = slack_helpers_module
    client = MagicMock()
    rate_limited = sh.slack_sdk.errors.SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"})
    client.users_lookupByEmail.side_effect = rate_limited

    base = sh.datetime.datetime(2024, 1, 1, tzinfo=sh.timezone.utc)
    # First call is `start`, computed once before the loop. Then each loop
    # iteration re-checks the elapsed time once. Simulate: still well under
    # the 30s budget on the first check, then past it on the second --
    # proving the elapsed time is measured against the *original* start,
    # not reset by a fresh "now" each retry (which the old recursive
    # version effectively did).
    now_values = [base, base + sh.timedelta(seconds=10), base + sh.timedelta(seconds=35)]

    with (
        patch.object(sh.datetime, "datetime", MagicMock(now=MagicMock(side_effect=now_values))),
        patch.object(sh.time, "sleep"),
    ):
        with pytest.raises(sh.slack_sdk.errors.SlackApiError):
            sh.get_user_by_email(client, "req@example.com")

    # Exactly two lookup attempts: the initial one, plus one retry after the
    # first (under-budget) elapsed-time check -- the second (over-budget)
    # check must raise before attempting a third lookup.
    assert client.users_lookupByEmail.call_count == 2  # noqa: PLR2004


def test_get_max_duration_block_caps_an_override_list_at_the_real_slack_limit(slack_helpers_module):
    """Regression test (found in a final pre-delivery review): Slack's
    StaticSelectElement hard-caps at 99 options. This used to truncate an
    oversized permission_duration_list_override to 99 elements *plus* the
    last one -- 100 total, one over the real limit -- which would get
    rejected by Slack with invalid_blocks the moment an operator configured
    101+ override entries."""
    sh = slack_helpers_module
    cfg = MagicMock()
    cfg.permission_duration_list_override = [f"{i:02d}:00" for i in range(150)]
    result = sh.get_max_duration_block(cfg)
    assert len(result) == 99
    # The last configured entry must still be present (preserving the
    # apparent intent of always keeping it visible), just not at the cost
    # of exceeding the real cap.
    assert result[-1].value == cfg.permission_duration_list_override[-1]
