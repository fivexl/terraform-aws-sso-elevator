"""Microsoft Teams App (microsoft-teams-apps) singleton and Lambda/HTTP event bridge."""

from __future__ import annotations

import asyncio
import json

import boto3
import config
from microsoft_teams.apps import App

from requester.common.api_gateway import normalize_api_gateway_headers, parse_api_gateway_event_json_body

from .teams_deps import TeamsDependencies

log = config.get_logger(service="teams_runtime")

_teams_app: App | None = None
# ``asyncio.run()`` (used in ``main.lambda_handler``) creates and then closes an event loop per
# invocation. The Teams SDK's httpx client is bound to that loop; reusing ``_teams_app`` across
# invocations causes ``RuntimeError: Event loop is closed`` on proactive sends (e.g. approval card).
_teams_app_loop_id: int | None = None
_deps: TeamsDependencies | None = None


def configure_teams_dependencies(deps: TeamsDependencies) -> None:
    """Call once per Lambda from main or revoker when chat platform is Teams."""
    global _deps, _teams_app, _teams_app_loop_id
    _deps = deps
    _teams_app = None
    _teams_app_loop_id = None


def _get_deps() -> TeamsDependencies:
    if _deps is None:
        c = config.get_config()
        session = boto3.Session()
        return TeamsDependencies(
            cfg=c,
            org_client=session.client("organizations"),  # type: ignore[assignment]
            s3_client=session.client("s3"),  # type: ignore[assignment]
            sso_client=session.client("sso-admin"),  # type: ignore[assignment]
            identity_store_client=session.client("identitystore"),  # type: ignore[assignment]
            schedule_client=session.client("scheduler"),  # type: ignore[assignment]
        )
    return _deps


async def get_teams_app() -> App:
    """Lazily build and initialize the Teams :class:`App` (credentials + routes)."""
    global _teams_app, _teams_app_loop_id
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    if _teams_app is not None and _teams_app_loop_id == loop_id:
        return _teams_app

    _teams_app = None
    _teams_app_loop_id = None

    deps = _get_deps()
    c = deps.cfg
    if not c.teams_microsoft_app_id or not c.teams_microsoft_app_password:
        raise ValueError("teams_microsoft_app_id and teams_microsoft_app_password are required for Teams mode")

    from . import teams_handlers

    app = App(
        client_id=c.teams_microsoft_app_id,
        client_secret=c.teams_microsoft_app_password,
        tenant_id=c.teams_azure_tenant_id,
        service_url=(f"https://smba.trafficmanager.net/{c.teams_azure_tenant_id}/" if c.teams_azure_tenant_id else None),
    )
    teams_handlers.register_teams_app_handlers(app, deps)
    await app.initialize()
    _teams_app = app
    _teams_app_loop_id = loop_id
    return app


async def process_teams_lambda_event(event: dict) -> dict:
    """Map API Gateway event through :meth:`App.server.handle_request` (JWT + routes)."""
    app = await get_teams_app()
    body, err = parse_api_gateway_event_json_body(event)
    if err is not None:
        return err
    if body is None:
        return {"statusCode": 400, "headers": {"Content-Type": "text/plain"}, "body": "Invalid body"}

    headers = normalize_api_gateway_headers(event.get("headers", {}) or {})
    try:
        raw = json.loads(body) if isinstance(body, str) else body
        if isinstance(raw, dict):
            # Log message text only: ``extra`` keys like ``name``/``type`` can clash with LogRecord/Powertools.
            log.info(
                "Teams incoming (pre-routing) activity_type=%s invoke=%s",
                raw.get("type"),
                raw.get("name"),
            )
    except Exception as e:
        log.warning("Teams incoming: could not log body summary: %s", str(e), exc_info=False)
    res = await app.server.handle_request({"body": body, "headers": headers})
    status = int(res.get("status", 200) or 200)
    log.info("Teams handle_request done", extra={"http_status": status})
    b = res.get("body")
    if b is None:
        body_str = ""
    elif isinstance(b, (dict, list)):
        body_str = json.dumps(b)
    else:
        body_str = str(b)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": body_str,
    }
