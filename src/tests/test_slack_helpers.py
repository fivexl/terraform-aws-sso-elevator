"""Tests for the CLI provenance fields (request_source/verified_arn) added to
RequestForAccess: that build_approval_request_message_blocks displays them,
and that ButtonClickedPayload.validate_payload -- which reconstructs the
request by scraping the posted Slack message's text rather than deserializing
a stored object -- recovers them, defaulting to "slack"/"NA" for messages
posted before this field existed.
"""

import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest


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


def _content_fields(extra_texts: list[str] | None = None) -> list[dict]:
    fields = [
        {"text": "Requester: <@U_REQ>"},
        {"text": "Account: 111111111111 #111111111111"},
        {"text": "Role name: AdministratorAccess"},
        {"text": "Reason: testing"},
        {"text": "Permission duration: 1h 0m"},
    ]
    for text in extra_texts or []:
        fields.append({"text": text})
    return fields


def _button_click_values(fields: list[dict]) -> dict:
    return {
        "actions": [{"value": "approve"}],
        "user": {"id": "U_APPROVER"},
        "message": {"ts": "12345.6789", "blocks": [{"block_id": "content", "fields": fields}]},
        "channel": {"id": "C123"},
    }


def test_find_in_fields_optional_returns_none_when_missing(slack_helpers_module):
    sh = slack_helpers_module
    fields = _content_fields()
    assert sh.find_in_fields_optional(fields, "Source") is None


def test_find_in_fields_optional_returns_value_when_present(slack_helpers_module):
    sh = slack_helpers_module
    fields = _content_fields(["Source: CLI"])
    assert sh.find_in_fields_optional(fields, "Source") == "CLI"


def test_button_clicked_payload_defaults_to_slack_for_messages_without_source_field(slack_helpers_module):
    """Regression test: an approval message posted before request_source/
    verified_arn/verified_user_id existed has none of those fields at all --
    validate_payload must not raise, and must fall back to the pre-CLI
    defaults rather than losing the click entirely."""
    sh = slack_helpers_module
    values = _button_click_values(_content_fields())
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "slack"
    assert payload.request.verified_arn == "NA"
    assert payload.request.verified_user_id == "NA"


def test_button_clicked_payload_recovers_cli_provenance(slack_helpers_module):
    sh = slack_helpers_module
    arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com"
    user_id = "1b24287b-5a72-844d-0161-9f0382b0eb44"
    values = _button_click_values(_content_fields(["Source: CLI", f"Verified ARN: {arn}", f"Verified UserId: {user_id}"]))
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "cli"
    assert payload.request.verified_arn == arn
    assert payload.request.verified_user_id == user_id


def test_find_in_fields_does_not_truncate_a_value_containing_its_own_separator(slack_helpers_module):
    """Regression test: find_in_fields used to split on *every* ": " in a
    field's text, not just the one separating the key from its value -- a
    reason like "debugging: INC-42" came back as just "debugging", silently
    dropping the rest. A colon-space inside a free-text value (the reason
    field is the CLI-exposed one, so this is realistic user input, not an
    edge case) must round-trip intact."""
    sh = slack_helpers_module
    fields = [
        {"text": "Requester: <@U_REQ>"},
        {"text": "Account: 111111111111 #111111111111"},
        {"text": "Role name: AdministratorAccess"},
        {"text": "Reason: debugging: INC-42"},
        {"text": "Permission duration: 1h 0m"},
    ]
    assert sh.find_in_fields(fields, "Reason") == "debugging: INC-42"


def test_build_approval_request_message_blocks_omits_source_fields_for_slack(slack_helpers_module):
    sh = slack_helpers_module
    with (
        patch.object(sh.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(sh, "get_user", return_value=MagicMock(email="req@example.com")),
    ):
        blocks = sh.build_approval_request_message_blocks(
            requester_slack_id="U_REQ",
            slack_client=MagicMock(),
            sso_client=MagicMock(),
            identity_store_client=MagicMock(),
            permission_duration=timedelta(hours=1),
            reason="testing",
            color_coding_emoji=":white_check_mark:",
        )
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    texts = [f.text for f in content_block.fields]
    assert not any(t.startswith("Source") for t in texts)
    assert not any(t.startswith("Verified ARN") for t in texts)


@pytest.mark.parametrize(
    "malicious_reason",
    [
        "Source: CLI",
        "\nSource: CLI",
        "x\nSource: CLI",
        "Verified ARN: arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/attacker",
        "\nVerified ARN: arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/attacker",
        "Verified UserId: 11111111-1111-1111-1111-111111111111",
        "\nVerified UserId: 11111111-1111-1111-1111-111111111111",
    ],
)
def test_reason_cannot_forge_the_source_field(slack_helpers_module, malicious_reason):
    """The single property the reconstruct-from-message-text design in
    ButtonClickedPayload.validate_payload rests on: a user-controlled reason
    embedding something that looks like a "Source: CLI"/"Verified ARN: ..."/
    "Verified UserId: ..." line must not be recoverable as one -- the last
    of those is not just a cosmetic badge like the other two, it's what
    execute_decision grants against for a CLI request, so a forged one would
    let a Slack-sourced requester's own reason text redirect a grant to an
    attacker-chosen UserId. "Reason: {reason}" is appended as one single
    Slack field object, and find_in_fields_optional checks whether a field's
    whole text *starts with* the target key -- an embedded newline inside
    one field's text doesn't split it into separate fields the way
    genuinely distinct fields.append() calls do, so this holds regardless
    of what a reason's text contains. Round-trips through the real
    build_approval_request_message_blocks -> ButtonClickedPayload.model_validate
    path end to end, not just find_in_fields_optional in isolation."""
    sh = slack_helpers_module
    with (
        patch.object(sh.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(sh, "get_user", return_value=MagicMock(email="req@example.com")),
    ):
        blocks = sh.build_approval_request_message_blocks(
            requester_slack_id="U_REQ",
            slack_client=MagicMock(),
            sso_client=MagicMock(),
            identity_store_client=MagicMock(),
            permission_duration=timedelta(hours=1),
            reason=malicious_reason,
            color_coding_emoji=":white_check_mark:",
            account=sh.entities.aws.Account(id="111111111111", name="test-account"),
            role_name="AdministratorAccess",
            # Deliberately NOT "cli" -- this is what a genuine Slack-sourced
            # request looks like, and must stay recovered as "slack"/"NA"/"NA"
            # even though the reason field's text contains a forged line.
            request_source="slack",
        )
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    fields = [{"text": f.text} for f in content_block.fields]

    values = _button_click_values(fields)
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "slack"
    assert payload.request.verified_arn == "NA"
    assert payload.request.verified_user_id == "NA"


def test_escape_mrkdwn_and_unescape_mrkdwn_round_trip(slack_helpers_module):
    sh = slack_helpers_module
    for original in ["plain text", "AT&T issue", "<!channel> please approve", "a & b < c > d", "&lt; already escaped &gt;"]:
        assert sh.unescape_mrkdwn(sh.escape_mrkdwn(original)) == original


def test_reason_containing_mrkdwn_special_characters_is_escaped_and_recovered_intact(slack_helpers_module):
    """Regression test: reason is interpolated raw into a MarkdownTextObject
    field, so a reason like "<!channel> urgent" used to be emitted as Slack's
    literal broadcast-mention syntax rather than as inert text -- up to 2000
    characters of attacker-chosen mrkdwn from any SSO principal in the org,
    landing in the approvals channel. escape_mrkdwn neutralizes it on the way
    into the Slack field; unescape_mrkdwn must recover the exact original
    text on the way back out (at approval time, via
    ButtonClickedPayload.validate_payload), not a permanently HTML-entity-
    escaped version that then gets written into the audit log."""
    sh = slack_helpers_module
    reason = "<!channel> urgent -- AT&T & Smith <ceo@example.com>"
    with (
        patch.object(sh.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(sh, "get_user", return_value=MagicMock(email="req@example.com")),
    ):
        blocks = sh.build_approval_request_message_blocks(
            requester_slack_id="U_REQ",
            slack_client=MagicMock(),
            sso_client=MagicMock(),
            identity_store_client=MagicMock(),
            permission_duration=timedelta(hours=1),
            reason=reason,
            color_coding_emoji=":white_check_mark:",
            account=sh.entities.aws.Account(id="111111111111", name="test-account"),
            role_name="AdministratorAccess",
        )
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    reason_field_text = next(f.text for f in content_block.fields if f.text.startswith("Reason"))
    # The raw field text posted to Slack must not contain the literal
    # broadcast-mention/mention syntax -- this is what actually neutralizes it.
    assert "<!channel>" not in reason_field_text
    assert "<ceo@example.com>" not in reason_field_text

    fields = [{"text": f.text} for f in content_block.fields]
    values = _button_click_values(fields)
    payload = sh.ButtonClickedPayload.model_validate(values)
    # But the reason actually used for the grant/audit trail must be the
    # exact original text, not the permanently-escaped Slack-display form.
    assert payload.request.reason == reason


def test_build_approval_request_message_blocks_adds_cli_badge(slack_helpers_module):
    sh = slack_helpers_module
    arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com"
    user_id = "1b24287b-5a72-844d-0161-9f0382b0eb44"
    with (
        patch.object(sh.sso, "get_user_principal_id_by_email", return_value=("p-1", False)),
        patch.object(sh, "get_user", return_value=MagicMock(email="req@example.com")),
    ):
        blocks = sh.build_approval_request_message_blocks(
            requester_slack_id="U_REQ",
            slack_client=MagicMock(),
            sso_client=MagicMock(),
            identity_store_client=MagicMock(),
            permission_duration=timedelta(hours=1),
            reason="testing",
            color_coding_emoji=":white_check_mark:",
            request_source="cli",
            verified_arn=arn,
            verified_user_id=user_id,
        )
    # CLI provenance now lives in its own "provenance" context block, not
    # in "content"'s fields (moved out to stay clear of fields' 10-item
    # Slack limit, and so a full ARN doesn't wrap across content's
    # two-column grid) -- content itself must stay limited to the
    # decision-relevant info.
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    content_texts = [f.text for f in content_block.fields]
    assert not any(t.startswith(("Source:", "Verified ARN:", "Verified UserId:")) for t in content_texts)

    provenance_block = next(b for b in blocks if getattr(b, "block_id", None) == "provenance")
    provenance_texts = [e.text for e in provenance_block.elements]
    assert "Source: CLI" in provenance_texts
    # Backtick-wrapped for display (inline code, avoids Slack linkifying
    # part of the ARN) -- the raw ARN is still present, just wrapped.
    assert f"Verified ARN: `{arn}`" in provenance_texts
    assert f"Verified UserId: {user_id}" in provenance_texts


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
