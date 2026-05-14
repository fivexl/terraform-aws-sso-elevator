"""Tests for explicit per-account warning map (config + Slack modal block helpers)."""

import config
from requester.slack import slack_helpers


def test_parse_account_warning_messages_raw_strips_keys() -> None:
    raw = '{" 111111111111 ": "Mgmt", "222222222222": "Prod"}'
    got = config.parse_account_warning_messages_raw(raw)
    assert got == {"111111111111": "Mgmt", "222222222222": "Prod"}


def test_account_warning_message_lookup() -> None:
    m = {"111111111111": "Hello"}
    assert config.account_warning_message("111111111111", m) == "Hello"
    assert config.account_warning_message("  111111111111  ", m) == "Hello"
    assert config.account_warning_message("999999999999", m) is None


def test_apply_account_warning_to_view_blocks() -> None:
    msgs = {"111111111111": "WARN_TEXT"}
    blocks = [
        {"type": "input", "block_id": slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID, "element": {}},
    ]
    out = slack_helpers.apply_account_warning_to_view_blocks(
        list(blocks),
        selected_account_id="111111111111",
        messages=msgs,
    )
    assert out[0]["block_id"] == slack_helpers.RequestForAccessView.ACCOUNT_WARNING_BLOCK_ID
    assert out[0]["text"]["text"] == "WARN_TEXT"
    assert out[1]["block_id"] == slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID


def test_apply_account_warning_removes_when_unlisted() -> None:
    msgs = {"111111111111": "WARN_TEXT"}
    blocks = [
        {
            "type": "section",
            "block_id": slack_helpers.RequestForAccessView.ACCOUNT_WARNING_BLOCK_ID,
            "text": {"type": "mrkdwn", "text": "x"},
        },
        {"type": "input", "block_id": slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID, "element": {}},
    ]
    out = slack_helpers.apply_account_warning_to_view_blocks(
        list(blocks),
        selected_account_id="222222222222",
        messages=msgs,
    )
    assert len(out) == 1
    assert out[0]["block_id"] == slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID


def test_parse_account_warning_messages_raw_invalid_json() -> None:
    assert config.parse_account_warning_messages_raw("{bad json") == {}


def test_parse_account_warning_messages_raw_empty_inputs() -> None:
    assert config.parse_account_warning_messages_raw(None) == {}
    assert config.parse_account_warning_messages_raw("") == {}
    assert config.parse_account_warning_messages_raw("   ") == {}
    assert config.parse_account_warning_messages_raw({}) == {}


def test_parse_account_warning_messages_raw_non_dict_json() -> None:
    assert config.parse_account_warning_messages_raw("[1, 2, 3]") == {}


def test_parse_account_warning_messages_raw_unknown_type() -> None:
    assert config.parse_account_warning_messages_raw(42) == {}
    assert config.parse_account_warning_messages_raw(["a", "b"]) == {}


def test_apply_account_warning_no_selection() -> None:
    msgs = {"111111111111": "WARN"}
    blocks = [{"type": "input", "block_id": slack_helpers.RequestForAccessView.ACCOUNT_BLOCK_ID, "element": {}}]
    out = slack_helpers.apply_account_warning_to_view_blocks(blocks, selected_account_id=None, messages=msgs)
    assert out == blocks


def test_apply_account_warning_no_account_block() -> None:
    msgs = {"111111111111": "WARN"}
    blocks = [{"type": "section", "block_id": "some_other_block"}]
    out = slack_helpers.apply_account_warning_to_view_blocks(blocks, selected_account_id="111111111111", messages=msgs)
    assert out == blocks
