"""Entry point ``python -m app``: avvia uvicorn, con TLS se configurato.

Se ``TLS_CERT_FILE`` e ``TLS_KEY_FILE`` sono entrambi impostati (certificato
generabile con ``scripts/gen_cert.py``), il server è avviato in HTTPS;
altrimenti in HTTP semplice. Host e porta sono configurabili con le variabili
d'ambiente ``HOST`` e ``PORT`` (default ``0.0.0.0:8000``).
"""

import os

import uvicorn

from .config import get_settings


def main() -> None:
    """Avvia il server HTTP/HTTPS secondo le impostazioni."""
    settings = get_settings()
    ssl_kwargs: dict[str, str] = {}
    if settings.tls_cert_file and settings.tls_key_file:
        ssl_kwargs = {
            "ssl_certfile": settings.tls_cert_file,
            "ssl_keyfile": settings.tls_key_file,
        }
    elif settings.tls_cert_file or settings.tls_key_file:
        raise SystemExit(
            "TLS_CERT_FILE e TLS_KEY_FILE vanno impostati entrambi (o nessuno dei due)."
        )
    uvicorn.run(
        "app.api.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        **ssl_kwargs,  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    main()
