"""Roster-based approver resolution: local-part match against channel members from the Bot API."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from requester.teams.teams_users import (
    fetch_channel_roster_teams_users,
    find_teams_user_in_roster_by_approver_email,
)
from entities.teams import TeamsUser


def test_find_teams_user_in_roster_matches_local_part_different_domain() -> None:
    roster = [
        TeamsUser(
            id="29:1",
            aad_object_id="aad1",
            email="jane.smith@contoso.onmicrosoft.com",
            display_name="Jane",
        )
    ]
    assert find_teams_user_in_roster_by_approver_email("jane.smith@fivexl.io", roster) is roster[0]
    assert find_teams_user_in_roster_by_approver_email("JANE.SMITH@contoso.onmicrosoft.com", roster) is roster[0]
    assert find_teams_user_in_roster_by_approver_email("other@fivexl.io", roster) is None


def test_fetch_channel_roster_builds_teams_users() -> None:
    m1 = SimpleNamespace(
        id="u1",
        aad_object_id="a1",
        email="a@b.com",
        name="A",
    )
    app = MagicMock()
    app.api.conversations.members.return_value.get_all = AsyncMock(return_value=[m1])

    out = asyncio.run(fetch_channel_roster_teams_users(app, "19:chan@thread.tacv2"))
    assert len(out) == 1
    assert out[0].id == "u1"
    assert out[0].email == "a@b.com"
    app.api.conversations.members.assert_called_once()
