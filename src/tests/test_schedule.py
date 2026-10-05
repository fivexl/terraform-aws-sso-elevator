import json
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import entities
import schedule
import sso
from events import Event, ScheduledRevokeEvent


def test_revoke_schedule_names_are_unique_and_fit_the_scheduler_limit():
    """#212: grants scheduled in the same second must not share a name, even for the longest function name."""
    with patch.object(schedule, "cfg", MagicMock(revoker_function_name="r" * 64)):
        names = {schedule.revoke_schedule_name() for _ in range(100)}

    assert len(names) == 100
    assert all(len(name) == schedule.SCHEDULE_NAME_MAX_LENGTH and name.startswith("r" * 36) for name in names)


def test_revoke_schedule_name_keeps_a_short_function_name_whole():
    with patch.object(schedule, "cfg", MagicMock(revoker_function_name="revoker")):
        name = schedule.revoke_schedule_name()

    assert re.fullmatch(r"revoker\d{4}(-\d{2}){5}-[0-9a-f]{8}", name)


def _schedule_revoke(request_source: str) -> dict:
    """The Input of the revoke schedule created for one account grant."""
    client = MagicMock()
    user = entities.slack.User(id="U1", email="u@example.com", real_name="U")
    with (
        patch.object(schedule, "cfg", MagicMock(revoker_function_name="revoker")),
        patch.object(schedule, "get_and_delete_scheduled_revoke_event_if_already_exist", return_value=[]),
    ):
        schedule.schedule_revoke_event(
            schedule_client=client,
            permission_duration=timedelta(hours=1),
            approver=user,
            requester=user,
            user_account_assignment=sso.UserAccountAssignment(
                instance_arn="i", account_id="111111111111", permission_set_arn="ps", user_principal_id="u"
            ),
            channel_id="C1",
            message_ts="1.1",
            request_source=request_source,
        )
    return json.loads(client.create_schedule.call_args.kwargs["Target"]["Input"])


def test_revoke_schedule_carries_the_request_source():
    event = Event.model_validate(_schedule_revoke("cli")).root

    assert isinstance(event, ScheduledRevokeEvent)
    assert event.revoke_event.request_source == "cli"


def test_revoke_schedule_created_before_the_request_source_still_parses():
    """Schedules already in flight at upgrade have no request_source; they parse as "NA"."""
    payload = _schedule_revoke("slack")
    revoke_event = json.loads(payload["revoke_event"])
    del revoke_event["request_source"]
    payload["revoke_event"] = json.dumps(revoke_event)

    assert Event.model_validate(payload).root.revoke_event.request_source == "NA"


def test_revoke_schedule_with_an_unknown_request_source_still_parses():
    """A rolled-back revoker must still see a newer schedule, or its sweep revokes that grant early."""
    payload = _schedule_revoke("slack")
    payload["revoke_event"] = json.dumps(json.loads(payload["revoke_event"]) | {"request_source": "api"})

    assert Event.model_validate(payload).root.revoke_event.request_source == "api"
