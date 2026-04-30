"""@mention configured approvers in the approval thread when a request needs human approval (Slack parity)."""

from __future__ import annotations

from dataclasses import dataclass

import config
import sso
from requester.teams.teams_notifier import TeamsGetApp, TeamsNotifier
from requester.teams.teams_threading import parent_activity_id_for_bot_thread_reply

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
    parent = (parent_activity_id_for_bot_thread_reply(teams_conversation_id, card_activity_id) or "").strip()
    if not parent:
        log.warning("Empty thread parent for approver ping; skipping")
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
                log.warning("Could not resolve approver in Teams: %s (%s)", e, ex)
        if u is not None:
            resolved.append(u)
            resolved_by_config_email.add(e)
    if graph is None and not resolved:
        log.warning("Graph client not configured; sending plain-text approver ping (emails)")

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
        await _post_thread_approver_line(tsend, parent, text, ent_list)
        return

    # No Graph or no resolvable user: plain text; show alternate domain (e.g. UPN) when secondary domains expand the list
    labels: list[str] = []
    for e in normalized:
        v = sso.ordered_email_variants_for_graph_lookup(e, cfg)
        labels.append(v[1] if len(v) > 1 else v[0])
    body = f"There is a request waiting for the approval. Approvers: {', '.join(labels)}"
    log.info("Sending plain-text approver waiting ping (Graph could not resolve any approver for @mention)")
    await _post_thread_approver_line(tsend, parent, body, None)
