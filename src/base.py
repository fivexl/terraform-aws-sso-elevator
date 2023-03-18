import hashlib
import hmac
import http.client
import json
import logging
import os
import time


def read_env_variable_or_die(env_var_name):
    value = os.environ.get(env_var_name, "")
    if value == "":
        message = f"Required env variable {env_var_name} is not defined or set to empty string"
        raise EnvironmentError(message)
    return value


# Slack web hook example
# 
def post_slack_message(hook_url, message):
    print(f"Sending message: {json.dumps(message)}")
    headers = {"Content-type": "application/json"}
    connection = http.client.HTTPSConnection("hooks.slack.com")
    connection.request(
        "POST",
        hook_url.replace("https://hooks.slack.com", ""),
        json.dumps(message),
        headers,
    )
    response = connection.getresponse()
    print("Response: {}, message: {}".format(response.status, response.read().decode()))
    return response.status


"""
Method to verifty slack requests
Read more here https://api.slack.com/authentication/verifying-requests-from-slack
Example:
    from utils import verify_slack_request

    SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')

    logger.info('Received event: {}'.format(json.dumps(event)))

    if 'headers' not in event:
        logger.error('Request does not contain headers. Return 400')
        return { "statusCode": 400 }
    if 'body' not in event:
        logger.error('Request does not contain body. Return 400')
        return { "statusCode": 400 }
    request_headers = event['headers']
    body = event['body']
    # Verify request that it is coming from Slack
    try:
        verify_slack_request(request_headers, SLACK_SIGNING_SECRET, body)
    except Exception as error:
        logger.error(error)
        return  { "statusCode": 400 }
"""


def verify_slack_request(headers, signing_secret, body, age=60):
    timestamp = None
    if "X-Slack-Request-Timestamp" in headers:
        timestamp = headers["X-Slack-Request-Timestamp"]
    elif "x-slack-request-timestamp" in headers:
        timestamp = headers["x-slack-request-timestamp"]
    else:
        raise Exception(
            "Request does not have X-Slack-Request-Timestamp or x-slack-request-timestamp"
        )
    if not timestamp.isdigit():
        raise Exception(
            "Value of X-Slack-Request-Timestamp does not appear to be a digit"
        )

    request_signature = None
    if "X-Slack-Signature" in headers:
        request_signature = headers["X-Slack-Signature"]
    elif "x-slack-signature" in headers:
        request_signature = headers["x-slack-signature"]
    else:
        raise Exception("Request does not have X-Slack-Signature or x-slack-signature")
    if abs(time.time() - int(timestamp)) > age:
        # The request timestamp is more than five minutes from local time.
        # It could be a replay attack, so let's ignore it.
        raise Exception("Request is older than one minute")

    sig_basestring = f"v0:{timestamp}:{body}"
    signature = hmac.new(
        bytes(signing_secret, "utf8"),
        msg=bytes(sig_basestring, "utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    logging.info(f"computed signature = {signature}")
    if f"v0={signature}" != request_signature:
        raise Exception(
            f"Request computed signature v0={signature} "
            + f"is not equal to received one {request_signature}"
        )
    else:
        logging.info("Request looks legit. It is safe to process it")
