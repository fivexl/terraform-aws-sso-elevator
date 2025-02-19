import functools

from aws_lambda_powertools import Logger
from slack_bolt import BoltContext
from slack_sdk import WebClient

import config


class ConfigurationError(Exception):
    ...


class AccountAssignmentError(ConfigurationError):
    ...


class NotFound(ConfigurationError):
    ...


class SSOUserNotFound(ConfigurationError):
    ...


logger = config.get_logger(service="errors")
cfg = config.get_config()


def error_handler(client: WebClient, e: Exception, logger: Logger, context: BoltContext, cfg: config.Config) -> None:
    logger.exception("An error occurred:", exc_info=e)
    user_id = context.get("user_id", "UNKNOWN_USER")

    if isinstance(e, SSOUserNotFound):
        text = (
            f"<@{user_id}> Your request for AWS permissions failed because SSO Elevator could not find your user in AWS SSO. "
            "This often happens if your AWS SSO email differs from your Slack email. "
            "Check the logs for more details."
        )
    else:
        text = f"<@{user_id}> Your request for AWS permissions encountered an unexpected error. " "Refer to the logs for more details."
    client.chat_postMessage(text=text, channel=cfg.slack_channel_id)


def handle_errors(fn):  # noqa: ANN001, ANN201
    # Default slack error handler (app.error) does not handle all exceptions. Or at least I did not find how to do it.
    # So I created this error handler.
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            client: WebClient = kwargs["client"]
            context: BoltContext = kwargs["context"]
            error_handler(client=client, e=e, logger=logger, context=context, cfg=cfg)

    return wrapper
