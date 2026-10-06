"""Local CA and broker certificate (SAN = mosquitto, localhost, 127.0.0.1).

Idempotent and self-renewing: if the server certificate is missing or has less than
RENEW_BEFORE_DAYS left, a new one is issued from the existing CA (clients keep trusting the CA).
Restart mosquitto after a renewal.
"""

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

OUT = Path(__file__).resolve().parents[1] / "infra" / "mosquitto" / "certs"
RENEW_BEFORE_DAYS = 30
CA_DAYS = 3650
SERVER_DAYS = 825


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _days_left(cert: x509.Certificate) -> float:
    return (cert.not_valid_after_utc - datetime.now(UTC)).total_seconds() / 86400


def _load_cert(path: Path) -> x509.Certificate | None:
    return x509.load_pem_x509_certificate(path.read_bytes()) if path.exists() else None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)

    ca_cert = _load_cert(OUT / "ca.crt")
    ca_key_path = OUT / "ca.key"
    if ca_cert is None or not ca_key_path.exists() or _days_left(ca_cert) < RENEW_BEFORE_DAYS:
        ca_key = _key()
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hastori Demo CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        (OUT / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        _write_key(ca_key_path, ca_key)
        (OUT / "server.crt").unlink(missing_ok=True)  # signed by the old CA
        print("issued new CA")
    signing_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    assert isinstance(signing_key, rsa.RSAPrivateKey)

    server = _load_cert(OUT / "server.crt")
    if server is not None and _days_left(server) >= RENEW_BEFORE_DAYS:
        print(f"server certificate valid for {_days_left(server):.0f} more days, keeping it")
        return

    srv_key = _key()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mosquitto")]))
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SERVER_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("mosquitto"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(signing_key, hashes.SHA256())
    )
    (OUT / "server.crt").write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    _write_key(OUT / "server.key", srv_key)
    print(f"issued server certificate, valid {SERVER_DAYS} days (restart mosquitto to load it)")


if __name__ == "__main__":
    main()
