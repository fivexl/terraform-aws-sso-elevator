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
    """Regression test: an approval message posted before request_source/verified_arn
    existed has no "Source" field at all -- validate_payload must not raise, and must
    fall back to the pre-CLI defaults rather than losing the click entirely."""
    sh = slack_helpers_module
    values = _button_click_values(_content_fields())
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "slack"
    assert payload.request.verified_arn == "NA"


def test_button_clicked_payload_recovers_cli_provenance(slack_helpers_module):
    sh = slack_helpers_module
    arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com"
    values = _button_click_values(_content_fields(["Source: CLI", f"Verified ARN: {arn}"]))
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "cli"
    assert payload.request.verified_arn == arn


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


@pytest.mark.parametrize("malicious_reason", ["Source: CLI", "\nSource: CLI", "x\nSource: CLI"])
def test_reason_cannot_forge_the_source_field(slack_helpers_module, malicious_reason):
    """The single property the reconstruct-from-message-text design in
    ButtonClickedPayload.validate_payload rests on: a user-controlled reason
    embedding something that looks like a "Source: CLI" line must not be
    recoverable as one. "Reason: {reason}" is appended as one single Slack
    field object, and find_in_fields_optional checks whether a field's whole
    text *starts with* "Source" -- an embedded newline inside one field's
    text doesn't split it into separate fields the way genuinely distinct
    fields.append() calls do, so this holds regardless of what a reason's
    text contains. Round-trips through the real
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
            # request looks like, and must stay recovered as "slack" even
            # though the reason field's text contains "Source: CLI".
            request_source="slack",
        )
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    fields = [{"text": f.text} for f in content_block.fields]

    values = _button_click_values(fields)
    payload = sh.ButtonClickedPayload.model_validate(values)
    assert payload.request.request_source == "slack"
    assert payload.request.verified_arn == "NA"


def test_build_approval_request_message_blocks_adds_cli_badge(slack_helpers_module):
    sh = slack_helpers_module
    arn = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com"
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
        )
    content_block = next(b for b in blocks if getattr(b, "block_id", None) == "content")
    texts = [f.text for f in content_block.fields]
    assert "Source: CLI" in texts
    assert f"Verified ARN: {arn}" in texts
