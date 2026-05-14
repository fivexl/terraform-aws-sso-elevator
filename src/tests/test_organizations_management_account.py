"""Unit tests for management account detection (Organizations API)."""

from unittest.mock import MagicMock

import organizations


def test_is_management_account() -> None:
    assert organizations.is_management_account("111122223333", "111122223333") is True
    assert organizations.is_management_account("111122223333", "  111122223333  ") is True
    assert organizations.is_management_account("111122223333", "999988887777") is False
    assert organizations.is_management_account("111122223333", None) is False
    assert organizations.is_management_account("", "111122223333") is False


def test_get_management_account_id_success() -> None:
    client = MagicMock()
    client.describe_organization.return_value = {"Organization": {"MasterAccountId": "123456789012"}}
    assert organizations.get_management_account_id(client) == "123456789012"
    client.describe_organization.assert_called_once()


def test_get_management_account_id_missing_field() -> None:
    client = MagicMock()
    client.describe_organization.return_value = {"Organization": {}}
    assert organizations.get_management_account_id(client) is None


def test_get_management_account_id_api_error() -> None:
    client = MagicMock()
    client.describe_organization.side_effect = RuntimeError("access denied")
    assert organizations.get_management_account_id(client) is None
