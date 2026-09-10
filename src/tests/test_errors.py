"""Tests for errors.error_handler's message selection (#194 test gap): the
new AmbiguousSSOUser branch was only ever asserted as *raised* elsewhere
(e.g. test_sso.py), never as *rendered* to a user here -- the whole reason
it exists as its own exception type, rather than reusing SSOUserNotFound, is
that it needs its own, accurate user-facing message (see the class's own
docstring: SSOUserNotFound's "your AWS SSO email differs from your Slack
email" told a user hitting a case-insensitive email collision the opposite
of what actually happened)."""

from unittest.mock import MagicMock

import errors


def test_error_handler_gives_ambiguous_sso_user_its_own_message():
    client = MagicMock()
    context = {"user_id": "U123"}

    errors.error_handler(
        client=client,
        e=errors.AmbiguousSSOUser("Multiple SSO users share the email 'a@b.com' case-insensitively"),
        logger=MagicMock(),
        context=context,
        cfg=MagicMock(slack_channel_id="C123"),
    )

    client.chat_postMessage.assert_called_once()
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<@U123>" in text
    assert "more than one" in text.lower()
    # Must not be confused with (or fall through to) SSOUserNotFound's own
    # message, which claims the opposite situation (nobody found), or the
    # generic unexpected-error fallback.
    assert "differs from your slack email" not in text.lower()
    assert "unexpected error" not in text.lower()


def test_error_handler_gives_sso_user_not_found_its_own_message():
    client = MagicMock()
    context = {"user_id": "U123"}

    errors.error_handler(
        client=client,
        e=errors.SSOUserNotFound("User with email a@b.com not found in SSO"),
        logger=MagicMock(),
        context=context,
        cfg=MagicMock(slack_channel_id="C123"),
    )

    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<@U123>" in text
    assert "differs from your slack email" in text.lower()
    assert "more than one" not in text.lower()


def test_error_handler_falls_back_to_a_generic_message_for_anything_else():
    client = MagicMock()
    context = {"user_id": "U123"}

    errors.error_handler(
        client=client,
        e=RuntimeError("boom"),
        logger=MagicMock(),
        context=context,
        cfg=MagicMock(slack_channel_id="C123"),
    )

    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<@U123>" in text
    assert "unexpected error" in text.lower()


def test_error_handler_falls_back_to_unknown_user_when_context_has_none():
    """context.get("user_id", "UNKNOWN_USER") is the only thing standing
    between a missing user_id and an unhandled KeyError inside the error
    handler itself -- the one place an error must not itself raise."""
    client = MagicMock()

    errors.error_handler(
        client=client,
        e=RuntimeError("boom"),
        logger=MagicMock(),
        context={},
        cfg=MagicMock(slack_channel_id="C123"),
    )

    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<@UNKNOWN_USER>" in text
