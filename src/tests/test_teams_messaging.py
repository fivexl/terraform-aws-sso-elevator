"""Property and unit tests for Microsoft Teams integration (cards, users, config, request store, events)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

import request_store
import s3
from requester.teams import teams_approval_deferred, teams_card_action_parse, teams_cards, teams_users
from entities import aws
from entities.teams import TeamsUser
from events import ApproverNotificationEvent, DiscardButtonsEvent
from tests.test_config import valid_config_dict

EMOJI_STYLES = {
    ":large_green_circle:": "good",
    ":large_yellow_circle:": "warning",
    ":red_circle:": "attention",
    ":white_circle:": "default",
}

_STYLE_CHOICES = st.sampled_from(["good", "warning", "attention", "default"])


def _count_type_in_body(body: list | None, el_type: str) -> int:
    if not body:
        return 0
    n = 0
    for el in body:
        if not isinstance(el, dict):
            continue
        if el.get("type") == el_type:
            n += 1
        n += _count_type_in_body(el.get("items"), el_type)  # type: ignore[arg-type]
    return n


def _head_container_style(card: dict) -> str | None:
    for item in card.get("body", []):
        if item.get("type") == "Container" and "style" in item:
            return item["style"]
    return None


@settings(max_examples=30, suppress_health_check=(HealthCheck.too_slow,))
@given(
    n_accounts=st.integers(min_value=1, max_value=5),
    n_perms=st.integers(min_value=1, max_value=5),
    n_dur=st.integers(min_value=1, max_value=5),
    n_groups=st.integers(min_value=1, max_value=5),
)
def test_property_form_card_choice_set_counts(
    n_accounts: int,
    n_perms: int,
    n_dur: int,
    n_groups: int,
) -> None:
    """Property 1: form cards have correct number of Input.ChoiceSet for accounts / groups / duration."""
    accounts = [aws.Account(id="1" * 12, name=f"acc{i}") for i in range(n_accounts)]
    psets = [aws.PermissionSet(name=f"ps{j}", arn=f"arn:ps{j}", description=None) for j in range(n_perms)]
    dur_opts = [f"{h}:00:00" for h in range(1, n_dur + 1)]
    acc_card = teams_cards.build_account_access_form(accounts, psets, dur_opts)
    assert _count_type_in_body(acc_card.get("body"), "Input.ChoiceSet") == 3

    groups = [aws.SSOGroup(id=f"group-id-{i}", name=f"G{i}", identity_store_id="is", description=None) for i in range(n_groups)]
    gcard = teams_cards.build_group_access_form(groups, dur_opts)
    assert _count_type_in_body(gcard.get("body"), "Input.ChoiceSet") == 2


@settings(max_examples=40, suppress_health_check=(HealthCheck.too_slow,))
@given(
    h=st.integers(min_value=0, max_value=48),
    m=st.integers(min_value=0, max_value=59),
    s=st.integers(min_value=0, max_value=59),
)
def test_property_duration_parse_roundtrip(h: int, m: int, s: int) -> None:
    """Property 2: task/submit duration H:M:S parses to matching total_seconds."""
    duration_str = f"{h}:{m:02d}:{s:02d}"
    td = teams_cards.parse_duration_choice(duration_str)
    p = duration_str.split(":")
    assert int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2]) == int(td.total_seconds())


@settings(max_examples=30, suppress_health_check=(HealthCheck.too_slow,))
@given(
    requester=st.text(min_size=1, max_size=60),
    acc_id=st.text(min_size=1, max_size=12, alphabet="0123456789"),
    role=st.text(min_size=1, max_size=20),
    reason=st.text(max_size=100),
    duration=st.sampled_from(["1:00:00", "2:30:00"]),
    style=_STYLE_CHOICES,
)
def test_property_approval_card_account_facts(requester: str, acc_id: str, role: str, reason: str, duration: str, style: str) -> None:
    """Property 3: account approval card FactSet contains required titles and values."""
    acc_digits = (acc_id * 2)[:12] if acc_id else "0" * 12
    account = aws.Account(id=acc_digits, name="acctname")
    card = teams_cards.build_approval_card(
        requester_name=requester,
        account=account,
        group=None,
        role_name=role,
        reason=reason,
        permission_duration=duration,
        show_buttons=True,
        color_style=style,
        request_data={"a": 1},
        elevator_request_id="eid-1",
    )
    fact_items = [x for x in card.get("body", []) if x.get("type") == "FactSet"]
    assert fact_items
    facts = {f["title"]: f["value"] for f in fact_items[0].get("facts", [])}
    assert facts["Requester"] == requester
    assert role in (facts.get("Role") or "")
    assert facts.get("Reason") == reason
    assert facts.get("Duration") == duration


@settings(max_examples=25, suppress_health_check=(HealthCheck.too_slow,))
@given(
    requester=st.text(min_size=1, max_size=40),
    gname=st.text(min_size=1, max_size=40),
    reason=st.text(max_size=80),
    duration=st.text(min_size=1, max_size=20),
    style=_STYLE_CHOICES,
    show_buttons=st.booleans(),
    eid=st.text(min_size=1, max_size=20),
    req_data=st.dictionaries(
        keys=st.text(min_size=1, max_size=5),
        values=st.one_of(st.text(max_size=10), st.integers()),
    ),
)
def test_property_approval_card_buttons(
    requester: str,
    gname: str,
    reason: str,
    duration: str,
    style: str,
    show_buttons: bool,
    eid: str,
    req_data: dict,
) -> None:
    """Property 4: when show_buttons True, two Action.Submit with elevator_request_id; else no actions."""
    g = aws.SSOGroup(
        id="g1-0000-0000-0000-000000000001",
        name=gname,
        identity_store_id="i",
        description=None,
    )
    card = teams_cards.build_approval_card(
        requester_name=requester,
        account=None,
        group=g,
        role_name=None,
        reason=reason,
        permission_duration=duration,
        show_buttons=show_buttons,
        color_style=style,
        request_data=req_data,
        elevator_request_id=eid,
    )
    actions = card.get("actions")
    if show_buttons:
        assert actions is not None
        subs = [a for a in actions if a.get("type") == "Action.Submit"]
        assert len(subs) == 2
        assert any(s.get("style") == "positive" for s in subs)
        assert any(s.get("style") == "destructive" for s in subs)
        for s in subs:
            assert s.get("data", {}).get("elevator_request_id") == eid
    else:
        assert not card.get("actions")


@settings(max_examples=20, suppress_health_check=(HealthCheck.too_slow,))
@given(
    requester=st.text(max_size=30),
    acc=st.sampled_from([aws.Account(id="1" * 12, name="A")]),
    style=_STYLE_CHOICES,
    dec=st.sampled_from(["discarded", "approved"]),
)
def test_property_card_state_transition(requester: str, acc, style: str, dec: str) -> None:  # noqa: ANN001
    """Property 7: update after decision preserves FactSet, removes actions, appends footer."""
    orig = teams_cards.build_approval_card(
        requester,
        acc,
        None,
        "R",
        "x",
        "1:0:0",
        True,
        "default",
        {},
        elevator_request_id="z",
    )
    assert orig.get("actions")
    u1 = teams_cards.update_card_after_decision(orig, dec, style)
    assert u1.get("actions") is None
    assert "FactSet" in {x.get("type") for x in u1.get("body", [])}
    assert any(f"Request {dec}" in (x.get("text") or "") for x in u1.get("body", []))
    assert _head_container_style(u1) == style

    u2 = teams_cards.update_card_on_expiry(orig, 3, "attention")
    assert u2.get("actions") is None
    assert "FactSet" in {x.get("type") for x in u2.get("body", [])}


@settings(max_examples=40, suppress_health_check=(HealthCheck.too_slow,))
@given(
    user_id=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters=("<>")), min_size=1, max_size=20),
    display_name=st.text(min_size=1, max_size=40),
)
def test_property_mention_and_teamsuser_slack(user_id: str, display_name: str) -> None:
    """Property 6 & 8: build_mention and to_slack_user() field compatibility."""
    text, ent = teams_users.build_mention(user_id, display_name)
    assert f"<at>{display_name}</at>" in text
    assert ent["mentioned"]["id"] == user_id
    tu = TeamsUser(id="tid", aad_object_id="aad", email="e@e.com", display_name="DN")
    su = tu.to_slack_user()
    assert su.id == "tid" and su.email == "e@e.com" and su.real_name == "DN"


@settings(max_examples=20, suppress_health_check=(HealthCheck.too_slow,))
@given(
    e_req=st.emails(),
    e_app=st.emails(),
    rid=st.text(alphabet="0123456789abcdef", min_size=8, max_size=20),
    aid=st.text(alphabet="0123456789abcdef", min_size=8, max_size=20),
)
def test_property_audit_teams_user_fields(e_req: str, e_app: str, rid: str, aid: str) -> None:
    """Property 9: AuditEntry from Teams-compatible slack users has non-empty id/email fields."""
    t_req = TeamsUser(id=rid, aad_object_id="a", email=e_req, display_name="R")
    t_app = TeamsUser(id=aid, aad_object_id="b", email=e_app, display_name="A")
    u_r = t_req.to_slack_user()
    u_a = t_app.to_slack_user()
    entry = s3.AuditEntry(
        group_name="g",
        group_id="gid",
        reason="r",
        requester_slack_id=u_r.id,
        requester_email=u_r.email,
        approver_slack_id=u_a.id,
        approver_email=u_a.email,
        operation_type="grant",
        permission_duration=timedelta(hours=1),
        sso_user_principal_id="p",
        audit_entry_type="group",
    )
    assert entry.requester_email and entry.approver_email
    assert entry.requester_slack_id != "NA" and entry.approver_slack_id != "NA"


# --- Unit tests (14.x) ---


def test_parse_adaptive_card_invoke_nested() -> None:
    nested: dict = {
        "msteams": {"type": "task/submit"},
        "action": {
            "type": "Action.Submit",
            "data": {"account_id": "1", "elevator_request_id": "e-99", "action": "discard"},
        },
    }
    eid, act = teams_card_action_parse.parse_adaptive_card_invoke_value(nested)
    assert eid == "e-99"
    assert act == "discard"


def test_value_from_message_activity_value_json_and_channeldata() -> None:
    """Some Teams clients send card submit as type=message: payload in value, JSON text, or channelData."""
    p = {"elevator_request_id": "e-1", "action": "approve"}
    v0 = teams_card_action_parse.value_from_message_activity_for_adaptive_submit(SimpleNamespace(value=p, text=None, channel_data=None))
    assert v0 == p
    eid, act = teams_card_action_parse.parse_adaptive_card_invoke_value(v0)
    assert eid == "e-1" and act == "approve"

    text_json = '{"elevator_request_id": "e-2", "action": "discard"}'
    v1 = teams_card_action_parse.value_from_message_activity_for_adaptive_submit(
        SimpleNamespace(value=None, text=text_json, channel_data=None)
    )
    eid, act = teams_card_action_parse.parse_adaptive_card_invoke_value(v1)
    assert eid == "e-2" and act == "discard"

    chd = {"elevator_request_id": "e-3", "action": "approve"}
    v2 = teams_card_action_parse.value_from_message_activity_for_adaptive_submit(
        SimpleNamespace(value=None, text="plain", channel_data=chd)
    )
    eid, act = teams_card_action_parse.parse_adaptive_card_invoke_value(v2)
    assert eid == "e-3" and act == "approve"


def test_get_color_style_maps_emoji_emoji() -> None:
    for em, expected in EMOJI_STYLES.items():
        assert teams_cards.get_color_style(em) == expected
    assert teams_cards.get_color_style("unknown:emoji:") == "default"


def test_account_approval_deferred_hmac_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_MICROSOFT_APP_PASSWORD", "test-secret-for-hmac")
    conv = "19:test@thread.tacv2"
    su = "https://smba.trafficmanager.net/tenant-guid/"
    parent = "1777337366075"
    th0 = teams_approval_deferred.AccountApprovalTeamsThread(conv, "", "")
    sig = teams_approval_deferred.sign_account_approval_post("eid-1", "User@Example.Com", th0)
    assert teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th0, sig)
    assert not teams_approval_deferred.verify_account_approval_post("eid-2", "user@example.com", th0, sig)
    th_su = teams_approval_deferred.AccountApprovalTeamsThread(conv, su, "")
    sig2 = teams_approval_deferred.sign_account_approval_post("eid-1", "User@Example.Com", th_su)
    assert teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th_su, sig2)
    assert not teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th0, sig2)
    th_par = teams_approval_deferred.AccountApprovalTeamsThread(conv, su, parent)
    sig3 = teams_approval_deferred.sign_account_approval_post("eid-1", "User@Example.Com", th_par)
    assert teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th_par, sig3)
    th_lid = teams_approval_deferred.AccountApprovalTeamsThread(conv, su, parent, "launcher-act-1")
    sig4 = teams_approval_deferred.sign_account_approval_post("eid-1", "User@Example.Com", th_lid)
    assert teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th_lid, sig4)
    assert sig4 != sig3
    th_other = teams_approval_deferred.AccountApprovalTeamsThread(conv, su, "other")
    assert not teams_approval_deferred.verify_account_approval_post("eid-1", "user@example.com", th_other, sig3)


def test_request_access_launcher_card_triggers_task_fetch() -> None:
    """Launcher card must use task/fetch so Teams opens the task module (message reply alone cannot)."""
    for kind in ("account", "group"):
        card = teams_cards.build_request_access_launcher_card(kind)
        actions = card.get("actions") or []
        assert len(actions) == 1
        data = actions[0].get("data") or {}
        assert data.get("kind") == kind
        assert (data.get("msteams") or {}).get("type") == "task/fetch"


def test_request_access_launcher_submitted_card_has_no_open_action() -> None:
    """After submit, launcher is replaced with a card that has no ``Action.Submit`` (button hidden)."""
    for kind in ("account", "group"):
        card = teams_cards.build_request_access_launcher_submitted_card(kind)
        assert not card.get("actions")
        assert "Form submitted" in str(card)


def test_teams_config_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    import config as config_module

    for k, v in {**valid_config_dict(), "chat_platform": "teams"}.items():
        v_e = v
        if k in ("teams_microsoft_app_id", "teams_microsoft_app_password", "teams_azure_tenant_id", "teams_approval_conversation_id"):
            v_e = ""
        monkeypatch.setenv(k, str(v_e))
    config_module._config = None
    with pytest.raises(ValueError) as e:
        config_module.Config()  # type: ignore[call-arg]
    assert "Teams platform requires" in str(e.value)
    for t, v in {
        "teams_microsoft_app_id": "a",
        "teams_microsoft_app_password": "b",
        "teams_azure_tenant_id": "c",
        "teams_approval_conversation_id": "d",
    }.items():
        monkeypatch.setenv(t, v)
    config_module._config = None
    c = config_module.Config()  # type: ignore[call-arg]
    assert c.chat_platform == "teams"
    assert c.teams_approval_conversation_id == "d"


def test_request_store_teams_extensions_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in valid_config_dict().items():
        monkeypatch.setenv(k, str(v))
    import config as config_module

    config_module._config = None
    request_store._memory.clear()  # noqa: SLF001
    eid = "e-test-1"
    request_store._memory[eid] = {"k": 1}  # noqa: SLF001
    request_store.update_teams_presentation(eid, "conv-1", "act-1")
    assert request_store._memory[eid]["teams_conversation_id"] == "conv-1"  # noqa: SLF001
    assert request_store._memory[eid]["teams_activity_id"] == "act-1"  # noqa: SLF001
    assert request_store.get_teams_presentation_ids(eid) == ("conv-1", "act-1")
    assert request_store.get_teams_presentation_ids("no-such") is None
    ref = {"c": 3}
    user_aad = "aad-99"
    request_store.save_conversation_reference(user_aad, ref)
    assert request_store.get_conversation_reference(user_aad) == ref


def test_event_models_roundtrip_teams_optional() -> None:
    d0 = {
        "action": "discard_buttons_event",
        "schedule_name": "s",
        "time_stamp": "1",
        "channel_id": "C",
        "teams_conversation_id": "c1",
        "teams_activity_id": "a1",
    }
    m = TypeAdapter(DiscardButtonsEvent)
    b = m.validate_python(d0)
    assert b.teams_conversation_id == "c1" and b.teams_activity_id == "a1"
    d1 = {
        "action": "approvers_renotification",
        "schedule_name": "s2",
        "time_stamp": "1",
        "channel_id": "C",
        "time_to_wait_in_seconds": 10.0,
        "teams_conversation_id": "x",
    }
    a = TypeAdapter(ApproverNotificationEvent).validate_python(d1)
    assert a.teams_conversation_id == "x" and a.teams_activity_id is None
    slack_only = {**d0, "teams_conversation_id": None, "teams_activity_id": None}
    assert TypeAdapter(DiscardButtonsEvent).validate_python(slack_only) is not None


def test_teamsuser_to_slack() -> None:
    t = TeamsUser(
        id="u1",
        aad_object_id="a1",
        email="m@x.com",
        display_name="Me",
    )
    s = t.to_slack_user()
    assert s.id == "u1" and s.email == "m@x.com" and s.real_name == "Me"


def test_build_mention_structure() -> None:
    text, e = teams_users.build_mention("u", "N")
    assert text == "<at>N</at>"
    assert e["type"] == "mention"
    assert e["mentioned"]["id"] == "u"


# Property 5: any valid style is applied
@settings(max_examples=10, suppress_health_check=(HealthCheck.too_slow,))
@given(style=_STYLE_CHOICES)
def test_property_five_color_style_in_card(style: str) -> None:
    card = teams_cards.build_approval_card(
        "R",
        aws.Account(id="1" * 12, name="A"),
        None,
        "role",
        "why",
        "1:0:0",
        False,
        style,
        {},
    )
    assert _head_container_style(card) == style
    orig = teams_cards.build_approval_card(
        "R",
        aws.Account(id="1" * 12, name="A"),
        None,
        "r",
        "q",
        "1:0:0",
        True,
        "default",
        {"x": 1},
        elevator_request_id="e2",
    )
    upd = teams_cards.update_card_after_decision(orig, "approved", style)
    assert _head_container_style(upd) == style
