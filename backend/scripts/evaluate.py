#!/usr/bin/env python3
"""Batteria di valutazione continua dell'agente documentale (cap. 12).

Esegue i quesiti di riferimento (default: ``eval/questions.json``) contro
l'agente reale e produce un report tabellare a console più un file JSON
(default: ``eval_report.json``) con il punteggio complessivo.

Prerequisiti:

- ``MOONSHOT_API_KEY`` configurata (la valutazione chiama il LLM reale);
- corpus già indicizzato nel ``DATA_DIR`` configurato (ingestion completata).

Uso (dalla cartella ``backend/``)::

    uv run python scripts/evaluate.py
    uv run python scripts/evaluate.py --questions eval/questions.json \
        --output eval_report.json --verbose
    uv run python scripts/evaluate.py --from-feedback   # + quesiti dai feedback negativi

Codici di uscita: 0 = tutti i quesiti superati; 1 = almeno un quesito
fallito; 2 = errore di configurazione (chiave assente, file quesiti illeggibile).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Permette l'import di `app` anche senza installazione del package
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.evaluation import is_abstention, load_questions, score_answer  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Argomenti CLI dello script di valutazione."""
    parser = argparse.ArgumentParser(
        description="Batteria di valutazione continua dell'agente documentale.",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=BACKEND_ROOT / "eval" / "questions.json",
        help="file JSON dei quesiti di riferimento (default: eval/questions.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=BACKEND_ROOT / "eval_report.json",
        help="destinazione del report JSON (default: eval_report.json)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="stampa a console anche le risposte complete dell'agente",
    )
    parser.add_argument(
        "--from-feedback",
        action="store_true",
        help=(
            "riesegue anche i quesiti dei feedback negativi letti dal DB, "
            "come batteria supplementare (sezione separata del report; "
            "non influenza il codice di uscita)"
        ),
    )
    return parser.parse_args()


async def load_feedback_questions() -> list[dict[str, Any]]:
    """Quesiti dei feedback negativi, deduplicati, in ordine di segnalazione.

    I feedback non hanno attese dichiarate (``expected_substrings`` ecc.):
    lo scoring risulta banalmente pieno, ma la riesecuzione rende i problemi
    segnalati dagli utenti regressioni monitorate e confrontabili nel tempo.
    """
    from sqlalchemy import select

    from app.db import get_session_factory, init_db
    from app.models import Feedback

    await init_db()  # i test puntano il DB a una directory temporanea
    async with get_session_factory()() as db:
        questions = (
            (await db.execute(select(Feedback.question).where(Feedback.rating < 0)))
            .scalars()
            .all()
        )
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for question in questions:
        question = question.strip()
        if question and question not in seen:
            seen.add(question)
            unique.append({"question": question})
    return unique


async def run_evaluation(
    questions: list[dict[str, Any]], *, verbose: bool
) -> list[dict[str, Any]]:
    """Esegue l'agente su ogni quesito e ne valuta la risposta."""
    from app.agent import run_agent

    results: list[dict[str, Any]] = []
    for i, expected in enumerate(questions, start=1):
        question = expected["question"]
        print(f"[{i}/{len(questions)}] {question}", flush=True)
        outcome = await run_agent(question)
        scored = score_answer(
            outcome.answer,
            [c.model_dump() for c in outcome.citations],
            expected,
            retrieved_count=len(outcome.retrieved_chunk_ids),
            latency_ms=outcome.latency_ms,
        )
        esito = "OK  " if scored["passed"] else "FAIL"
        print(
            f"  {esito} score={scored['score']:.3f} "
            f"citazioni={len(outcome.citations)} "
            f"passaggi={len(outcome.retrieved_chunk_ids)} "
            f"latenza={outcome.latency_ms} ms percorso={outcome.path or '-'}",
            flush=True,
        )
        if verbose:
            print(f"  risposta: {outcome.answer}\n", flush=True)
        results.append(
            {
                "question": question,
                "answer": outcome.answer,
                "path": outcome.path,
                "warnings": outcome.warnings,
                # Metriche anti-allucinazione: la risposta è un'astensione?
                "abstained": is_abstention(outcome.answer),
                "expects_no_answer": bool(expected.get("expects_no_answer")),
                **scored,
            }
        )
    return results


def hallucination_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Metriche riassuntive anti-allucinazione della batteria.

    - ``hallucination_rate``: tra i quesiti ``expects_no_answer``, frazione
      in cui l'agente ha risposto NEL MERITO invece di astenersi (il caso
      da eliminare);
    - ``over_abstention_rate``: tra i quesiti normali, frazione degenerata
      in astensione (guardia contro l'eccesso di prudenza).
    ``None`` per le frazioni senza denominatore.
    """
    no_answer = [r for r in results if r.get("expects_no_answer")]
    normal = [r for r in results if not r.get("expects_no_answer")]
    hallucinated = sum(1 for r in no_answer if not r.get("abstained"))
    over_abstained = sum(1 for r in normal if r.get("abstained"))
    return {
        "no_answer_questions": len(no_answer),
        "hallucinated": hallucinated,
        "hallucination_rate": (
            round(hallucinated / len(no_answer), 3) if no_answer else None
        ),
        "normal_questions": len(normal),
        "over_abstained": over_abstained,
        "over_abstention_rate": (
            round(over_abstained / len(normal), 3) if normal else None
        ),
    }


def main() -> int:
    """Entry point: configurazione, esecuzione, report."""
    args = parse_args()
    settings = get_settings()
    if not settings.llm_configured:
        print(
            "ERRORE: MOONSHOT_API_KEY non configurata. La batteria di "
            "valutazione esegue l'agente reale: impostare la chiave "
            "nell'ambiente o nel file .env e riprovare.",
            file=sys.stderr,
        )
        return 2
    try:
        questions = load_questions(args.questions)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERRORE nel caricamento dei quesiti: {exc}", file=sys.stderr)
        return 2

    results = asyncio.run(run_evaluation(questions, verbose=args.verbose))
    overall = round(sum(r["score"] for r in results) / len(results), 3)
    passed = sum(1 for r in results if r["passed"])

    metrics = hallucination_metrics(results)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": settings.kimi_model,
        "questions_file": str(args.questions),
        "overall_score": overall,
        "passed": passed,
        "failed": len(results) - passed,
        "hallucination_metrics": metrics,
        "results": results,
    }

    print("\n=== Esito della batteria ===")
    print(f"quesiti superati: {passed}/{len(results)}")
    print(f"punteggio complessivo: {overall:.3f}")
    if metrics["no_answer_questions"]:
        print(
            f"hallucination_rate: {metrics['hallucination_rate']} "
            f"({metrics['hallucinated']}/{metrics['no_answer_questions']} "
            "quesiti no-answer con risposta nel merito)"
        )
    if metrics["normal_questions"]:
        print(
            f"over_abstention_rate: {metrics['over_abstention_rate']} "
            f"({metrics['over_abstained']}/{metrics['normal_questions']} "
            "quesiti normali degenerati in astensione)"
        )

    if args.from_feedback:
        feedback_questions = asyncio.run(load_feedback_questions())
        if not feedback_questions:
            print("\n=== Quesiti da feedback negativi ===")
            print("nessun feedback negativo presente nel DB")
            report["feedback_results"] = []
        else:
            print("\n=== Quesiti da feedback negativi ===")
            feedback_results = asyncio.run(
                run_evaluation(feedback_questions, verbose=args.verbose)
            )
            feedback_passed = sum(1 for r in feedback_results if r["passed"])
            print(
                f"quesiti da feedback rieseguiti: {len(feedback_results)} "
                f"(superati: {feedback_passed})"
            )
            report["feedback_results"] = feedback_results

    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report JSON: {args.output}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
