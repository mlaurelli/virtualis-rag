#!/usr/bin/env python3
"""Test di carico per il retrieval del sistema Virtualis.

Misura le latenze della ricerca ibrida (e degli endpoint di consultazione)
sotto concorrenza, contro un backend in esecuzione. Non richiede il LLM:
gli endpoint di ricerca e documenti lavorano interamente in locale.

Uso:
    uv run python scripts/load_test.py [--host http://localhost:8000]
        [--users 8] [--requests 50] [--username admin] [--password ...]

Produce un report a console (p50/p95/p99/max, errori, throughput) e
salva ``load_test_report.json`` nella directory corrente.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

QUERIES = [
    "quanto dura la garanzia sugli interventi di riparazione?",
    "prezzo del sensore SP-220",
    "delibera del consiglio di amministrazione",
    "procedure di sicurezza per il fermo impianto",
    "rimborso spese di trasferta",
    "filtro olio idraulico FLT-100",
    "SLA e penali del contratto di manutenzione",
    "canone annuo di manutenzione",
    "bilancio di sostenibilità emissioni",
    "piano di manutenzione programmata",
]


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _worker(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    n_requests: int,
    latencies: list[float],
    errors: list[str],
) -> None:
    for i in range(n_requests):
        query = QUERIES[i % len(QUERIES)]
        start = time.perf_counter()
        try:
            resp = await client.post(
                "/api/search",
                json={"query": query, "limit": 10},
                headers=headers,
            )
            resp.raise_for_status()
            if not resp.json().get("results"):
                errors.append(f"nessun risultato per {query!r}")
        except Exception as exc:  # il report raccoglie tutti gli errori
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            latencies.append(time.perf_counter() - start)


async def run_load_test(
    host: str, users: int, requests: int, username: str, password: str
) -> dict:
    async with httpx.AsyncClient(base_url=host, timeout=60.0) as client:
        token = await _login(client, username, password)
        headers = {"Authorization": f"Bearer {token}"}
        latencies: list[float] = []
        errors: list[str] = []
        start = time.perf_counter()
        await asyncio.gather(
            *(
                _worker(client, headers, requests, latencies, errors)
                for _ in range(users)
            )
        )
        elapsed = time.perf_counter() - start

    total = users * requests
    ordered = sorted(latencies)

    def pct(p: float) -> float:
        idx = min(int(len(ordered) * p), len(ordered) - 1)
        return ordered[idx]

    return {
        "host": host,
        "utenti_concorrenti": users,
        "richieste_per_utente": requests,
        "richieste_totali": total,
        "durata_secondi": round(elapsed, 2),
        "throughput_ricerche_al_secondo": round(total / elapsed, 2),
        "latenza_media_s": round(statistics.mean(latencies), 3),
        "latenza_p50_s": round(pct(0.50), 3),
        "latenza_p95_s": round(pct(0.95), 3),
        "latenza_p99_s": round(pct(0.99), 3),
        "latenza_max_s": round(max(latencies), 3),
        "errori": len(errors),
        "dettaglio_errori": errors[:10],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=8)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    report = asyncio.run(
        run_load_test(args.host, args.users, args.requests, args.username, args.password)
    )
    print("\n=== Test di carico — ricerca ibrida ===")
    for key, value in report.items():
        if key != "dettaglio_errori":
            print(f"  {key}: {value}")
    if report["dettaglio_errori"]:
        print("  primi errori:")
        for err in report["dettaglio_errori"]:
            print(f"    - {err}")
    Path("load_test_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    print("\nReport salvato in load_test_report.json")


if __name__ == "__main__":
    main()
