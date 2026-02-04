"""Tests for slack_helpers module."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from slack_helpers import ButtonClickedPayload, ButtonGroupClickedPayload, get_max_duration_block


def _make_config(max_hours: int, override: list[str] | None = None) -> MagicMock:
    """Create a mock config with specified max_permissions_duration_time."""
    cfg = MagicMock()
    cfg.max_permissions_duration_time = max_hours
    cfg.permission_duration_list_override = override
    return cfg


class TestGetMaxDurationBlock:
    def test_default_durations_with_24h_max(self):
        """All 8 base durations returned when max is 24h."""
        cfg = _make_config(max_hours=24)
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert values == ["00:15", "00:30", "01:00", "02:00", "04:00", "08:00", "12:00", "24:00"]

    def test_filters_durations_exceeding_max(self):
        """Durations > max_permissions_duration_time are excluded."""
        cfg = _make_config(max_hours=4)
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert values == ["00:15", "00:30", "01:00", "02:00", "04:00"]
        assert "08:00" not in values
        assert "12:00" not in values
        assert "24:00" not in values

    def test_includes_max_when_not_in_base_set(self):
        """If max is 6h, includes 6h even though not in base set."""
        cfg = _make_config(max_hours=6)
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert "06:00" in values
        # Should be sorted correctly
        assert values == ["00:15", "00:30", "01:00", "02:00", "04:00", "06:00"]

    def test_max_already_in_base_set_not_duplicated(self):
        """If max is 8h (in base set), no duplicate."""
        cfg = _make_config(max_hours=8)
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert values.count("08:00") == 1
        assert values == ["00:15", "00:30", "01:00", "02:00", "04:00", "08:00"]

    def test_display_text_is_human_readable(self):
        """Display shows '15 min', '1 hour', '2 hours' etc."""
        cfg = _make_config(max_hours=24)
        options = get_max_duration_block(cfg)

        # Option.text can be a PlainTextObject or string depending on slack-sdk version
        texts = [opt.text if isinstance(opt.text, str) else opt.text.text for opt in options]
        assert texts == ["15 min", "30 min", "1 hour", "2 hours", "4 hours", "8 hours", "12 hours", "24 hours"]

    def test_value_is_hhmm_format(self):
        """Value is HH:MM format for backend parsing."""
        cfg = _make_config(max_hours=24)
        options = get_max_duration_block(cfg)

        for opt in options:
            # Value should match HH:MM format
            assert len(opt.value) == 5
            assert opt.value[2] == ":"
            hours, minutes = opt.value.split(":")
            assert hours.isdigit() and len(hours) == 2
            assert minutes.isdigit() and len(minutes) == 2

    def test_override_list_used_when_provided(self):
        """permission_duration_list_override takes precedence."""
        cfg = _make_config(max_hours=24, override=["01:00", "02:00", "03:00"])
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert values == ["01:00", "02:00", "03:00"]

    def test_small_max_includes_at_least_max(self):
        """Even with small max like 0.5h, max is included."""
        cfg = _make_config(max_hours=0.5)
        options = get_max_duration_block(cfg)

        values = [opt.value for opt in options]
        assert "00:30" in values


class TestFindInFields:
    """Tests for find_in_fields static method."""

    def test_parses_key_value_format(self):
        """Parses *Key*\\nValue format correctly."""
        fields = [{"text": "*Requester*\n<@U12345>"}]
        result = ButtonClickedPayload.find_in_fields(fields, "Requester")
        assert result == "<@U12345>"

    def test_strips_whitespace_from_value(self):
        """Strips leading/trailing whitespace from parsed value."""
        fields = [{"text": "*Duration*\n  0h 15m  "}]
        result = ButtonClickedPayload.find_in_fields(fields, "Duration")
        assert result == "0h 15m"

    def test_handles_multiline_values(self):
        """Handles values that contain newlines (only splits on first)."""
        fields = [{"text": "*Reason*\nLine 1\nLine 2"}]
        result = ButtonClickedPayload.find_in_fields(fields, "Reason")
        assert result == "Line 1\nLine 2"

    def test_raises_value_error_for_missing_key(self):
        """Raises ValueError when key not found."""
        fields = [{"text": "*Requester*\n<@U12345>"}]
        with pytest.raises(ValueError, match="Could not find MissingKey"):
            ButtonClickedPayload.find_in_fields(fields, "MissingKey")

    def test_finds_key_among_multiple_fields(self):
        """Finds correct key when multiple fields present."""
        fields = [
            {"text": "*Requester*\n<@U12345>"},
            {"text": "*Account*\nMyAccount (123456789012)"},
            {"text": "*Duration*\n1h 00m"},
        ]
        assert ButtonClickedPayload.find_in_fields(fields, "Account") == "MyAccount (123456789012)"
        assert ButtonClickedPayload.find_in_fields(fields, "Duration") == "1h 00m"


class TestButtonClickedPayload:
    """Tests for ButtonClickedPayload validation."""

    def _make_payload(self, **overrides: str) -> dict:
        """Create a realistic Slack button click payload with optional field overrides."""
        defaults = {
            "action": "approve",
            "requester": "<@U_REQUESTER>",
            "account": "TestAccount#123456789012",
            "permission_set": "AdminAccess",
            "duration": "0h 15m",
            "reason": "Testing",
        }
        fields = {**defaults, **overrides}
        return {
            "actions": [{"value": fields["action"]}],
            "user": {"id": "U_APPROVER"},
            "message": {
                "ts": "1234567890.123456",
                "blocks": [
                    {
                        "block_id": "content",
                        "fields": [
                            {"text": f"*Requester*\n{fields['requester']}"},
                            {"text": f"*Account*\n{fields['account']}"},
                            {"text": f"*Permission Set*\n{fields['permission_set']}"},
                            {"text": f"*Duration*\n{fields['duration']}"},
                            {"text": f"*Reason*\n{fields['reason']}"},
                        ],
                    }
                ],
            },
            "channel": {"id": "C_CHANNEL"},
        }

    def test_parses_approve_action(self):
        """Parses approve action from payload."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(action="approve"))
        assert payload.action.value == "approve"

    def test_parses_discard_action(self):
        """Parses discard action from payload."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(action="discard"))
        assert payload.action.value == "discard"

    def test_extracts_requester_slack_id(self):
        """Extracts requester ID from <@ID> format."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(requester="<@U_REQUESTER>"))
        assert payload.request.requester_slack_id == "U_REQUESTER"

    def test_extracts_account_id_from_hash_format(self):
        """Extracts account ID after # separator."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(account="TestAccount#123456789012"))
        assert payload.request.account_id == "123456789012"

    def test_extracts_permission_set_name(self):
        """Extracts permission set name from field."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(permission_set="AdminAccess"))
        assert payload.request.permission_set_name == "AdminAccess"

    def test_parses_duration(self):
        """Parses humanized duration into timedelta."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(duration="1h 30m"))
        assert payload.request.permission_duration == timedelta(hours=1, minutes=30)

    def test_extracts_reason(self):
        """Extracts reason from field."""
        payload = ButtonClickedPayload.model_validate(self._make_payload(reason="Need to debug production"))
        assert payload.request.reason == "Need to debug production"

    def test_extracts_approver_and_channel(self):
        """Extracts approver ID and channel ID."""
        payload = ButtonClickedPayload.model_validate(self._make_payload())
        assert payload.approver_slack_id == "U_APPROVER"
        assert payload.channel_id == "C_CHANNEL"

    def test_raises_on_missing_permission_set_field(self):
        """Raises ValueError if Permission Set field is missing."""
        bad_payload = self._make_payload()
        # Remove the Permission Set field
        bad_payload["message"]["blocks"][0]["fields"] = [
            f for f in bad_payload["message"]["blocks"][0]["fields"] if "Permission Set" not in f["text"]
        ]
        with pytest.raises(ValueError, match="Could not find Permission Set"):
            ButtonClickedPayload.model_validate(bad_payload)


class TestButtonGroupClickedPayload:
    """Tests for ButtonGroupClickedPayload validation."""

    def _make_payload(
        self,
        action: str = "approve",
        requester: str = "<@U_REQUESTER>",
        group: str = "TestGroup#group-123",
        duration: str = "0h 15m",
        reason: str = "Testing",
    ) -> dict:
        """Create a realistic Slack button click payload for group access."""
        return {
            "actions": [{"value": action}],
            "user": {"id": "U_APPROVER"},
            "message": {
                "ts": "1234567890.123456",
                "blocks": [
                    {
                        "block_id": "content",
                        "fields": [
                            {"text": f"*Requester*\n{requester}"},
                            {"text": f"*Group*\n{group}"},
                            {"text": f"*Duration*\n{duration}"},
                            {"text": f"*Reason*\n{reason}"},
                        ],
                    }
                ],
            },
            "channel": {"id": "C_CHANNEL"},
        }

    def test_parses_approve_action(self):
        """Parses approve action from payload."""
        payload = ButtonGroupClickedPayload.model_validate(self._make_payload(action="approve"))
        assert payload.action.value == "approve"

    def test_extracts_group_id_from_hash_format(self):
        """Extracts group ID after # separator."""
        payload = ButtonGroupClickedPayload.model_validate(self._make_payload(group="TestGroup#group-123"))
        assert payload.request.group_id == "group-123"

    def test_parses_duration(self):
        """Parses humanized duration into timedelta."""
        payload = ButtonGroupClickedPayload.model_validate(self._make_payload(duration="2h 00m"))
        assert payload.request.permission_duration == timedelta(hours=2)

    def test_raises_on_missing_group_field(self):
        """Raises ValueError if Group field is missing."""
        bad_payload = self._make_payload()
        bad_payload["message"]["blocks"][0]["fields"] = [
            f for f in bad_payload["message"]["blocks"][0]["fields"] if "Group" not in f["text"]
        ]
        with pytest.raises(ValueError, match="Could not find Group"):
            ButtonGroupClickedPayload.model_validate(bad_payload)
