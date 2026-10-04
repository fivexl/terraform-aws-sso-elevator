from unittest.mock import MagicMock, patch

import schedule


def test_revoke_schedule_names_are_unique_and_fit_the_scheduler_limit():
    """#212: grants scheduled in the same second must not share a name, even for the longest function name."""
    with patch.object(schedule, "cfg", MagicMock(revoker_function_name="r" * 64)):
        names = {schedule.revoke_schedule_name() for _ in range(100)}

    assert len(names) == 100  # noqa: PLR2004
    assert all(len(name) == schedule.SCHEDULE_NAME_MAX_LENGTH and name.startswith("r" * 36) for name in names)


def test_revoke_schedule_name_keeps_a_short_function_name_whole():
    with patch.object(schedule, "cfg", MagicMock(revoker_function_name="revoker")):
        name = schedule.revoke_schedule_name()

    assert name.startswith("revoker20")
    assert len(name) == len("revoker") + len("2026-01-01-00-00-00-") + 8
