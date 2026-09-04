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
    assert sh.ButtonClickedPayload.find_in_fields_optional(fields, "Source") is None


def test_find_in_fields_optional_returns_value_when_present(slack_helpers_module):
    sh = slack_helpers_module
    fields = _content_fields(["Source: CLI"])
    assert sh.ButtonClickedPayload.find_in_fields_optional(fields, "Source") == "CLI"


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
    assert sh.ButtonClickedPayload.find_in_fields(fields, "Reason") == "debugging: INC-42"


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
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    texts = [f.text for f in content_block.fields]
    assert "Source: CLI" in texts
    assert f"Verified ARN: {arn}" in texts
    assert f"Verified UserId: {user_id}" in texts
