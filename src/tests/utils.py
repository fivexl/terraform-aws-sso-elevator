import base64
import json
import os
import uuid
from urllib.parse import parse_qsl, urlencode

from aws_lambda_powertools.utilities.typing import LambdaContext


def decode_body(raw_body: str) -> dict:
    """Decode the body of a Slack request"""
    body = base64.b64decode(raw_body).decode("utf-8")
    body_dict = dict(parse_qsl(body))
    json_payload = body_dict["payload"]
    return json.loads(json_payload)


def encode_body(payload: dict) -> str:
    """Encode the body of a Slack request"""
    json_payload = json.dumps(payload, separators=(",", ":"))
    body_dict = {"payload": json_payload}
    qls = urlencode(body_dict)
    return base64.b64encode(qls.encode("utf-8")).decode("utf-8")


def get_lambda_env_vars(lambda_client, function_name: str, qualifier: str = "$LATEST") -> dict:
    print(f"Getting environment variables from lambda {function_name}:{qualifier}...")
    return lambda_client.get_function_configuration(FunctionName=function_name, Qualifier=qualifier)["Environment"]["Variables"]


def update_local_env_vars_from_lambda(lambda_client, function_name: str, qualifier: str = "$LATEST"):
    lambda_env_vars = get_lambda_env_vars(lambda_client, function_name, qualifier)
    os.environ |= lambda_env_vars
    print(f"Local environment variables updated from lambda {function_name}:{qualifier}!")


class LambdaTestContext(LambdaContext):
    def __init__(self, name: str, version: int = 1, region: str = "us-east-1", account_id: str = "111122223333"):
        self._function_name = name
        self._function_version = str(version)
        self._memory_limit_in_mb = 128
        self._invoked_function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{name}:{version}"
        self._aws_request_id = str(uuid.uuid4())
        self._log_group_name = f"/aws/lambda/{name}"
        self._log_stream_name = str(uuid.uuid4())
