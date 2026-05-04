"""@mention configured approvers in the approval thread when a request needs human approval (Slack parity)."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

import httpx

import config
import entities
import sso
from requester.teams.teams_notifier import (
    TeamsGetApp,
    TeamsNotifier,
)
from requester.teams.teams_threading import thread_follow_up_reply_parent_candidates

from . import teams_notifier, teams_users

log = config.get_logger(service="teams_approver_ping")

_SLACK_PARITY_LINE = "there is a request waiting for the approval."


@dataclass(frozen=True, slots=True)
class _TeamsThreadSend:
    cfg: config.Config
    get_app: TeamsGetApp
    teams_conversation_id: str
    service_url: str | None


def _graph_client(cfg: config.Config) -> object | None:
    t = (cfg.teams_azure_tenant_id or "").strip()
    cid = (cfg.teams_microsoft_app_id or "").strip()
    sec = (cfg.teams_microsoft_app_password or "").strip()
    if not t or not cid or not sec:
        return None
    from azure.identity import ClientSecretCredential
    from msgraph import GraphServiceClient

    cred = ClientSecretCredential(tenant_id=t, client_id=cid, client_secret=sec)
    return GraphServiceClient(credentials=cred, scopes=["https://graph.microsoft.com/.default"])


async def _post_thread_approver_line(
    t: _TeamsThreadSend,
    parent: str,
    text: str,
    entities: list[dict] | None,
) -> None:
    """In-thread line via :meth:`TeamsNotifier.send_thread_text_with_transport_fallback`."""
    su = (t.service_url or "").strip() or None
    rn = TeamsNotifier(
        t.cfg,
        t.get_app,
        conversation_id_override=t.teams_conversation_id,
        service_url_override=su,
    )
    await rn.send_thread_text_with_transport_fallback(parent, text, entities)


async def _post_thread_line_with_parent_candidates(
    t: _TeamsThreadSend,
    teams_conversation_id: str,
    card_activity_id: str,
    text: str,
    entities: list[dict] | None,
) -> None:
    parents = thread_follow_up_reply_parent_candidates(teams_conversation_id, card_activity_id)
    if not parents:
        log.warning("Empty thread parent candidates for approver ping; skipping")
        return
    last: httpx.HTTPStatusError | None = None
    for parent in parents:
        try:
            await _post_thread_approver_line(t, parent, text, entities)
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                last = e
                continue
            raise
    if last is not None:
        raise last


async def _resolve_single_teams_user_by_email(
    cfg: config.Config,
    get_app: TeamsGetApp,
    *,
    teams_conversation_id: str,
    service_url: str | None,
    email: str,
) -> entities.teams.TeamsUser | None:
    """Match one address to a channel member or Graph user (same strategy as approver ping)."""
    e = (email or "").strip().lower()
    if not e:
        return None
    app = await get_app()
    roster: list = []
    try:
        base_cid = teams_notifier.base_approval_channel_conversation_id(teams_conversation_id, cfg)
        roster = await teams_users.fetch_channel_roster_teams_users(
            app,
            base_cid,
            service_url=service_url,
            fallback_tenant_id=cfg.teams_azure_tenant_id,
        )
    except Exception as ex:
        log.exception("Could not load channel roster for grant mention: %s", ex)
    if roster:
        u = teams_users.find_teams_user_in_roster_by_approver_email(e, roster)
        if u is not None:
            return u
    graph = _graph_client(cfg)
    if graph is None:
        return None
    try:
        return await teams_users.get_user_by_email_with_config(graph, e, cfg)
    except Exception as ex:
        log.warning("Could not resolve requester in Teams for grant mention: %s", ex)
        return None


async def post_auto_grant_thread_follow_ups(  # noqa: PLR0913
    cfg: config.Config,
    get_app: TeamsGetApp,
    *,
    teams_conversation_id: str,
    service_url: str | None,
    card_activity_id: str,
    status_text: str | None,
    requester: entities.teams.TeamsUser | None = None,
    requester_email: str = "",
    requester_display_name: str = "",
    include_status: bool = True,
    include_grant_line: bool = True,
) -> None:
    """Post auto-grant explanation and/or grantor line in the approval thread (Slack parity)."""
    if not thread_follow_up_reply_parent_candidates(teams_conversation_id, card_activity_id):
        log.warning("Empty thread parent candidates for auto-grant follow-up; skipping")
        return
    tsend = _TeamsThreadSend(
        cfg=cfg,
        get_app=get_app,
        teams_conversation_id=teams_conversation_id,
        service_url=(service_url or "").strip(),
    )
    if include_status and (status_text or "").strip():
        await _post_thread_line_with_parent_candidates(
            tsend,
            teams_conversation_id,
            card_activity_id,
            (status_text or "").strip(),
            None,
        )
    if not include_grant_line:
        return

    u = requester if (requester and str(requester.id or "").strip()) else None
    if u is None and (requester_email or "").strip():
        u = await _resolve_single_teams_user_by_email(
            cfg,
            get_app,
            teams_conversation_id=teams_conversation_id,
            service_url=service_url,
            email=requester_email,
        )
    fallback_name = (
        (requester_display_name or "").strip()
        or (requester.display_name if requester else "")
        or (u.display_name if u else "")
        or (requester_email or "").strip()
        or "Requester"
    )
    if u and str(u.id or "").strip():
        m, ent = teams_users.build_mention(u.id, (u.display_name or "").strip() or fallback_name)
        grant_line = f"Permissions have been granted by {m}."
        await _post_thread_line_with_parent_candidates(tsend, teams_conversation_id, card_activity_id, grant_line, [ent])
    else:
        plain = f"Permissions have been granted by {fallback_name}."
        await _post_thread_line_with_parent_candidates(tsend, teams_conversation_id, card_activity_id, plain, None)


async def send_approvers_waiting_ping_in_thread(  # noqa: PLR0913, PLR0912
    cfg: config.Config,
    get_app: TeamsGetApp,
    *,
    teams_conversation_id: str,
    service_url: str | None,
    card_activity_id: str,
    approver_emails: frozenset[str],
) -> None:
    """Post a short thread reply with @mentions when Microsoft Graph can resolve approvers; else plain text (emails).

    Graph ``User.Read.All`` (or equivalent) is often missing and returns 403 — Slack parity still needs an
    immediate line in the thread so approvers see the request.
    """
    if not approver_emails:
        return
    if not thread_follow_up_reply_parent_candidates(teams_conversation_id, card_activity_id):
        log.warning("Empty thread parent candidates for approver ping; skipping")
        return

    normalized: list[str] = []
    for email in approver_emails:
        e = (email or "").strip().lower()
        if e:
            normalized.append(e)
    if not normalized:
        return

    tsend = _TeamsThreadSend(
        cfg=cfg,
        get_app=get_app,
        teams_conversation_id=teams_conversation_id,
        service_url=service_url,
    )
    graph = _graph_client(cfg)
    roster: list = []
    try:
        app = await get_app()
        base_cid = teams_notifier.base_approval_channel_conversation_id(teams_conversation_id, cfg)
        roster = await teams_users.fetch_channel_roster_teams_users(
            app,
            base_cid,
            service_url=service_url,
            fallback_tenant_id=cfg.teams_azure_tenant_id,
        )
        if roster:
            log.info("Channel roster loaded for approver match: %d members", len(roster))
    except Exception as ex:
        log.exception("Could not load channel roster for approver match: %s", ex)
        roster = []
    resolved: list = []
    resolved_by_config_email: set[str] = set()
    for e in normalized:
        u = None
        if roster:
            u = teams_users.find_teams_user_in_roster_by_approver_email(e, roster)
        if u is not None:
            resolved.append(u)
            resolved_by_config_email.add(e)
            continue
        if graph is not None:
            try:
                u = await teams_users.get_user_by_email_with_config(graph, e, cfg)
            except Exception as ex:
                # Avoid logging raw approver emails (PII) on resolution failures.
                log.warning("Could not resolve an approver in Teams: %s", ex)
        if u is not None:
            resolved.append(u)
            resolved_by_config_email.add(e)
    if graph is None and not resolved:
        log.info("Graph client not configured; sending plain-text approver ping (emails)")

    if resolved:
        parts: list[str] = []
        ent_list: list[dict] = []
        for u in resolved:
            t, ent = teams_users.build_mention(u.id, u.display_name)
            parts.append(t)
            ent_list.append(ent)
        text = f"{' '.join(parts)} {_SLACK_PARITY_LINE}"
        missing = [e for e in normalized if e not in resolved_by_config_email]
        if missing:
            miss_labels: list[str] = []
            for e in missing:
                mv = sso.ordered_email_variants_for_graph_lookup(e, cfg)
                miss_labels.append(mv[1] if len(mv) > 1 else mv[0])
            text = f"{text} Also notify: {', '.join(miss_labels)}"
        await _post_thread_line_with_parent_candidates(tsend, teams_conversation_id, card_activity_id, text, ent_list)
        return

    # No Graph or no resolvable user: plain text; show alternate domain (e.g. UPN) when secondary domains expand the list
    labels: list[str] = []
    for e in normalized:
        v = sso.ordered_email_variants_for_graph_lookup(e, cfg)
        labels.append(v[1] if len(v) > 1 else v[0])
    body = f"There is a request waiting for approval. Approvers: {', '.join(labels)}"
    log.info("Sending plain-text approver waiting ping (Graph could not resolve any approver for @mention)")
    await _post_thread_line_with_parent_candidates(tsend, teams_conversation_id, card_activity_id, body, None)
