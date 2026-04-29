"""@mention configured approvers in the approval thread when a request needs human approval (Slack parity)."""

from __future__ import annotations

from dataclasses import dataclass

import config
from requester.teams.teams_notifier import TeamsGetApp, TeamsNotifier
from requester.teams.teams_threading import parent_activity_id_for_bot_thread_reply

from . import teams_users

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
    """Post one line in the approval card thread (Slack-style ping) via Bot Framework ``reply`` (in-thread)."""
    su = (t.service_url or "").strip() or None
    rn = TeamsNotifier(
        t.cfg,
        t.get_app,
        conversation_id_override=t.teams_conversation_id,
        service_url_override=su,
    )
    if entities:
        await rn.send_thread_reply_with_entities(parent, text, entities)
    else:
        await rn.send_thread_reply(parent, text)


async def send_approvers_waiting_ping_in_thread(  # noqa: PLR0913
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
    resolved: list = []
    if graph is not None:
        for e in normalized:
            try:
                resolved.append(await teams_users.get_user_by_email(graph, e))
            except Exception as ex:
                log.warning("Could not resolve approver in Teams: %s (%s)", e, ex)
    else:
        log.warning("Graph client not configured; sending plain-text approver ping (emails)")

    if resolved:
        parts: list[str] = []
        ent_list: list[dict] = []
        for u in resolved:
            t, ent = teams_users.build_mention(u.id, u.display_name)
            parts.append(t)
            ent_list.append(ent)
        text = f"{' '.join(parts)} {_SLACK_PARITY_LINE}"
        resolved_emails = {(u.email or "").strip().lower() for u in resolved}
        missing = [e for e in normalized if e not in resolved_emails]
        if missing:
            text = f"{text} Also notify: {', '.join(missing)}"
        await _post_thread_approver_line(tsend, parent, text, ent_list)
        return

    # No Graph or no resolvable user: same immediate thread line as Slack, with emails (no @mention entities)
    body = f"There is a request waiting for the approval. Approvers: {', '.join(normalized)}"
    log.info("Sending plain-text approver waiting ping (Graph could not resolve any approver for @mention)")
    await _post_thread_approver_line(tsend, parent, body, None)
