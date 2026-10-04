import hashlib
import hmac
import json
import socket
import ssl
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import quote, unquote, urlsplit

import pytest

import cli_proof

from .proof_helpers import (
    API_ID,
    NONCE,
    REGION,
    SESSION_TOKEN,
    SIGNATURE,
    SIGNED_AT,
    default_payload,
    load_golden,
    make_body,
    proof_query,
    proof_url,
    sts_error_xml,
    sts_identity_xml,
)

ARN = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Foo_abc/req@example.com"


def parse(body: object, *, now: datetime = SIGNED_AT, api_id: str = API_ID, region: str = REGION) -> cli_proof.Envelope:
    return cli_proof.parse_envelope(body, expected_api_id=api_id, region=region, now=now)


def rejected(body: object, **kwargs) -> None:
    with pytest.raises(cli_proof.ProofRejected):
        parse(body, **kwargs)


def with_query(**changes: str | None) -> str:
    query = proof_query()
    for key, value in changes.items():
        name = key.replace("_", "-")
        if value is None:
            query.pop(name)
        else:
            query[name] = value
    return make_body(query=query)


# ---------------------------------------------------------------------------
# Golden vector shared with cmd/elevator/proof_test.go
# ---------------------------------------------------------------------------


def _golden_now(golden: dict, seconds: int) -> datetime:
    return datetime.fromisoformat(golden["signing_time"]) + timedelta(seconds=seconds)


def test_golden_envelope_from_the_go_cli_passes_every_local_check():
    golden = load_golden()
    envelope = parse(golden["body"], now=_golden_now(golden, 10), api_id=golden["api_id"], region=golden["region"])

    assert envelope.payload == json.loads(golden["payload"])
    assert envelope.payload["reason"] == 'incident <42> & "quotes" ü'
    assert envelope.headers[cli_proof.PAYLOAD_HASH_HEADER] == golden["payload_sha256"]
    assert hashlib.sha256(golden["payload"].encode()).hexdigest() == golden["payload_sha256"]


@pytest.mark.parametrize(("seconds", "accepted"), [(-30, True), (-31, False), (60, True), (61, False)])
def test_golden_envelope_freshness_window(seconds, accepted):
    golden = load_golden()
    kwargs = {"now": _golden_now(golden, seconds), "api_id": golden["api_id"], "region": golden["region"]}
    if accepted:
        parse(golden["body"], **kwargs)
    else:
        rejected(golden["body"], **kwargs)


def _sigv4_query_signature(url: str, headers: dict[str, str], secret_key: str) -> str:
    """Recomputes a presigned SigV4 signature from scratch, as STS does."""
    parts = urlsplit(url)
    params = dict(pair.split("=", 1) for pair in parts.query.split("&"))
    params = {k: unquote(v) for k, v in params.items()}
    signature = params.pop("X-Amz-Signature")
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    all_headers = {"host": parts.netloc} | headers
    signed = params["X-Amz-SignedHeaders"]
    canonical_headers = "".join(f"{name}:{all_headers[name]}\n" for name in signed.split(";"))
    canonical_request = "\n".join(["GET", "/", canonical_query, canonical_headers, signed, hashlib.sha256(b"").hexdigest()])
    _akid, date, region, service, terminator = params["X-Amz-Credential"].split("/")
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            params["X-Amz-Date"],
            f"{date}/{region}/{service}/{terminator}",
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{secret_key}".encode()
    for part in (date, region, service, terminator):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    assert hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest() == signature
    return signature


def test_golden_envelope_signature_covers_the_binding_headers():
    """An independent SigV4 recomputation proves the Go CLI signs the x-elevator headers, so STS
    rejects any change to them."""
    golden = load_golden()
    proof = json.loads(golden["body"])["proof"]
    _sigv4_query_signature(proof["url"], proof["headers"], golden["secret_access_key"])

    tampered = proof["headers"] | {cli_proof.SERVER_ID_HEADER: "zzzzz99999"}
    with pytest.raises(AssertionError):
        _sigv4_query_signature(proof["url"], tampered, golden["secret_access_key"])


# ---------------------------------------------------------------------------
# parse_envelope: success and the 400 cases
# ---------------------------------------------------------------------------


def test_valid_envelope_parses():
    envelope = parse(make_body())
    assert envelope.payload == default_payload()
    assert envelope.headers[cli_proof.NONCE_HEADER] == NONCE


def test_envelope_repr_hides_the_proof():
    text = repr(parse(make_body()))
    assert SIGNATURE not in text
    assert SESSION_TOKEN not in text


@pytest.mark.parametrize(
    "body",
    [
        None,
        42,
        "",
        "not json",
        "[]",
        json.dumps(default_payload()),
        json.dumps({"version": 1, "payload": "{}"}),
        json.dumps({"version": 2, "payload": "{}", "proof": {}}),
        json.dumps({"version": "1", "payload": "{}", "proof": {}}),
        json.dumps({"version": True, "payload": "{}", "proof": {}}),
        pytest.param("[" * 60_000, id="deeply-nested"),
    ],
)
def test_bodies_from_old_clients_need_an_upgrade(body):
    with pytest.raises(cli_proof.UpgradeRequired):
        parse(body)


def test_body_over_the_cap_is_too_large():
    with pytest.raises(cli_proof.BadRequest):
        parse(make_body(default_payload() | {"reason": "x" * cli_proof.MAX_BODY_BYTES}))


def test_multibyte_body_over_the_cap_in_bytes_is_too_large():
    body = make_body(json.dumps(default_payload() | {"reason": "ü" * 40_000}, ensure_ascii=False))
    assert len(body) <= cli_proof.MAX_BODY_BYTES
    with pytest.raises(cli_proof.BadRequest):
        parse(body)


def test_payload_over_the_cap_is_too_large():
    with pytest.raises(cli_proof.BadRequest):
        parse(make_body(default_payload() | {"reason": "x" * cli_proof.MAX_PAYLOAD_BYTES}))


@pytest.mark.parametrize("payload", ["not json", "[1]", '{"account": "1", "account": "2"}', '{"a": NaN}'])
def test_malformed_payload_after_a_matching_hash_is_a_bad_request(payload):
    with pytest.raises(cli_proof.BadRequest):
        parse(make_body(payload))


# ---------------------------------------------------------------------------
# parse_envelope: the 403 cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("api_id", "region"), [("", REGION), (API_ID, "")])
def test_unconfigured_cli_rejects_everything(api_id, region):
    rejected(make_body(), api_id=api_id, region=region)


def test_envelope_with_an_extra_key_is_rejected():
    doc = json.loads(make_body())
    doc["extra"] = 1
    rejected(json.dumps(doc))


def test_duplicate_envelope_key_is_rejected():
    body = make_body()
    rejected(body[:-1] + ', "version": 1}')


def test_duplicate_proof_header_key_is_rejected():
    body = make_body()
    rejected(body.replace('"x-elevator-nonce"', f'"x-elevator-nonce": "{NONCE}", "x-elevator-nonce"'))


@pytest.mark.parametrize(
    "proof",
    [
        "x",
        {"url": "x"},
        {"url": "x", "headers": {}, "extra": 1},
        {"url": 1, "headers": {}},
        {"url": "x", "headers": []},
    ],
)
def test_malformed_proof_is_rejected(proof):
    rejected(json.dumps({"version": 1, "payload": json.dumps(default_payload()), "proof": proof}))


def test_non_string_payload_is_rejected():
    doc = json.loads(make_body())
    doc["payload"] = default_payload()
    rejected(json.dumps(doc))


def test_payload_with_a_lone_surrogate_is_rejected():
    rejected(make_body('{"reason": "\ud800"}'))


def test_url_over_the_cap_is_rejected():
    rejected(make_body(url=proof_url(proof_query()) + "a" * cli_proof.MAX_URL_LENGTH))


def test_wrong_audience_is_rejected():
    rejected(make_body(api_id="zzzzz99999"))


def test_payload_hash_mismatch_is_rejected():
    body = json.loads(make_body())
    body["payload"] = json.dumps(default_payload() | {"account": "222222222222"})
    rejected(json.dumps(body))


@pytest.mark.parametrize("nonce", ["", "abc", NONCE.upper(), NONCE + "00", "g" * 32])
def test_malformed_nonce_is_rejected(nonce):
    rejected(make_body(nonce=nonce))


@pytest.mark.parametrize("missing", [cli_proof.PAYLOAD_HASH_HEADER, cli_proof.SERVER_ID_HEADER, cli_proof.NONCE_HEADER])
def test_missing_binding_header_is_rejected(missing):
    headers = json.loads(make_body())["proof"]["headers"]
    del headers[missing]
    rejected(make_body(headers=headers))


@pytest.mark.parametrize(
    "extra",
    [
        {"authorization": "x"},
        {"x-amz-date": "20260115T120000Z"},
        {"x-forwarded-for": "1.2.3.4"},
        {"X-Elevator-Nonce": NONCE},
        {"host": "evil.example.com"},
        {"host": "sts.us-east-1.amazonaws.com"},
    ],
)
def test_header_outside_the_allowlist_or_conflicting_is_rejected(extra):
    rejected(make_body(extra_headers=extra))


@pytest.mark.parametrize(
    "value",
    [NONCE + "\r\nx-injected: 1", NONCE + "\n", " " + NONCE, NONCE + " ", "", "ü" * 32, 7, None],
)
def test_header_value_that_is_not_clean_printable_ascii_is_rejected(value):
    rejected(make_body(extra_headers={cli_proof.NONCE_HEADER: value}))


def test_header_value_over_the_cap_is_rejected():
    rejected(make_body(extra_headers={"host": "a" * (cli_proof.MAX_HEADER_VALUE_LENGTH + 1)}))


def test_host_header_matching_the_url_is_accepted():
    signed = "host;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id"
    parse(make_body(query=proof_query(signed_headers=signed), extra_headers={"Host": f"sts.{REGION}.amazonaws.com"}))


def test_security_token_header_is_accepted_when_signed_and_not_also_in_the_query():
    query = proof_query(signed_headers="host;x-amz-security-token;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id")
    del query["X-Amz-Security-Token"]
    parse(make_body(query=query, extra_headers={"x-amz-security-token": SESSION_TOKEN}))


def test_security_token_header_over_its_cap_is_rejected():
    query = proof_query(signed_headers="host;x-amz-security-token;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id")
    del query["X-Amz-Security-Token"]
    rejected(make_body(query=query, extra_headers={"x-amz-security-token": "a" * (cli_proof.MAX_SECURITY_TOKEN_LENGTH + 1)}))


def test_security_token_in_both_header_and_query_is_rejected():
    query = proof_query(signed_headers="host;x-amz-security-token;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id")
    rejected(make_body(query=query, extra_headers={"x-amz-security-token": SESSION_TOKEN}))


def test_unsigned_security_token_header_is_rejected():
    query = proof_query()
    del query["X-Amz-Security-Token"]
    rejected(make_body(query=query, extra_headers={"x-amz-security-token": SESSION_TOKEN}))


@pytest.mark.parametrize(
    "url",
    [
        proof_url(proof_query()).replace("https://", "http://"),
        proof_url(proof_query(), host="sts.amazonaws.com"),
        proof_url(proof_query(), host="sts.us-east-1.amazonaws.com"),
        proof_url(proof_query(), host="sts.eu-central-1.amazonaws.com.evil.example"),
        proof_url(proof_query(), host="sts.eu-central-1.amazonaws.com:443"),
        proof_url(proof_query(), host="user@sts.eu-central-1.amazonaws.com"),
        proof_url(proof_query()).replace(".com/?", ".com/x?"),
        proof_url(proof_query()).replace(".com/?", ".com?"),
        proof_url(proof_query()) + "#frag",
        proof_url(proof_query()) + "&Action=GetCallerIdentity",
        proof_url(proof_query()) + "&Extra=1",
        proof_url(proof_query()) + "&",
        proof_url(proof_query()) + "&Flag",
        proof_url(proof_query()).replace("Action=", "Act%69on="),
        proof_url(proof_query()).replace("X-Amz-Date=", "X-Amz-Date=%FF"),
        proof_url(proof_query()) + " ",
        f"https://sts.{REGION}.amazonaws.com/",
    ],
)
def test_url_that_is_not_exactly_a_presigned_sts_request_is_rejected(url):
    rejected(make_body(url=url))


@pytest.mark.parametrize(
    "changes",
    [
        {"Action": "AssumeRole"},
        {"Version": "2011-06-16"},
        {"X_Amz_Algorithm": "AWS4-HMAC-SHA1"},
        {"X_Amz_Algorithm": "AWS4-ECDSA-P256-SHA256"},
        {"X_Amz_Credential": "ASIAIOSFODNN7EXAMPLE/20260115/us-east-1/sts/aws4_request"},
        {"X_Amz_Credential": f"ASIAIOSFODNN7EXAMPLE/20260115/{REGION}/iam/aws4_request"},
        {"X_Amz_Credential": f"ASIAIOSFODNN7EXAMPLE/20260114/{REGION}/sts/aws4_request"},
        {"X_Amz_Credential": f"short/20260115/{REGION}/sts/aws4_request"},
        {"X_Amz_Date": "2026-01-15T12:00:00Z"},
        {"X_Amz_Date": "20260115T250000Z"},
        {"X_Amz_Expires": "0"},
        {"X_Amz_Expires": "901"},
        {"X_Amz_Expires": "060"},
        {"X_Amz_Expires": "x"},
        {"X_Amz_Signature": "abc"},
        {"X_Amz_Signature": "A" * 64},
        {"X_Amz_SignedHeaders": "host;x-elevator-payload-sha256;x-elevator-server-id"},
        {"X_Amz_SignedHeaders": "x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id"},
        {"X_Amz_SignedHeaders": "host;user-agent;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id"},
        {"X_Amz_SignedHeaders": "x-elevator-nonce;host;x-elevator-payload-sha256;x-elevator-server-id"},
        {"X_Amz_SignedHeaders": "host;host;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id"},
        {"X_Amz_Signature": None},
        {"X_Amz_Expires": None},
        {"X_Amz_Credential": None},
    ],
)
def test_query_parameter_outside_the_contract_is_rejected(changes):
    rejected(with_query(**changes))


def test_proof_without_a_session_token_is_accepted():
    parse(with_query(X_Amz_Security_Token=None))


def test_proof_older_than_its_own_expiry_is_rejected():
    body = with_query(X_Amz_Expires="10")
    parse(body, now=SIGNED_AT + timedelta(seconds=10))
    rejected(body, now=SIGNED_AT + timedelta(seconds=11))


@pytest.mark.parametrize(("seconds", "accepted"), [(-30, True), (-31, False), (60, True), (61, False)])
def test_freshness_window(seconds, accepted):
    now = SIGNED_AT + timedelta(seconds=seconds)
    if accepted:
        parse(make_body(), now=now)
    else:
        rejected(make_body(), now=now)


# ---------------------------------------------------------------------------
# fetch_caller_identity
# ---------------------------------------------------------------------------


@pytest.fixture
def connection():
    with patch.object(cli_proof.http.client, "HTTPSConnection") as connection_cls:
        conn = connection_cls.return_value
        conn.sock = MagicMock()
        yield connection_cls


def respond(connection_cls, status: int, body: bytes) -> None:
    response = connection_cls.return_value.getresponse.return_value
    response.status = status
    response.read.side_effect = lambda amt: body[:amt]


def test_fetch_forwards_the_proof_unchanged_and_returns_the_sts_identity(connection):
    respond(connection, 200, sts_identity_xml(ARN))
    envelope = parse(make_body(extra_headers={"host": f"sts.{REGION}.amazonaws.com"}, query=proof_query()))

    assert cli_proof.fetch_caller_identity(envelope) == cli_proof.CallerIdentity(arn=ARN, account="111111111111")

    host, port = connection.call_args.args
    assert (host, port) == (f"sts.{REGION}.amazonaws.com", 443)
    assert connection.call_args.kwargs["timeout"] == cli_proof.STS_CONNECT_TIMEOUT_SECONDS
    assert connection.call_args.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    conn = connection.return_value
    conn.sock.settimeout.assert_called_once_with(cli_proof.STS_READ_TIMEOUT_SECONDS)
    method, target = conn.request.call_args.args
    assert method == "GET"
    assert target == "/?" + urlsplit(envelope.url).query
    assert conn.request.call_args.kwargs["headers"] == {k: v for k, v in envelope.headers.items() if k != "host"}
    conn.close.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        b"not xml",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
        sts_identity_xml(ARN).replace(b"GetCallerIdentityResponse", b"AssumeRoleResponse"),
        sts_identity_xml(ARN).replace(b'xmlns="https://sts.amazonaws.com/doc/2011-06-15/"', b""),
        sts_identity_xml(ARN, account="222222222222"),
        sts_identity_xml(ARN, account="1111"),
        sts_identity_xml(ARN.replace("arn:aws:", "arn:aws-cn:")),
        sts_identity_xml(ARN).replace(b"<Account>111111111111</Account>", b""),
        sts_identity_xml(ARN).replace(b"<Arn>", b"<Arn>x</Arn><Arn>"),
        sts_identity_xml(ARN).replace(b"</GetCallerIdentityResult>", b"</GetCallerIdentityResult><GetCallerIdentityResult/>"),
        sts_identity_xml(ARN) + b" " * cli_proof.MAX_STS_RESPONSE_BYTES,
    ],
)
def test_unexpected_sts_success_body_is_rejected(connection, body):
    respond(connection, 200, body)
    with pytest.raises(cli_proof.ProofRejected):
        cli_proof.fetch_caller_identity(parse(make_body()))


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, sts_error_xml("SignatureDoesNotMatch"), cli_proof.ProofRejected),
        (403, sts_error_xml("ExpiredToken"), cli_proof.ProofRejected),
        (400, b"garbage", cli_proof.ProofRejected),
        (302, b"", cli_proof.ProofRejected),
        (400, sts_error_xml("Throttling"), cli_proof.ProofUnavailable),
        (429, b"", cli_proof.ProofUnavailable),
        (500, sts_error_xml("InternalFailure"), cli_proof.ProofUnavailable),
        (503, b"", cli_proof.ProofUnavailable),
        (403, b"x" * (cli_proof.MAX_STS_RESPONSE_BYTES + 1), cli_proof.ProofRejected),
    ],
)
def test_sts_error_status_mapping(connection, status, body, error):
    respond(connection, status, body)
    with pytest.raises(error) as excinfo:
        cli_proof.fetch_caller_identity(parse(make_body()))
    assert SESSION_TOKEN not in str(excinfo.value)


def test_oversized_sts_5xx_is_still_unavailable(connection):
    respond(connection, 503, b"x" * (cli_proof.MAX_STS_RESPONSE_BYTES * 2))
    with pytest.raises(cli_proof.ProofUnavailable):
        cli_proof.fetch_caller_identity(parse(make_body()))
    connection.return_value.getresponse.return_value.read.assert_called_once_with(cli_proof.MAX_STS_RESPONSE_BYTES + 1)


@pytest.mark.parametrize(
    "failure",
    [
        socket.timeout(f"timed out reading {proof_url(proof_query())}"),
        ConnectionRefusedError(f"refused {SIGNATURE}"),
        ssl.SSLCertVerificationError(f"bad cert for {SESSION_TOKEN}"),
        cli_proof.http.client.RemoteDisconnected(f"gone {SIGNATURE}"),
    ],
)
def test_transport_failure_is_unavailable_and_carries_no_proof(connection, failure):
    connection.return_value.getresponse.side_effect = failure
    with pytest.raises(cli_proof.ProofUnavailable) as excinfo:
        cli_proof.fetch_caller_identity(parse(make_body()))

    error = excinfo.value
    assert error.__cause__ is None
    assert error.__context__ is None
    for marker in (SIGNATURE, SESSION_TOKEN, "X-Amz-Signature"):
        assert marker not in str(error)
    connection.return_value.close.assert_called_once()
