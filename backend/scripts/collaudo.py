#!/usr/bin/env python3
"""Collaudo end-to-end del sistema RAG agentico (post-consegna).

Quattro fasi, ciascuna con esito PASS/FAIL e report finale:

1. **Ingest demo** — genera il corpus dimostrativo se assente (riusa
   ``scripts/demo_seed.py``) e lo indicizza su un ``DATA_DIR`` dedicato
   (default ``data-demo/``); idempotente: se tutto è già indicizzato salta.
2. **Domande reali** — 5 quesiti via ``run_agent`` (2 fast, 3 agentici di
   confronto) con verifiche automatiche su percorso, citazioni e warning.
3. **Knowledge graph** — (ri)costruzione del grafo (riusa
   ``scripts/kg_build.py``) e una domanda GraphRAG che richiede relazioni.
4. **Batteria eval** — genera ``eval/questions.collaudo.json`` tarato sul
   corpus demo ed esegue la batteria riusando ``app/evaluation.py``.

Prerequisito: ``MOONSHOT_API_KEY`` attiva. Lo script apre con una **probe**
(una chiamata LLM minima): se il servizio non risponde (es. 429 per saldo
esaurito/account sospeso) esce SUBITO con codice 2.

Uso (dalla cartella ``backend/``)::

    uv run python scripts/collaudo.py
    uv run python scripts/collaudo.py --data-dir data-demo --skip-kg --skip-eval

Codici di uscita: 0 = tutte le fasi PASS; 1 = almeno una fase FAIL;
2 = LLM non raggiungibile (probe fallita). Il report dettagliato è scritto
in ``<data-dir>/collaudo_report.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Permette l'import di `app` e `scripts` anche senza installazione del package
sys.path.insert(0, str(BACKEND_ROOT))

log = structlog.get_logger("collaudo")

DEFAULT_DATA_DIR = BACKEND_ROOT / "data-demo"
EVAL_QUESTIONS_PATH = BACKEND_ROOT / "eval" / "questions.collaudo.json"
REPORT_NAME = "collaudo_report.json"

# Frammento del warning emesso dal nodo ``verify`` quando la risposta non è
# ancorata ai passaggi citati: per il collaudo è un problema grave.
WARNING_ADERENZA = "affermazioni non pienamente supportate"

# Fase 2: 2 quesiti puntuali (fast) e 3 di confronto tra documenti (agentic)
DOMANDE_COLLAUDO: list[dict[str, str]] = [
    {
        "question": "Quanto dura la garanzia sugli interventi di riparazione "
        "nel contratto di manutenzione 2025?",
        "expected_path": "fast",
    },
    {
        "question": "Quanto costa il sensore di pressione SP-220 nel listino 2025?",
        "expected_path": "fast",
    },
    {
        "question": "Le condizioni di garanzia e gli SLA del contratto di "
        "manutenzione 2025 sono cambiati rispetto al contratto 2023? "
        "Cosa dice il verbale del CDA in proposito?",
        "expected_path": "agentic",
    },
    {
        "question": "Confronta canone e penali del contratto di manutenzione "
        "2023 con quelli del 2025: cosa è cambiato e quale delibera del CDA "
        "copre il nuovo contratto?",
        "expected_path": "agentic",
    },
    {
        "question": "Per sostituire i filtri della linea B: quali articoli del "
        "listino servono, quanto costano in totale e quale procedura di "
        "sicurezza devo seguire?",
        "expected_path": "agentic",
    },
]

# Fase 3: domanda GraphRAG che richiede di attraversare relazioni del grafo
DOMANDA_GRAPHRAG = (
    "Chi ha firmato il contratto di manutenzione 2025 e cosa ha deliberato "
    "il Consiglio di Amministrazione in proposito?"
)

# Fase 4: quesiti di riferimento tarati sui contenuti REALI del corpus demo
# (vedi scripts/demo_seed.py: garanzia 12→24 mesi, SLA 48→24 ore, delibera
# 2024-017 con budget € 85.000, canone 2025 € 21.500, listino SP-220 € 210,
# kit filtri FLT-100 + FLT-110)
QUESITI_COLLAUDO: list[dict[str, Any]] = [
    {
        "question": "Quanto dura la garanzia sugli interventi di riparazione "
        "prevista dal contratto di manutenzione del 2023?",
        "expected_substrings": ["12 mesi"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Quanto dura la garanzia sugli interventi di riparazione "
        "nel contratto di manutenzione 2025?",
        "expected_substrings": ["24 mesi"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Qual è il tempo massimo di intervento (SLA) per un guasto "
        "bloccante nel contratto di manutenzione 2025?",
        "expected_substrings": ["24 ore"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Quale delibera del CDA ha approvato il budget di "
        "manutenzione 2025 e a quanto ammonta il budget complessivo?",
        "expected_substrings": ["2024-017", "85.000"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Quanto costa il sensore di pressione SP-220 nel listino 2025?",
        "expected_substrings": ["SP-220", "210"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Quali ricambi servono per la sostituzione del kit filtri "
        "della linea B secondo la nota operativa?",
        "expected_substrings": ["FLT-100", "FLT-110"],
        "must_cite": True,
        "min_results": 1,
    },
    {
        "question": "Qual è il canone annuo del contratto di manutenzione 2025?",
        "expected_substrings": ["21.500"],
        "must_cite": True,
        "min_results": 1,
    },
]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argomenti CLI del collaudo."""
    parser = argparse.ArgumentParser(
        description="Collaudo end-to-end del sistema RAG agentico (4 fasi).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"DATA_DIR dedicato per ingest e indici (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--skip-kg",
        action="store_true",
        help="salta la fase 3 (knowledge graph e domanda GraphRAG)",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="salta la fase 4 (batteria di valutazione)",
    )
    return parser.parse_args(argv)


def _setup_env(data_dir: Path) -> None:
    """Punta l'applicazione sul DATA_DIR dedicato del collaudo.

    Va eseguito PRIMA del primo import/accesso a ``app.config.get_settings``
    (tutti gli import di ``app`` in questo script sono differiti nelle fasi).
    """
    import os

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["UPLOAD_DIR"] = str(data_dir / "uploads")


async def probe_llm() -> None:
    """Chiamata LLM minima: solleva se il servizio non risponde.

    ``get_kimi_client`` solleva ``LLMNotConfiguredError`` se la chiave manca;
    errori remoti (429 saldo, 401, timeout) arrivano dal client OpenAI.
    """
    from app.llm import get_kimi_client

    client = get_kimi_client()
    # margine per i "thinking model" (es. qwen3.6): il ragionamento consuma token
    await client.chat(
        [{"role": "user", "content": "Rispondi con la sola parola: OK"}],
        max_tokens=2048,
    )


# ---------------------------------------------------------------------------
# Verifiche automatiche (funzioni pure, testabili)
# ---------------------------------------------------------------------------


def verifica_risposta(
    *,
    path: str,
    expected_path: str,
    n_citations: int,
    warnings: list[str],
) -> list[str]:
    """Controlli su una risposta dell'agente; ritorna la lista dei problemi.

    - il percorso scelto deve essere quello atteso (fast/agentic);
    - almeno una citazione risolta;
    - nessun warning di aderenza grave dal nodo ``verify``.
    """
    problemi: list[str] = []
    if path != expected_path:
        problemi.append(f"percorso atteso '{expected_path}', ottenuto '{path or '-'}'")
    if n_citations < 1:
        problemi.append("nessuna citazione risolta")
    for warning in warnings:
        if WARNING_ADERENZA in warning:
            problemi.append(f"warning di aderenza grave: {warning}")
    return problemi


# ---------------------------------------------------------------------------
# Fase 1 — Ingest demo
# ---------------------------------------------------------------------------


async def _tabella_documenti() -> list[dict[str, Any]]:
    """Documenti del source demo-corpus con stato e numero di chunk."""
    from sqlalchemy import func, select

    from app.db import get_session_factory, init_db
    from app.models import Chunk, Document, Source

    await init_db()
    async with get_session_factory()() as db:
        result = await db.execute(
            select(Document.title, Document.status, func.count(Chunk.id))
            .join(Source, Document.source_id == Source.id)
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Source.name == "demo-corpus")
            .group_by(Document.id)
            .order_by(Document.title)
        )
        return [
            {"titolo": title, "stato": status, "chunk": n_chunks}
            for title, status, n_chunks in result.all()
        ]


async def fase_ingest(data_dir: Path) -> dict[str, Any]:
    """Fase 1: genera il corpus se manca e lo indicizza (idempotente)."""
    started = time.perf_counter()
    from scripts import demo_seed

    print("\n=== Fase 1 — Ingest del corpus demo ===")
    corpus_dir = demo_seed.DEFAULT_CORPUS_DIR
    if not corpus_dir.is_dir() or not any(corpus_dir.iterdir()):
        print(f"Corpus assente: generazione in {corpus_dir}…")
        created = demo_seed.generate_corpus(corpus_dir)
        print(f"  generati {len(created)} documenti")
    files = sorted(p for p in corpus_dir.iterdir() if p.is_file())

    # Idempotenza: se il DB ha già tutti i documenti indicizzati, salta
    tabella = await _tabella_documenti()
    n_indexed = sum(1 for r in tabella if r["stato"] == "indexed")
    if n_indexed >= len(files):
        print(f"Ingest saltato: {n_indexed} documenti già indicizzati.")
        esito = "PASS"
    else:
        print(f"Ingest di {len(files)} documenti su DATA_DIR={data_dir}…")
        fallito = False
        try:
            await demo_seed._ingest(corpus_dir, data_dir)
        except SystemExit:
            # demo_seed._ingest esce con codice 1 se qualche documento fallisce
            fallito = True
        tabella = await _tabella_documenti()
        esito = (
            "FAIL"
            if fallito or any(r["stato"] == "failed" for r in tabella)
            else "PASS"
        )

    print("\n  Documenti del corpus demo:")
    for r in tabella:
        print(f"    {r['titolo']:<45} {r['stato']:<10} chunk={r['chunk']}")
    print(f"  esito fase: {esito}")
    log.info("fase ingest completata", esito=esito, documenti=len(tabella))
    return {
        "nome": "ingest",
        "esito": esito,
        "durata_s": round(time.perf_counter() - started, 1),
        "documenti": tabella,
    }


# ---------------------------------------------------------------------------
# Fase 2 — Domande reali
# ---------------------------------------------------------------------------


async def fase_domande() -> dict[str, Any]:
    """Fase 2: 5 domande reali via run_agent con verifiche automatiche."""
    from app.agent import run_agent

    started = time.perf_counter()
    print("\n=== Fase 2 — Domande reali all'agente ===")
    risultati: list[dict[str, Any]] = []
    for i, item in enumerate(DOMANDE_COLLAUDO, start=1):
        question = item["question"]
        print(f"\n[{i}/{len(DOMANDE_COLLAUDO)}] {question}")
        outcome = await run_agent(question)
        print(f"  percorso: {outcome.path or '-'} (atteso: {item['expected_path']})")
        print(f"  latenza: {outcome.latency_ms} ms")
        print("  passi dell'agente:")
        for step in outcome.steps:
            print(f"    • {step.get('step')}: {step.get('detail')}")
        print(f"  risposta: {outcome.answer}")
        if outcome.citations:
            print("  citazioni risolte:")
            for c in outcome.citations:
                pagina = f", pag. {c.page}" if c.page is not None else ""
                print(f"    [{c.n}] «{c.title}»{pagina}")
        for warning in outcome.warnings:
            print(f"  warning: {warning}")

        problemi = verifica_risposta(
            path=outcome.path,
            expected_path=item["expected_path"],
            n_citations=len(outcome.citations),
            warnings=outcome.warnings,
        )
        esito = "PASS" if not problemi else "FAIL"
        print(f"  esito: {esito}" + (f" — {'; '.join(problemi)}" if problemi else ""))
        risultati.append(
            {
                "domanda": question,
                "percorso_atteso": item["expected_path"],
                "percorso": outcome.path,
                "n_citazioni": len(outcome.citations),
                "warnings": outcome.warnings,
                "latenza_ms": outcome.latency_ms,
                "risposta": outcome.answer,
                "problemi": problemi,
                "esito": esito,
            }
        )

    print("\n  --- Tabella esiti ---")
    for r in risultati:
        print(f"    {r['esito']:<5} [{r['percorso_atteso']:<7}] {r['domanda'][:70]}")
    n_fail = sum(1 for r in risultati if r["esito"] == "FAIL")
    esito = "FAIL" if n_fail else "PASS"
    print(f"  esito fase: {esito} ({len(risultati) - n_fail}/{len(risultati)} PASS)")
    log.info("fase domande completata", esito=esito, fallite=n_fail)
    return {
        "nome": "domande",
        "esito": esito,
        "durata_s": round(time.perf_counter() - started, 1),
        "domande": risultati,
    }


# ---------------------------------------------------------------------------
# Fase 3 — Knowledge graph
# ---------------------------------------------------------------------------


async def fase_kg() -> dict[str, Any]:
    """Fase 3: (ri)costruzione del KG e una domanda GraphRAG con relazioni."""
    started = time.perf_counter()
    print("\n=== Fase 3 — Knowledge graph (GraphRAG) ===")
    from scripts import kg_build

    print("Costruzione/aggiornamento del knowledge graph…")
    failures = await kg_build.run(None)

    # Abilita l'uso del grafo nel ciclo agentico (nodo kg_lookup) per la
    # domanda GraphRAG: la modifica vale solo per questo processo.
    from app.config import get_settings

    get_settings().kg_enabled = True

    from app.agent import run_agent

    print(f"\nDomanda GraphRAG: {DOMANDA_GRAPHRAG}")
    outcome = await run_agent(DOMANDA_GRAPHRAG)
    print(f"  percorso: {outcome.path or '-'} — latenza {outcome.latency_ms} ms")

    kg_steps = [s for s in outcome.steps if s.get("step") == "kg_lookup"]
    n_entities = 0
    for step in kg_steps:
        detail = str(step.get("detail") or "")
        print(f"  kg_lookup: {detail}")
        match = re.search(r"(\d+)\s+entit[àa]", detail)
        if match:
            n_entities += int(match.group(1))
    print(f"  risposta: {outcome.answer}")
    if outcome.citations:
        print("  citazioni risolte:")
        for c in outcome.citations:
            pagina = f", pag. {c.page}" if c.page is not None else ""
            print(f"    [{c.n}] «{c.title}»{pagina}")
    for warning in outcome.warnings:
        print(f"  warning: {warning}")

    problemi: list[str] = []
    if failures:
        problemi.append(f"{failures} estrazioni del knowledge graph fallite")
    if not kg_steps:
        problemi.append("lo step 'kg_lookup' non compare nel percorso dell'agente")
    elif n_entities < 1:
        problemi.append("kg_lookup senza entità riconosciute")
    esito = "PASS" if not problemi else "FAIL"
    print(f"  esito fase: {esito}" + (f" — {'; '.join(problemi)}" if problemi else ""))
    log.info("fase knowledge graph completata", esito=esito, entita=n_entities)
    return {
        "nome": "knowledge-graph",
        "esito": esito,
        "durata_s": round(time.perf_counter() - started, 1),
        "estrazioni_fallite": failures,
        "entita_riconosciute": n_entities,
        "domanda": DOMANDA_GRAPHRAG,
        "risposta": outcome.answer,
        "problemi": problemi,
    }


# ---------------------------------------------------------------------------
# Fase 4 — Batteria di valutazione
# ---------------------------------------------------------------------------


async def fase_eval() -> dict[str, Any]:
    """Fase 4: genera i quesiti di riferimento ed esegue la batteria eval."""
    started = time.perf_counter()
    print("\n=== Fase 4 — Batteria di valutazione ===")
    EVAL_QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVAL_QUESTIONS_PATH.write_text(
        json.dumps(QUESITI_COLLAUDO, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Quesiti di riferimento scritti in {EVAL_QUESTIONS_PATH}")

    from app.evaluation import load_questions
    from scripts.evaluate import run_evaluation

    questions = load_questions(EVAL_QUESTIONS_PATH)
    results = await run_evaluation(questions, verbose=False)
    overall = round(sum(r["score"] for r in results) / len(results), 3)
    passed = sum(1 for r in results if r["passed"])
    print(f"\n  quesiti superati: {passed}/{len(results)}")
    print(f"  punteggio complessivo: {overall:.3f}")
    esito = "PASS" if passed == len(results) else "FAIL"
    print(f"  esito fase: {esito}")
    log.info("fase eval completata", esito=esito, punteggio=overall)
    return {
        "nome": "eval",
        "esito": esito,
        "durata_s": round(time.perf_counter() - started, 1),
        "punteggio_complessivo": overall,
        "superati": passed,
        "totale": len(results),
        "risultati": results,
    }


# ---------------------------------------------------------------------------
# Report e entry point
# ---------------------------------------------------------------------------


def _fase_saltata(nome: str) -> dict[str, Any]:
    return {"nome": nome, "esito": "SALTATA", "durata_s": 0.0}


def _scrivi_report(data_dir: Path, fasi: list[dict[str, Any]], durata_s: float) -> Path:
    """Scrive il report JSON del collaudo nella data-dir."""
    esito = "FAIL" if any(f["esito"] == "FAIL" for f in fasi) else "PASS"
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "data_dir": str(data_dir),
        "esito_complessivo": esito,
        "durata_totale_s": round(durata_s, 1),
        "fasi": fasi,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    report_path = data_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report_path


def _stampa_riepilogo(fasi: list[dict[str, Any]], durata_s: float) -> None:
    print("\n=== Riepilogo del collaudo ===")
    for fase in fasi:
        print(f"  {fase['esito']:<8} {fase['nome']:<18} ({fase['durata_s']} s)")
    esito = "FAIL" if any(f["esito"] == "FAIL" for f in fasi) else "PASS"
    print(f"  esito complessivo: {esito} — durata totale {round(durata_s, 1)} s")


def main(argv: list[str] | None = None) -> int:
    """Entry point: probe LLM, poi le 4 fasi con report finale."""
    args = parse_args(argv)
    data_dir = args.data_dir
    _setup_env(data_dir)

    print("=== Collaudo end-to-end del sistema RAG agentico ===")
    print(f"DATA_DIR dedicato: {data_dir}")
    log.info("avvio collaudo", data_dir=str(data_dir))
    started_all = time.perf_counter()

    print("\nProbe LLM (verifica che l'account sia attivo)…")
    try:
        asyncio.run(probe_llm())
    except Exception as exc:
        # Mai stampare la chiave API: solo tipo e messaggio dell'errore
        print(
            "ERRORE: il servizio LLM non risponde — account non attivo o "
            "credito esaurito. Riprovare più tardi.\n"
            f"Dettaglio: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        log.warning("probe LLM fallita", errore=type(exc).__name__)
        return 2
    print("  LLM raggiungibile: account attivo.")

    fasi: list[dict[str, Any]] = []
    fasi.append(asyncio.run(fase_ingest(data_dir)))
    if fasi[-1]["esito"] != "FAIL":
        fasi.append(asyncio.run(fase_domande()))
        if args.skip_kg:
            fasi.append(_fase_saltata("knowledge-graph"))
        else:
            fasi.append(asyncio.run(fase_kg()))
        if args.skip_eval:
            fasi.append(_fase_saltata("eval"))
        else:
            fasi.append(asyncio.run(fase_eval()))
    else:
        print("\nIngest fallito: le fasi successive vengono saltate.")
        log.error("collaudo interrotto: ingest fallito")

    durata = time.perf_counter() - started_all
    report_path = _scrivi_report(data_dir, fasi, durata)
    _stampa_riepilogo(fasi, durata)
    print(f"  report JSON: {report_path}")
    log.info("collaudo terminato", report=str(report_path))
    return 1 if any(f["esito"] == "FAIL" for f in fasi) else 0


if __name__ == "__main__":
    sys.exit(main())
