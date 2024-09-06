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


logger = config.get_logger(service="errors")
cfg = config.get_config()


def error_handler(client: WebClient, e: Exception, logger: Logger, context: BoltContext, cfg: config.Config) -> None:
    logger.exception(e)
    if isinstance(e, ConfigurationError):
        text = f"<@{context['user_id']}> Your request for AWS permissions failed with error: {e}. Check logs for more details."
    else:
        text = f"<@{context['user_id']}> Your request for AWS permissions failed with error. Check access-requester logs for more details."

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
