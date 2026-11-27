#!/usr/bin/env python3
"""Reindicizzazione dell'indice vettoriale: client CLI della route admin.

Il client Qdrant local-mode NON tollera un secondo processo sullo stesso
path: questo script quindi non apre alcun client proprio, ma pilota l'app
viva via HTTP(S) — ``POST /api/admin/reindex`` per l'avvio e polling su
``GET /api/admin/reindex/status`` fino al termine. Pensato per girare nel
container (``python scripts/reindex.py``) o da cron/nohup sull'host.

Il job nell'app rilavora ogni documento ``indexed`` ri-embeddando i chunk
dal DB col formato corrente (titolo + testo) ed è riprendibile: se lo script
o l'app cadono, una nuova esecuzione riparte dall'ultimo documento
completato (stato in ``/data/reindex_state.json``; ``--restart`` lo ignora).

Uso:
    python scripts/reindex.py [--url https://localhost:8000]
        [--username admin] --password ... [--dry-run] [--restart]
        [--poll-interval 10]

Il certificato TLS dell'app è self-signed: la verifica è disattivata.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import httpx

# Stati terminali del job (oltre i quali il polling si ferma)
_TERMINAL = {"done", "failed", "cancelled"}


def _login(client: httpx.Client, username: str, password: str) -> str:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def _print_progress(status: dict[str, Any]) -> None:
    """Una riga di progresso per ogni poll (leggibile anche nei log nohup)."""
    pause = " [in pausa: ingest attivo]" if status.get("paused_for_ingest") else ""
    print(
        f"{status['percent']:5.1f}%  "
        f"documenti {status['done_documents']}/"
        f"{status['total_documents'] - status['skipped_documents']}  "
        f"chunk {status['done_chunks']}/{status['total_chunks']}  "
        f"{status['rate_chunks_per_s']:.1f} chunk/s  "
        f"ETA {_fmt_eta(status['eta_seconds'])}{pause}",
        flush=True,
    )


def _print_summary(status: dict[str, Any]) -> None:
    print("\n=== Riepilogo reindicizzazione ===")
    for key in (
        "state",
        "dry_run",
        "total_documents",
        "skipped_documents",
        "done_documents",
        "failed_documents",
        "total_chunks",
        "done_chunks",
        "rate_chunks_per_s",
        "started_at",
        "finished_at",
        "error",
    ):
        print(f"  {key}: {status.get(key)}")


def run(args: argparse.Namespace) -> int:
    with httpx.Client(base_url=args.url, timeout=30.0, verify=False) as client:
        token = _login(client, args.username, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            "/api/admin/reindex",
            json={"confirm": True, "dry_run": args.dry_run, "restart": args.restart},
            headers=headers,
        )
        if resp.status_code == 409:
            print("Una reindicizzazione è già in corso: mi metto in polling.")
        else:
            resp.raise_for_status()
            print(
                "Reindicizzazione avviata"
                + (" (dry-run: solo conteggio)" if args.dry_run else "")
            )

        # Polling fino a uno stato terminale; il token JWT dura ore, ben
        # oltre qualunque reindex ragionevole tra un login e l'altro
        while True:
            status = client.get("/api/admin/reindex/status", headers=headers)
            status.raise_for_status()
            snap = status.json()
            if snap["state"] in _TERMINAL:
                _print_summary(snap)
                return 0 if snap["state"] == "done" else 1
            _print_progress(snap)
            time.sleep(args.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        default="https://localhost:8000",
        help="Base URL dell'app (default: https://localhost:8000)",
    )
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Conta solo documenti e chunk da rilavorare, senza reindicizzare",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignora lo stato di ripresa salvato e riparte dal primo documento",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Secondi tra un controllo dello stato e l'altro (default: 10)",
    )
    args = parser.parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        # Il job nell'app prosegue comunque: lo script è solo un osservatore
        print("\nInterrotto: il job continua nell'app (ripeti lo script per seguirlo).")
        sys.exit(130)


if __name__ == "__main__":
    main()
