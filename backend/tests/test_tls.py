"""Test del supporto TLS: settings e script di generazione del certificato."""

import os
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import Settings
from scripts import gen_cert


def test_settings_tls_default_http() -> None:
    """Senza configurazione TLS il server resta in HTTP (campi vuoti)."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.tls_cert_file == ""
    assert s.tls_key_file == ""


def test_settings_tls_da_env(monkeypatch) -> None:
    monkeypatch.setenv("TLS_CERT_FILE", "/data/certs/virtualis.crt")
    monkeypatch.setenv("TLS_KEY_FILE", "/data/certs/virtualis.key")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.tls_cert_file == "/data/certs/virtualis.crt"
    assert s.tls_key_file == "/data/certs/virtualis.key"


def test_gen_cert_certificato_valido(tmp_path) -> None:
    """Il certificato generato è self-signed, valido, con le SAN richieste;
    la chiave privata ha permessi 600 e corrisponde al certificato."""
    cert_path, key_path = gen_cert.generate_self_signed_cert(
        ["workstation.example.lan", "virtualis"],
        ["192.168.1.50", "127.0.0.1"],
        tmp_path,
    )
    assert cert_path.is_file() and key_path.is_file()
    assert os.stat(key_path).st_mode & 0o777 == 0o600

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

    # SAN: hostname e IP richiesti presenti
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addrs = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "workstation.example.lan" in dns_names
    assert "virtualis" in dns_names
    assert "192.168.1.50" in ip_addrs
    assert "127.0.0.1" in ip_addrs

    # Self-signed: subject == issuer, firma verificabile con la propria chiave
    assert cert.subject == cert.issuer
    cert.public_key().verify(
        cert.signature,
        cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        cert.signature_hash_algorithm,
    )

    # La chiave privata corrisponde alla pubblica del certificato
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Validità: già valido e per oltre 2 anni (default 825 giorni)
    now = datetime.now(UTC)
    assert cert.not_valid_before_utc <= now
    assert cert.not_valid_after_utc > now + timedelta(days=800)

    # Uso previsto: server TLS, non CA
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False


def test_gen_cert_main_end_to_end(tmp_path) -> None:
    """Il CLI rileva hostname/IP della workstation e include le SAN extra."""
    rc = gen_cert.main(["--out", str(tmp_path), "10.20.30.40", "nas-ufficio"])
    assert rc == 0
    cert = x509.load_pem_x509_certificate((tmp_path / "virtualis.crt").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addrs = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "nas-ufficio" in dns_names
    assert "127.0.0.1" in ip_addrs
    assert "10.20.30.40" in ip_addrs
