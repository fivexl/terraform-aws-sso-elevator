"""Verifies the elevator CLI's caller-identity proof: a presigned sts:GetCallerIdentity request
bound to the request it travels with. The CLI half lives in cmd/elevator/proof.go; a shared
golden vector in tests/fixtures/sts_proof/ keeps the two ends in step.

Wire contract, version 1. The CLI POSTs this JSON body to the REST API's CLI route:

    {"version": 1,
     "payload": "<operation JSON, as a string>",
     "proof": {"url": "<presigned STS URL>", "headers": {"<name>": "<value>", ...}}}

- payload: a raw JSON string (not base64), hashed as SHA-256 lowercase hex over its UTF-8 bytes,
  and parsed only after that hash matches x-elevator-payload-sha256.
- proof.url: GET https://sts.<AWS_REGION>.amazonaws.com/ with exactly the query parameters
  Action=GetCallerIdentity, Version=2011-06-15, X-Amz-Algorithm=AWS4-HMAC-SHA256,
  X-Amz-Credential (scope <date>/<AWS_REGION>/sts/aws4_request), X-Amz-Date, X-Amz-Expires (1-900),
  X-Amz-SignedHeaders, X-Amz-Signature, and optionally X-Amz-Security-Token. AWS_REGION is the
  Lambda's own region. No port, userinfo, fragment, duplicate or percent-encoded parameter names.
- proof.headers: exactly x-elevator-payload-sha256, x-elevator-server-id (the REST API id) and
  x-elevator-nonce (32 lowercase hex chars), plus optionally host (must equal the URL host) and
  x-amz-security-token. Names are case-insensitive; values printable ASCII without surrounding
  whitespace. X-Amz-SignedHeaders must be exactly host plus every header sent.
- Caps: body 64 KiB, payload 16 KiB, url 8 KiB, header value 1 KiB (x-amz-security-token 4 KiB),
  STS response 16 KiB. Duplicate JSON keys are rejected at every level.
- Freshness: X-Amz-Date at most 60 s old, at most 30 s ahead, and inside X-Amz-Expires. STS itself
  accepts a presigned GetCallerIdentity for about 15 minutes whatever X-Amz-Expires says.

main.py maps UpgradeRequired and BadRequest to 400, ProofRejected to 403, ProofUnavailable to 503.
Proof material (URL, headers, signature, token) must never reach a log line or an exception message.
"""

import hashlib
import http.client
import json
import re
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from botocore.httpsession import where as ca_bundle_path

ENVELOPE_VERSION = 1

MAX_BODY_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_URL_LENGTH = 8 * 1024
MAX_HEADER_VALUE_LENGTH = 1024
MAX_SECURITY_TOKEN_LENGTH = 4 * 1024
MAX_STS_RESPONSE_BYTES = 16 * 1024

MAX_PROOF_AGE = timedelta(seconds=60)
MAX_CLOCK_SKEW = timedelta(seconds=30)
MAX_EXPIRES_SECONDS = 900

STS_CONNECT_TIMEOUT_SECONDS = 2
STS_READ_TIMEOUT_SECONDS = 3

PAYLOAD_HASH_HEADER = "x-elevator-payload-sha256"
SERVER_ID_HEADER = "x-elevator-server-id"
NONCE_HEADER = "x-elevator-nonce"
SECURITY_TOKEN_HEADER = "x-amz-security-token"
_BINDING_HEADERS = frozenset({PAYLOAD_HASH_HEADER, SERVER_ID_HEADER, NONCE_HEADER})
_OPTIONAL_HEADERS = frozenset({"host", SECURITY_TOKEN_HEADER})

_FIXED_QUERY = {"Action": "GetCallerIdentity", "Version": "2011-06-15"}
_SIGV4_QUERY = frozenset({"X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature"})
_REQUIRED_QUERY = _SIGV4_QUERY | _FIXED_QUERY.keys()
_ALLOWED_QUERY = _REQUIRED_QUERY | {"X-Amz-Security-Token"}

_URL_RE = re.compile(r"[A-Za-z0-9\-._~%&=/?:]+")
_HEADER_VALUE_RE = re.compile(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_AMZ_DATE_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_EXPIRES_RE = re.compile(r"[1-9][0-9]{0,2}")
_CREDENTIAL_RE = re.compile(r"[A-Z0-9]{16,128}/(?P<date>[0-9]{8})/(?P<region>[a-z0-9-]+)/sts/aws4_request")
_ACCOUNT_RE = re.compile(r"[0-9]{12}")
_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9.]{1,64}")

_STS_NS = "{https://sts.amazonaws.com/doc/2011-06-15/}"
_TRANSIENT_STS_CODES = frozenset({"Throttling", "ThrottlingException", "RequestLimitExceeded", "ServiceUnavailable", "InternalFailure"})
_HTTP_OK = 200
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500


class UpgradeRequired(Exception):
    """The body is not a version 1 envelope: an elevator CLI older than 5.0.0."""


class BadRequest(Exception):
    """The request is malformed in a way the caller can fix. str(e) is safe to return."""


class ProofRejected(Exception):
    """The proof does not establish an identity. str(e) is a log-safe reason, never shown to the caller."""


class ProofUnavailable(Exception):
    """STS could not answer (timeout, throttling, 5xx). str(e) is log-safe."""


@dataclass(frozen=True)
class Envelope:
    payload: dict
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class CallerIdentity:
    arn: str
    account: str


def sts_host(region: str) -> str:
    return f"sts.{region}.amazonaws.com"


def parse_envelope(body: object, *, expected_api_id: str, region: str, now: datetime | None = None) -> Envelope:
    """Every check that needs no network call, cheapest first."""
    if not expected_api_id or not region:
        raise ProofRejected("the CLI route is not configured")
    doc = _parse_body(body)
    payload, proof = doc["payload"], doc["proof"]
    if not isinstance(payload, str):
        raise ProofRejected("payload is not a string")
    payload_bytes = _utf8(payload)
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise BadRequest("Request is too large.")
    if not isinstance(proof, dict) or proof.keys() != {"url", "headers"}:
        raise ProofRejected("proof does not have exactly the keys url and headers")
    url, headers = proof["url"], proof["headers"]
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise ProofRejected("proof url is not a string within the size cap")
    headers = _normalize_headers(headers)

    if headers[SERVER_ID_HEADER] != expected_api_id:
        raise ProofRejected("proof audience is not this deployment's API id")
    if headers[PAYLOAD_HASH_HEADER] != hashlib.sha256(payload_bytes).hexdigest():
        raise ProofRejected("payload hash does not match the signed hash")
    if not _NONCE_RE.fullmatch(headers[NONCE_HEADER]):
        raise ProofRejected("nonce is not 32 lowercase hex characters")
    host = sts_host(region)
    if headers.get("host", host) != host:
        raise ProofRejected("host header does not match the STS host")
    query = _parse_url(url, host)
    if "X-Amz-Security-Token" in query and SECURITY_TOKEN_HEADER in headers:
        raise ProofRejected("security token sent both as a header and a query parameter")
    _check_signature_parameters(query, headers, region, now or datetime.now(UTC))
    return Envelope(payload=_parse_payload(payload), url=url, headers=headers)


def fetch_caller_identity(envelope: Envelope) -> CallerIdentity:
    """Forward the presigned request to STS unchanged; the identity STS returns is the caller."""
    parts = urlsplit(envelope.url)
    headers = {name: value for name, value in envelope.headers.items() if name != "host"}
    status, body = _sts_get(parts.netloc, f"{parts.path}?{parts.query}", headers)
    if status == _HTTP_OK:
        return _parse_identity(body)
    code = _error_code(body)
    if status == _HTTP_TOO_MANY_REQUESTS or status >= _HTTP_SERVER_ERROR or code in _TRANSIENT_STS_CODES:
        raise ProofUnavailable(f"STS answered {status} {code}")
    raise ProofRejected(f"STS refused the proof: {status} {code}")


def _sts_get(host: str, target: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """The HTTP boundary. Errors leave as ProofUnavailable carrying only the error's type name,
    raised outside the except block so no traceback or __context__ can carry the URL or headers."""
    failure = None
    connection = http.client.HTTPSConnection(
        host, 443, timeout=STS_CONNECT_TIMEOUT_SECONDS, context=ssl.create_default_context(cafile=ca_bundle_path())
    )
    try:
        connection.connect()
        connection.sock.settimeout(STS_READ_TIMEOUT_SECONDS)
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_STS_RESPONSE_BYTES + 1)
        status = response.status
    except (OSError, http.client.HTTPException, ValueError) as e:
        failure = type(e).__name__
    finally:
        connection.close()
    if failure:
        raise ProofUnavailable(f"STS request failed: {failure}")
    if len(body) > MAX_STS_RESPONSE_BYTES:
        raise ProofRejected("STS response exceeds the size cap")
    return status, body


def _parse_body(body: object) -> dict:
    if not isinstance(body, str):
        raise UpgradeRequired
    if len(body) > MAX_BODY_BYTES or len(body.encode("utf-8", "surrogatepass")) > MAX_BODY_BYTES:
        raise BadRequest("Request is too large.")
    try:
        doc = _strict_loads(body)
    except _DuplicateKey:
        raise ProofRejected("envelope repeats a JSON key") from None
    except ValueError, RecursionError:
        raise UpgradeRequired from None
    if not isinstance(doc, dict) or "version" not in doc or "proof" not in doc:
        raise UpgradeRequired
    version = doc["version"]
    if type(version) is not int or version != ENVELOPE_VERSION:
        raise UpgradeRequired
    if doc.keys() != {"version", "payload", "proof"}:
        raise ProofRejected("envelope does not have exactly the keys version, payload and proof")
    return doc


def _parse_payload(payload: str) -> dict:
    try:
        operation = _strict_loads(payload)
    except ValueError, RecursionError:
        raise BadRequest("payload must be valid JSON without duplicate keys.") from None
    if not isinstance(operation, dict):
        raise BadRequest("payload must be a JSON object.")
    return operation


class _DuplicateKey(ValueError):
    pass


def _strict_loads(text: str) -> object:
    """json.loads that rejects duplicate keys and NaN/Infinity (both raise ValueError)."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise _DuplicateKey("duplicate key")
        return result

    def no_constants(name: str) -> object:
        raise ValueError(f"{name} is not allowed")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=no_constants)


def _utf8(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise ProofRejected("payload is not encodable as UTF-8") from None


def _normalize_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise ProofRejected("proof headers is not an object")
    normalized = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower not in _BINDING_HEADERS | _OPTIONAL_HEADERS:
            raise ProofRejected("proof carries a header outside the allowlist")
        if lower in normalized:
            raise ProofRejected("proof carries a header twice")
        cap = MAX_SECURITY_TOKEN_LENGTH if lower == SECURITY_TOKEN_HEADER else MAX_HEADER_VALUE_LENGTH
        if not isinstance(value, str) or len(value) > cap or not _HEADER_VALUE_RE.fullmatch(value):
            raise ProofRejected("proof header value is not printable ASCII within the size cap")
        normalized[lower] = value
    if not _BINDING_HEADERS <= normalized.keys():
        raise ProofRejected("proof is missing a binding header")
    return normalized


def _parse_url(url: str, host: str) -> dict[str, str]:
    if not _URL_RE.fullmatch(url):
        raise ProofRejected("proof url contains a character outside the allowed set")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != host or parts.path != "/" or parts.fragment:
        raise ProofRejected("proof url is not https://<STS host>/")
    query = {}
    for pair in parts.query.split("&"):
        name, sep, raw_value = pair.partition("=")
        if not sep or "%" in name:
            raise ProofRejected("proof url has a malformed query parameter")
        if name in query:
            raise ProofRejected("proof url repeats a query parameter")
        try:
            query[name] = unquote(raw_value, errors="strict")
        except UnicodeDecodeError:
            raise ProofRejected("proof url has a malformed query parameter") from None
    if not _REQUIRED_QUERY <= query.keys() <= _ALLOWED_QUERY:
        raise ProofRejected("proof url does not have exactly the allowed query parameters")
    if any(query[name] != value for name, value in _FIXED_QUERY.items()):
        raise ProofRejected("proof url is not GetCallerIdentity version 2011-06-15")
    return query


def _check_signature_parameters(query: dict[str, str], headers: dict[str, str], region: str, now: datetime) -> None:
    if query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256":
        raise ProofRejected("proof is not signed with AWS4-HMAC-SHA256")
    credential = _CREDENTIAL_RE.fullmatch(query["X-Amz-Credential"])
    if not credential or credential["region"] != region:
        raise ProofRejected("proof credential scope is not sts in this region")
    amz_date = query["X-Amz-Date"]
    if not _AMZ_DATE_RE.fullmatch(amz_date) or credential["date"] != amz_date[:8]:
        raise ProofRejected("proof X-Amz-Date is malformed or disagrees with the credential scope")
    try:
        signed_at = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ProofRejected("proof X-Amz-Date is not a valid time") from None
    expires = query["X-Amz-Expires"]
    if not _EXPIRES_RE.fullmatch(expires) or int(expires) > MAX_EXPIRES_SECONDS:
        raise ProofRejected("proof X-Amz-Expires is out of range")
    age = now - signed_at
    if age > MAX_PROOF_AGE or age > timedelta(seconds=int(expires)) or -age > MAX_CLOCK_SKEW:
        raise ProofRejected("proof is outside the freshness window")
    if not _SHA256_HEX_RE.fullmatch(query["X-Amz-Signature"]):
        raise ProofRejected("proof signature is malformed")
    signed = query["X-Amz-SignedHeaders"].split(";")
    if signed != sorted(set(signed)) or set(signed) != {"host"} | headers.keys():
        raise ProofRejected("proof X-Amz-SignedHeaders is not exactly host plus the headers sent")


def _parse_identity(body: bytes) -> CallerIdentity:
    root = _safe_xml(body)
    if root is None or root.tag != f"{_STS_NS}GetCallerIdentityResponse":
        raise ProofRejected("STS response is not a GetCallerIdentityResponse")
    results = root.findall(f"{_STS_NS}GetCallerIdentityResult")
    if len(results) != 1:
        raise ProofRejected("STS response does not have exactly one result")
    arn, account = _single_text(results[0], "Arn"), _single_text(results[0], "Account")
    if not _ACCOUNT_RE.fullmatch(account):
        raise ProofRejected("STS returned a malformed account")
    arn_parts = arn.split(":", 5)
    if len(arn_parts) != 6 or arn_parts[:2] != ["arn", "aws"] or arn_parts[4] != account:  # noqa: PLR2004
        raise ProofRejected("STS returned an ARN that disagrees with its account")
    return CallerIdentity(arn=arn, account=account)


def _single_text(parent: ET.Element, name: str) -> str:
    found = parent.findall(f"{_STS_NS}{name}")
    if len(found) != 1 or not found[0].text:
        raise ProofRejected(f"STS response does not have exactly one {name}")
    return found[0].text


def _error_code(body: bytes) -> str:
    root = _safe_xml(body)
    code = root.find(f"{_STS_NS}Error/{_STS_NS}Code") if root is not None else None
    if code is None or not code.text or not _ERROR_CODE_RE.fullmatch(code.text):
        return "unknown"
    return code.text


def _safe_xml(body: bytes) -> ET.Element | None:
    # STS never sends a DOCTYPE, entity, comment or CDATA section; refusing "<!" rules out entity expansion.
    if b"<!" in body:
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None
