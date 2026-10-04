"""Builders for CLI proof envelopes in tests, plus the shared golden vector from the Go CLI."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sts_proof" / "valid_envelope.json"

API_ID = "abcde12345"
REGION = "eu-central-1"
SIGNED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
SIGNATURE = "5" * 64
SESSION_TOKEN = "TEST-SESSION-TOKEN-MARKER"
NONCE = "0123456789abcdef0123456789abcdef"


def load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def default_payload() -> dict:
    return {"account": "111111111111", "permission_set": "Foo", "reason": "x", "duration": "1"}


def proof_query(region: str = REGION, signed_at: datetime = SIGNED_AT, signed_headers: str | None = None) -> dict[str, str]:
    return {
        "Action": "GetCallerIdentity",
        "Version": "2011-06-15",
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"ASIAIOSFODNN7EXAMPLE/{signed_at:%Y%m%d}/{region}/sts/aws4_request",
        "X-Amz-Date": f"{signed_at:%Y%m%dT%H%M%SZ}",
        "X-Amz-Expires": "60",
        "X-Amz-Security-Token": SESSION_TOKEN,
        "X-Amz-SignedHeaders": signed_headers or "host;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id",
        "X-Amz-Signature": SIGNATURE,
    }


def proof_url(query: dict[str, str], host: str = f"sts.{REGION}.amazonaws.com") -> str:
    return f"https://{host}/?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())


def make_body(  # noqa: PLR0913
    payload: dict | str | None = None,
    *,
    api_id: str = API_ID,
    nonce: str = NONCE,
    query: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict | None = None,
    extra_headers: dict | None = None,
) -> str:
    """A v1 envelope that passes every local check at SIGNED_AT unless an argument breaks it."""
    if payload is None:
        payload = default_payload()
    payload_text = payload if isinstance(payload, str) else json.dumps(payload)
    if headers is None:
        headers = {
            "x-elevator-payload-sha256": hashlib.sha256(payload_text.encode("utf-8", "surrogatepass")).hexdigest(),
            "x-elevator-server-id": api_id,
            "x-elevator-nonce": nonce,
        }
    headers = headers | (extra_headers or {})
    proof = {"url": url or proof_url(query or proof_query()), "headers": headers}
    return json.dumps({"version": 1, "payload": payload_text, "proof": proof}, ensure_ascii=False)


def sts_identity_xml(arn: str, account: str | None = None) -> bytes:
    account = account or arn.split(":")[4]
    return (
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<GetCallerIdentityResult><Arn>{arn}</Arn><UserId>AROAEXAMPLE:session</UserId><Account>{account}</Account>"
        "</GetCallerIdentityResult><ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata>"
        "</GetCallerIdentityResponse>"
    ).encode()


def sts_error_xml(code: str) -> bytes:
    return (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<Error><Type>Sender</Type><Code>{code}</Code>"
        f"<Message>The request signature we calculated does not match. Canonical request: X-Amz-Security-Token={SESSION_TOKEN}</Message>"
        "</Error><RequestId>req-1</RequestId></ErrorResponse>"
    ).encode()
