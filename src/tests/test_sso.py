import datetime
import io
import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

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


def test_list_users_with_cache_returns_projected_sorted_users_when_cache_is_disabled():
    """Regression test (#193 item 2): callers expect the {"Users": [...]} wrapper,
    not the bare list get_cached_users returns. Cache disabled so the real
    with_cache_resilience exercises only the API path."""
    client = MagicMock()
    paginator = MagicMock()
    client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [
        {"Users": [{"UserId": "u-2", "UserName": "b", "DisplayName": "B"}, {"UserId": "u-1", "UserName": "a", "DisplayName": "A"}]}
    ]
    cfg = MagicMock(cache_enabled=False, config_bucket_name="unused")

    result = sso.list_users_with_cache(client, "d-1234567890", MagicMock(), cfg)

    assert result == {"Users": [{"UserId": "u-1", "UserName": "a"}, {"UserId": "u-2", "UserName": "b"}]}


def _api_users() -> list[dict]:
    created = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    return [
        {
            "UserId": f"u-{i}",
            "UserName": f"user{i}",
            "Emails": [{"Value": f"user{i}@example.com", "Type": "work", "Primary": True}],
            "DisplayName": f"User {i}",
            "CreatedAt": created,
            "UpdatedAt": created,
            "IdentityStoreId": "d-1234567890",
        }
        for i in (1, 2)
    ]


_PROJECTED_USERS = [
    {"UserId": f"u-{i}", "UserName": f"user{i}", "Emails": [{"Value": f"user{i}@example.com", "Type": "work", "Primary": True}]}
    for i in (1, 2)
]


def _identitystore_client(pages) -> MagicMock:
    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = pages
    return client


def _s3_client(cached_body: bytes | None) -> MagicMock:
    s3 = MagicMock()
    s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    if cached_body is None:
        s3.get_object.side_effect = s3.exceptions.NoSuchKey()
    else:
        s3.get_object.return_value = {"Body": io.BytesIO(cached_body)}
    return s3


_CACHE_CFG = MagicMock(cache_enabled=True, config_bucket_name="test-config-bucket", config_bucket_kms_key_arn="")


def test_list_users_with_cache_writes_users_with_datetime_fields_projected():
    """Regression (#223): botocore returns CreatedAt/UpdatedAt as datetime,
    which made the users cache unwritable."""
    s3 = _s3_client(None)

    sso.list_users_with_cache(_identitystore_client(lambda **_: [{"Users": _api_users()}]), "d-1234567890", s3, _CACHE_CFG)

    s3.put_object.assert_called_once()
    assert json.loads(s3.put_object.call_args.kwargs["Body"]) == _PROJECTED_USERS


def test_list_users_with_cache_does_not_rewrite_unchanged_users_in_a_different_order():
    s3 = _s3_client(json.dumps(_PROJECTED_USERS).encode())
    client = _identitystore_client(lambda **_: [{"Users": list(reversed(_api_users()))}])

    result = sso.list_users_with_cache(client, "d-1234567890", s3, _CACHE_CFG)

    assert result == {"Users": _PROJECTED_USERS}
    s3.put_object.assert_not_called()


def test_list_users_with_cache_falls_back_to_what_it_wrote_when_list_users_throttles():
    first_s3 = _s3_client(None)
    sso.list_users_with_cache(_identitystore_client(lambda **_: [{"Users": _api_users()}]), "d-1234567890", first_s3, _CACHE_CFG)
    written = first_s3.put_object.call_args.kwargs["Body"]

    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "ListUsers")
    second_s3 = _s3_client(written)
    result = sso.list_users_with_cache(_identitystore_client(throttled), "d-1234567890", second_s3, _CACHE_CFG)

    assert result == {"Users": _PROJECTED_USERS}
    assert sso.find_email_by_username(result, "user2") == ("user2@example.com", "u-2")
    second_s3.put_object.assert_not_called()


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


def test_find_email_by_username_refuses_to_pick_between_two_matching_users():
    """Regression test (#194 B9, resolved by mirroring
    find_user_principal_id_by_email_strict's own collect-and-refuse shape):
    previously returned whichever matching user happened to appear first in
    list_of_users -- silently resolving to a possibly-wrong person on any
    RoleSessionName collision -- instead of refusing to guess."""
    list_of_users = _users(
        [
            {"UserId": "u-1", "UserName": "jsmith", "Emails": [{"Value": "john.smith@company.com", "Primary": True}]},
            {"UserId": "u-2", "UserName": "jsmith", "Emails": [{"Value": "jane.smith@company.com", "Primary": True}]},
        ]
    )

    with pytest.raises(errors.AmbiguousSSOUser, match="jsmith"):
        sso.find_email_by_username(list_of_users, "jsmith")


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
