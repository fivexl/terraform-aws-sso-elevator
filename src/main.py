import asyncio

from requester.common.context import get_requester_context
from requester.teams.teams_approval_deferred import (
    ACCOUNT_APPROVAL_INTERNAL_ACTION,
    GROUP_APPROVAL_INTERNAL_ACTION,
    run_post_account_approval_worker,
    run_post_group_approval_worker,
)
from requester.teams.teams_deps import TeamsDependencies

# Mutable flag avoids ``global`` for one-time ``configure_teams_dependencies`` (PLW0603).
_teams_context_once: list[bool] = [False]


def lambda_handler(event: str, context: object) -> object:  # noqa: ANN001
    """Handle API Gateway (Slack or Teams) events; load only the selected chat stack."""
    if isinstance(event, dict) and event.get("internal_action") == ACCOUNT_APPROVAL_INTERNAL_ACTION:
        return asyncio.run(run_post_account_approval_worker(event))
    if isinstance(event, dict) and event.get("internal_action") == GROUP_APPROVAL_INTERNAL_ACTION:
        return asyncio.run(run_post_group_approval_worker(event))
    ctx = get_requester_context()

    # Inbound mutual-TLS gate: when enabled, only accept requests whose client certificate
    # (forwarded by API Gateway) presents the expected SAN. Internal self-invocations are
    # handled above and never reach here.
    if getattr(ctx.cfg, "require_slack_mtls", False):
        from requester.common.api_gateway import verify_client_cert_san

        ok, reason = verify_client_cert_san(event if isinstance(event, dict) else {}, ctx.cfg.slack_mtls_expected_san)
        if not ok:
            import config as _config

            _config.get_logger("main").warning("Rejecting request: mTLS client certificate check failed", extra={"reason": reason})
            return {"statusCode": 403, "headers": {"Content-Type": "text/plain"}, "body": "Forbidden"}

    if ctx.cfg.chat_platform == "teams":
        from requester.teams import teams_runtime

        if not _teams_context_once[0]:
            teams_runtime.configure_teams_dependencies(
                TeamsDependencies(
                    cfg=ctx.cfg,
                    org_client=ctx.org_client,
                    s3_client=ctx.s3_client,
                    sso_client=ctx.sso_client,
                    identity_store_client=ctx.identity_store_client,
                    schedule_client=ctx.schedule_client,
                )
            )
            _teams_context_once[0] = True
        return asyncio.run(teams_runtime.process_teams_lambda_event(event))

    from requester.slack.slack_app import get_slack_app
    from slack_bolt.adapter.aws_lambda import SlackRequestHandler

    return SlackRequestHandler(app=get_slack_app(ctx)).handle(event, context)
