import asyncio

from requester.common.context import get_requester_context
from requester.teams.teams_deps import TeamsDependencies

# Mutable flag avoids ``global`` for one-time ``configure_teams_dependencies`` (PLW0603).
_teams_context_once: list[bool] = [False]


def lambda_handler(event: str, context: object) -> object:  # noqa: ANN001
    """Handle API Gateway (Slack or Teams) events; load only the selected chat stack."""
    ctx = get_requester_context()
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
