import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from requester.common.api_gateway import verify_client_cert_san

# ruff: noqa: ANN201, S101

EXPECTED = "platform-tls-client.slack.com"


def _make_cert_pem(common_name: str, san_dns: list[str] | None) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if san_dns:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in san_dns]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _event_with_pem(pem: str) -> dict:
    return {"requestContext": {"authentication": {"clientCert": {"clientCertPem": pem}}}}


def test_mtls_accepts_matching_san():
    pem = _make_cert_pem(common_name="not-the-name", san_dns=[EXPECTED])
    ok, _ = verify_client_cert_san(_event_with_pem(pem), EXPECTED)
    assert ok is True


def test_mtls_accepts_matching_cn_when_no_san():
    pem = _make_cert_pem(common_name=EXPECTED, san_dns=None)
    ok, _ = verify_client_cert_san(_event_with_pem(pem), EXPECTED)
    assert ok is True


def test_mtls_rejects_wrong_identity():
    pem = _make_cert_pem(common_name="evil.example.com", san_dns=["evil.example.com"])
    ok, reason = verify_client_cert_san(_event_with_pem(pem), EXPECTED)
    assert ok is False
    assert reason


def test_mtls_rejects_missing_cert():
    ok, reason = verify_client_cert_san({"requestContext": {}}, EXPECTED)
    assert ok is False
    assert "no client certificate" in reason


def test_mtls_subjectdn_fallback_when_no_pem():
    event = {"requestContext": {"authentication": {"clientCert": {"subjectDN": f"CN={EXPECTED},O=Slack"}}}}
    ok, _ = verify_client_cert_san(event, EXPECTED)
    assert ok is True


def test_mtls_accepts_v1_identity_client_cert_path():
    # REST / payload v1.0 places the client cert at requestContext.identity.clientCert.
    pem = _make_cert_pem(common_name="not-the-name", san_dns=[EXPECTED])
    event = {"requestContext": {"identity": {"clientCert": {"clientCertPem": pem}}}}
    ok, _ = verify_client_cert_san(event, EXPECTED)
    assert ok is True
