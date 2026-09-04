from unittest.mock import MagicMock

import pytest

import errors
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
    list_of_users = _users([{"UserId": "u-1", "UserName": "jsmith", "Emails": [{"Value": "j.smith@company.com", "Primary": True}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") == ("j.smith@company.com", "u-1")


def test_find_email_by_username_prefers_primary_email():
    list_of_users = _users(
        [
            {
                "UserId": "u-1",
                "UserName": "jsmith",
                "Emails": [
                    {"Value": "secondary@company.com", "Primary": False},
                    {"Value": "primary@company.com", "Primary": True},
                ],
            }
        ]
    )

    assert sso.find_email_by_username(list_of_users, "jsmith") == ("primary@company.com", "u-1")


def test_find_email_by_username_falls_back_to_first_email_if_none_marked_primary():
    list_of_users = _users([{"UserId": "u-1", "UserName": "jsmith", "Emails": [{"Value": "only@company.com", "Primary": False}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") == ("only@company.com", "u-1")


def test_find_email_by_username_returns_none_when_user_has_no_email():
    list_of_users = _users([{"UserId": "u-1", "UserName": "jsmith", "Emails": []}])

    assert sso.find_email_by_username(list_of_users, "jsmith") is None


def test_find_email_by_username_returns_none_when_no_user_matches():
    list_of_users = _users([{"UserId": "u-1", "UserName": "someone-else", "Emails": [{"Value": "x@company.com", "Primary": True}]}])

    assert sso.find_email_by_username(list_of_users, "jsmith") is None


def test_find_email_by_username_does_not_prefix_match_a_truncated_username():
    """A username truncated by RoleSessionName's 64-character limit is a
    known, accepted limitation -- this must not fall back to a fuzzy/prefix
    match, since that could resolve to the wrong person."""
    full_username = "jsmith.very.long.username.that.got.truncated@company.com"
    list_of_users = _users([{"UserId": "u-1", "UserName": full_username, "Emails": [{"Value": "jsmith@company.com", "Primary": True}]}])

    # RoleSessionName caps at 64 characters, so simulate a real truncation by
    # slicing rather than hardcoding a second near-duplicate literal.
    assert sso.find_email_by_username(list_of_users, full_username[:44]) is None


def test_find_user_principal_id_by_email_strict_matches_case_insensitively():
    list_of_users = _users([{"UserId": "u-1", "Emails": [{"Value": "Jane.Smith@Company.com"}]}])

    assert sso.find_user_principal_id_by_email_strict("jane.smith@company.com", list_of_users) == "u-1"


def test_find_user_principal_id_by_email_strict_returns_none_when_no_match():
    list_of_users = _users([{"UserId": "u-1", "Emails": [{"Value": "someone@company.com"}]}])

    assert sso.find_user_principal_id_by_email_strict("nobody@company.com", list_of_users) is None


def test_find_user_principal_id_by_email_strict_refuses_to_pick_between_case_colliding_users():
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

    with pytest.raises(errors.AmbiguousSSOUser):
        sso.find_user_principal_id_by_email_strict("jane.smith@company.com", list_of_users)


def test_get_user_principal_id_by_email_does_not_fall_through_to_secondary_domain_on_collision():
    """Regression test: a None return from find_user_principal_id_by_email_strict
    means "try the next secondary fallback domain" -- so before it was made
    to raise on a collision instead, get_user_principal_id_by_email would
    silently resolve an ambiguous primary-email lookup to a third, unrelated
    user via secondary_fallback_email_domains, rather than stopping at the
    ambiguity. This proves the fallback domain is never even queried."""
    client = MagicMock()
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [
        {
            "Users": [
                {"UserId": "u-1", "Emails": [{"Value": "jane.smith@company.com", "Primary": True}]},
                {"UserId": "u-2", "Emails": [{"Value": "Jane.Smith@Company.com", "Primary": True}]},
                # The third, unrelated user a naive fallback could have
                # resolved to, if the collision above were ignored.
                {"UserId": "u-3", "Emails": [{"Value": "jane.smith@fallback.com", "Primary": True}]},
            ]
        }
    ]
    cfg = MagicMock(secondary_fallback_email_domains=["@fallback.com"])

    with pytest.raises(errors.AmbiguousSSOUser):
        sso.get_user_principal_id_by_email(client, "d-1234567890", "jane.smith@company.com", cfg)


def test_find_user_principal_id_by_email_strict_same_user_repeated_email_is_not_a_collision():
    """A single user with the same email listed twice (e.g. once as primary,
    once as an identical secondary) is not an ambiguity -- only genuinely
    different UserIds should trigger the refuse-to-guess path."""
    list_of_users = _users(
        [{"UserId": "u-1", "Emails": [{"Value": "jane.smith@company.com", "Primary": True}, {"Value": "jane.smith@company.com"}]}]
    )

    assert sso.find_user_principal_id_by_email_strict("jane.smith@company.com", list_of_users) == "u-1"
