from datetime import timedelta
from types import SimpleNamespace

import permission_duration_options
from requester.teams import teams_cards


def test_parse_duration_choice_hh_mm_matches_slack_form() -> None:
    assert teams_cards.parse_duration_choice("00:30") == timedelta(minutes=30)
    assert teams_cards.parse_duration_choice("01:30") == timedelta(hours=1, minutes=30)


def test_permission_duration_strings_half_hour_grid() -> None:
    cfg = SimpleNamespace(max_permissions_duration_time=2, permission_duration_list_override=[])
    assert permission_duration_options.permission_duration_choice_strings(cfg) == ["00:30", "01:00", "01:30", "02:00"]


def test_permission_duration_respects_override_list() -> None:
    cfg = SimpleNamespace(max_permissions_duration_time=99, permission_duration_list_override=["a", "b"])
    assert permission_duration_options.permission_duration_choice_strings(cfg) == ["a", "b"]
