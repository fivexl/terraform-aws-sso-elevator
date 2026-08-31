from unittest.mock import MagicMock

import sso


def _client_returning(users: list[dict]) -> MagicMock:
    client = MagicMock()
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"Users": users}]
    return client


def test_find_email_by_username_matches_exact_username():
    """The realistic case this function exists for: RoleSessionName is an
    Identity Store username, not necessarily an email (e.g. an AD
    sAMAccountName) -- matched here by username, independent of what the
    user's actual email looks like."""
    client = _client_returning(
        [{"UserName": "jsmith", "Emails": [{"Value": "j.smith@company.com", "Primary": True}]}],
    )

    assert sso.find_email_by_username(client, "d-1234567890", "jsmith") == "j.smith@company.com"


def test_find_email_by_username_prefers_primary_email():
    client = _client_returning(
        [
            {
                "UserName": "jsmith",
                "Emails": [
                    {"Value": "secondary@company.com", "Primary": False},
                    {"Value": "primary@company.com", "Primary": True},
                ],
            }
        ],
    )

    assert sso.find_email_by_username(client, "d-1234567890", "jsmith") == "primary@company.com"


def test_find_email_by_username_falls_back_to_first_email_if_none_marked_primary():
    client = _client_returning(
        [{"UserName": "jsmith", "Emails": [{"Value": "only@company.com", "Primary": False}]}],
    )

    assert sso.find_email_by_username(client, "d-1234567890", "jsmith") == "only@company.com"


def test_find_email_by_username_returns_none_when_user_has_no_email():
    client = _client_returning([{"UserName": "jsmith", "Emails": []}])

    assert sso.find_email_by_username(client, "d-1234567890", "jsmith") is None


def test_find_email_by_username_returns_none_when_no_user_matches():
    client = _client_returning([{"UserName": "someone-else", "Emails": [{"Value": "x@company.com", "Primary": True}]}])

    assert sso.find_email_by_username(client, "d-1234567890", "jsmith") is None


def test_find_email_by_username_does_not_prefix_match_a_truncated_username():
    """A username truncated by RoleSessionName's 64-character limit is a
    known, accepted limitation -- this must not fall back to a fuzzy/prefix
    match, since that could resolve to the wrong person."""
    full_username = "jsmith.very.long.username.that.got.truncated@company.com"
    client = _client_returning([{"UserName": full_username, "Emails": [{"Value": "jsmith@company.com", "Primary": True}]}])

    # RoleSessionName caps at 64 characters, so simulate a real truncation by
    # slicing rather than hardcoding a second near-duplicate literal.
    assert sso.find_email_by_username(client, "d-1234567890", full_username[:44]) is None
