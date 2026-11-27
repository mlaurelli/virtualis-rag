#!/usr/bin/env python3
"""Genera un certificato TLS self-signed per l'uso su LAN aziendale.

Uso::

    uv run python scripts/gen_cert.py [--out DIR] [--days N] [SAN_EXTRA ...]

- le SAN includono automaticamente hostname della workstation, ``localhost``,
  ``127.0.0.1`` e l'IP LAN rilevato; si possono aggiungere hostname/IP extra
  come argomenti posizionali (es. l'IP statico assegnato dal DHCP aziendale);
- i file ``virtualis.crt`` e ``virtualis.key`` (permessi 600) sono scritti in
  ``--out`` (default: ``<repo>/data/certs``);
- per attivare HTTPS impostare in ``.env``::

      TLS_CERT_FILE=<out>/virtualis.crt
      TLS_KEY_FILE=<out>/virtualis.key

  e avviare il backend con ``uv run python -m app`` (o Docker Compose).
Trattandosi di un certificato self-signed, i client della LAN dovranno
accettare l'eccezione di sicurezza del browser (o installare il certificato
nelle proprie CA fidate).
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Radice del repository: backend/scripts/gen_cert.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# Validità massima riconosciuta dai browser per i certificati server
DEFAULT_VALIDITY_DAYS = 825

CERT_FILENAME = "virtualis.crt"
KEY_FILENAME = "virtualis.key"


def _detect_lan_ip() -> str | None:
    """IP LAN della workstation (socket UDP verso un indirizzo TEST-NET:
    nessun pacchetto è realmente inviato, serve solo a scegliere l'interfaccia)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def generate_self_signed_cert(
    hostnames: list[str],
    ips: list[str],
    out_dir: Path | str,
    *,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
) -> tuple[Path, Path]:
    """Genera certificato e chiave RSA self-signed; ritorna i due path.

    ``hostnames`` e ``ips`` finiscono nella Subject Alternative Name (SAN),
    deduplicati mantenendo l'ordine. La chiave è scritta con permessi 600.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    common_name = hostnames[0] if hostnames else "virtualis.local"
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Virtualis"),
        ]
    )
    san_entries: list[x509.GeneralName] = [
        x509.DNSName(host) for host in dict.fromkeys(hostnames)
    ] + [
        x509.IPAddress(ipaddress.ip_address(ip)) for ip in dict.fromkeys(ips)
    ]
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = out / CERT_FILENAME
    key_path = out / KEY_FILENAME
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


def main(argv: list[str] | None = None) -> int:
    """CLI: rileva le SAN della workstation e genera il certificato."""
    parser = argparse.ArgumentParser(
        description="Genera un certificato TLS self-signed per Virtualis (LAN)."
    )
    parser.add_argument(
        "extra_san",
        nargs="*",
        help="hostname o IP aggiuntivi da includere nella SAN "
        "(es. l'IP statico della workstation)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"cartella di destinazione (default: {REPO_ROOT / 'data' / 'certs'})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_VALIDITY_DAYS,
        help=f"giorni di validità (default: {DEFAULT_VALIDITY_DAYS})",
    )
    args = parser.parse_args(argv)

    hostnames = [socket.gethostname(), "localhost"]
    ips = ["127.0.0.1"]
    lan_ip = _detect_lan_ip()
    if lan_ip:
        ips.append(lan_ip)
    for item in args.extra_san:
        try:
            ips.append(str(ipaddress.ip_address(item)))
        except ValueError:
            hostnames.append(item)

    out_dir = Path(args.out) if args.out else REPO_ROOT / "data" / "certs"
    cert_path, key_path = generate_self_signed_cert(
        hostnames, ips, out_dir, validity_days=args.days
    )
    print(f"Certificato: {cert_path}")
    print(f"Chiave:      {key_path} (permessi 600)")
    print(f"SAN:         {', '.join(dict.fromkeys(hostnames + ips))}")
    print(
        "\nPer attivare HTTPS aggiungere a .env:\n"
        f"  TLS_CERT_FILE={cert_path}\n"
        f"  TLS_KEY_FILE={key_path}\n"
        "e avviare il backend con: uv run python -m app"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
