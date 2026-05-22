"""API Gateway event helpers shared by any HTTP-wrapped handler on Lambda."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


def normalize_api_gateway_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if v is not None and isinstance(v, str):
            out[k] = v
        elif v is not None and isinstance(v, (list, tuple)) and v:
            out[k] = v[0] if isinstance(v[0], str) else str(v[0])
    return out


def verify_client_cert_san(event: dict[str, Any], expected_san: str) -> tuple[bool, str]:  # noqa: PLR0911
    """Verify the mutual-TLS client certificate forwarded by API Gateway presents ``expected_san``.

    API Gateway (HTTP API, payload v2.0) exposes the client certificate under
    ``requestContext.authentication.clientCert`` (``subjectDN``, ``issuerDN``, ``clientCertPem``, ...).
    Because Slack's client cert chains to a public CA, the API Gateway truststore alone is not
    sufficient to prove identity — we must also confirm the SAN/CN. Prefers DNS SANs (RFC 6125),
    falls back to the subject Common Name. Returns ``(ok, reason)``.
    """
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    if not isinstance(request_context, Mapping):
        return False, "no client certificate presented"
    # HTTP API payload v2.0 exposes it at requestContext.authentication.clientCert;
    # REST / payload v1.0 at requestContext.identity.clientCert. Check both.
    client_cert: object = None
    authentication = request_context.get("authentication")
    if isinstance(authentication, Mapping):
        client_cert = authentication.get("clientCert")
    if not isinstance(client_cert, Mapping):
        identity = request_context.get("identity")
        if isinstance(identity, Mapping):
            client_cert = identity.get("clientCert")
    if not isinstance(client_cert, Mapping):
        return False, "no client certificate presented"

    expected = (expected_san or "").strip().lower()
    if not expected:
        return False, "no expected SAN configured"

    pem = client_cert.get("clientCertPem")
    if isinstance(pem, str) and pem.strip():
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID

            cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
            try:
                san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                if expected in {name.lower() for name in san_ext.value.get_values_for_type(x509.DNSName)}:
                    return True, ""
            except x509.ExtensionNotFound:
                pass
            if expected in {attr.value.lower() for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)}:
                return True, ""
            return False, "client certificate SAN/CN does not match expected value"
        except Exception:  # noqa: BLE001 — fall back to the API Gateway-provided subjectDN string
            pass

    subject_dn = str(client_cert.get("subjectDN") or "").lower()
    if subject_dn and f"cn={expected}" in subject_dn:
        return True, ""
    return False, "client certificate SAN/CN does not match expected value"


def parse_api_gateway_event_json_body(
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse JSON object body from a Lambda proxy event.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` with a full API Gateway–style
    response dict to return from the handler.
    """
    raw_body = event.get("body", "")
    if isinstance(raw_body, str) and event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
    if isinstance(raw_body, str):
        try:
            body: Any = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return None, {
                "statusCode": 400,
                "headers": {"Content-Type": "text/plain"},
                "body": "Invalid JSON body",
            }
    else:
        body = raw_body
    if not isinstance(body, dict):
        return None, {
            "statusCode": 400,
            "headers": {"Content-Type": "text/plain"},
            "body": "Expected object JSON body",
        }
    return body, None
