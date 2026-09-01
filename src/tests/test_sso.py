from unittest.mock import MagicMock

import sso


def _users(users: list[dict]) -> dict:
    return {"Users": users}


def test_list_users_collects_users_across_pages():
    client = MagicMock()
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [
        {"Users": [{"UserName": "a"}]},
        {"Users": [{"UserName": "b"}]},
    ]

    result = sso.list_users(client, "d-1234567890")

    assert result == {"Users": [{"UserName": "a"}, {"UserName": "b"}]}
    client.get_paginator.assert_called_once_with("list_users")
    paginator.paginate.assert_called_once_with(IdentityStoreId="d-1234567890")


def test_find_email_by_username_matches_exact_username():
    """The realistic case this function exists for: RoleSessionName is an
    Identity Store username, not necessarily an email (e.g. an AD
    sAMAccountName) -- matched here by username, independent of what the
    user's actual email looks like."""
    list_of_users = _users([{"UserName": "jsmith", "Emails": [{"Value": "j.smith@company.com", "Primary": True}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") == "j.smith@company.com"


def test_find_email_by_username_prefers_primary_email():
    list_of_users = _users(
        [
            {
                "UserName": "jsmith",
                "Emails": [
                    {"Value": "secondary@company.com", "Primary": False},
                    {"Value": "primary@company.com", "Primary": True},
                ],
            }
        ]
    )

    assert sso.find_email_by_username(list_of_users, "jsmith") == "primary@company.com"


def test_find_email_by_username_falls_back_to_first_email_if_none_marked_primary():
    list_of_users = _users([{"UserName": "jsmith", "Emails": [{"Value": "only@company.com", "Primary": False}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") == "only@company.com"


def test_find_email_by_username_returns_none_when_user_has_no_email():
    list_of_users = _users([{"UserName": "jsmith", "Emails": []}])

    assert sso.find_email_by_username(list_of_users, "jsmith") is None


def test_find_email_by_username_returns_none_when_no_user_matches():
    list_of_users = _users([{"UserName": "someone-else", "Emails": [{"Value": "x@company.com", "Primary": True}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") is None


def test_find_email_by_username_does_not_prefix_match_a_truncated_username():
    """A username truncated by RoleSessionName's 64-character limit is a
    known, accepted limitation -- this must not fall back to a fuzzy/prefix
    match, since that could resolve to the wrong person."""
    full_username = "jsmith.very.long.username.that.got.truncated@company.com"
    list_of_users = _users([{"UserName": full_username, "Emails": [{"Value": "jsmith@company.com", "Primary": True}]}])

    # RoleSessionName caps at 64 characters, so simulate a real truncation by
    # slicing rather than hardcoding a second near-duplicate literal.
    assert sso.find_email_by_username(list_of_users, full_username[:44]) is None


def test_find_user_principal_id_by_email_matches_case_insensitively():
    list_of_users = _users([{"UserId": "u-1", "Emails": [{"Value": "Jane.Smith@Company.com"}]}])

    assert sso._find_user_principal_id_by_email("jane.smith@company.com", list_of_users) == "u-1"


def test_find_user_principal_id_by_email_returns_none_when_no_match():
    list_of_users = _users([{"UserId": "u-1", "Emails": [{"Value": "someone@company.com"}]}])

    assert sso._find_user_principal_id_by_email("nobody@company.com", list_of_users) is None


def test_find_user_principal_id_by_email_refuses_to_pick_between_case_colliding_users():
    """Regression test: two different Identity Store users whose emails
    differ only by case must not resolve to whichever happens to come first
    in list_users' pagination order -- that would grant access based on
    iteration order rather than a genuine, unambiguous identity match."""
    list_of_users = _users(
        [
            {"UserId": "u-1", "Emails": [{"Value": "jane.smith@company.com"}]},
            {"UserId": "u-2", "Emails": [{"Value": "Jane.Smith@Company.com"}]},
        ]
    )

    assert sso._find_user_principal_id_by_email("jane.smith@company.com", list_of_users) is None


def test_find_user_principal_id_by_email_same_user_repeated_email_is_not_a_collision():
    """A single user with the same email listed twice (e.g. once as primary,
    once as an identical secondary) is not an ambiguity -- only genuinely
    different UserIds should trigger the refuse-to-guess path."""
    list_of_users = _users(
        [{"UserId": "u-1", "Emails": [{"Value": "jane.smith@company.com", "Primary": True}, {"Value": "jane.smith@company.com"}]}]
    )

    assert sso._find_user_principal_id_by_email("jane.smith@company.com", list_of_users) == "u-1"
