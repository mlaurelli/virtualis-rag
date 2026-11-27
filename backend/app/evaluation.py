"""Logica di scoring della batteria di valutazione continua (cap. 12).

La funzione pura :func:`score_answer` valuta una risposta dell'agente
rispetto alle attese dichiarate in un quesito di riferimento: nessuna
chiamata LLM, nessun I/O, così da essere testabile in isolamento.
Lo script CLI ``scripts/evaluate.py`` orchestra l'esecuzione reale
(run dell'agente su ogni quesito) e produce il report complessivo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Frase standard con cui l'agente DICHIARA l'astensione (gate di pertinenza
# in ``app.agent.graph``); centralizzata qui così l'eval la riconosce senza
# duplicare la stringa.
ABSTENTION_PREFIX = "Nel materiale indicizzato non ho trovato documenti pertinenti"

# Pattern (lowercase) che qualificano una risposta come astensione: coprono
# la frase standard del gate, la risposta "nessun passaggio" storica e le
# formulazioni libere ammesse dai prompt di sintesi/refine.
_NO_ANSWER_PATTERNS = (
    "non ho trovato",
    "non contiene informazioni",
    "non risultano informazioni",
    "non sono presenti informazioni",
    "non è presente alcuna informazione",
)


def is_abstention(answer: str) -> bool:
    """True se la risposta è una dichiarazione di astensione (no-answer)."""
    lowered = answer.lower()
    return any(pattern in lowered for pattern in _NO_ANSWER_PATTERNS)


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Carica e valida la batteria di quesiti da un file JSON.

    Formato atteso: lista non vuota di oggetti con chiavi
    ``question`` (str, obbligatoria), ``expected_substrings`` (list[str]),
    ``must_cite`` (bool) e ``min_results`` (int).
    """
    questions = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{path}: attesa una lista non vuota di quesiti")
    for i, question in enumerate(questions, start=1):
        if not isinstance(question, dict) or not str(question.get("question") or "").strip():
            raise ValueError(f"{path}: quesito #{i} senza chiave 'question' valida")
    return questions


def score_answer(
    answer: str,
    citations: list[dict[str, Any]],
    expected: dict[str, Any],
    *,
    retrieved_count: int = 0,
    latency_ms: int = 0,
) -> dict[str, Any]:
    """Valuta una risposta rispetto alle attese (funzione pura, no LLM).

    Tre controlli indipendenti, di peso uguale:

    - ``expected_substrings``: ogni sottostringa attesa deve comparire nella
      risposta (confronto case-insensitive);
    - ``must_cite``: almeno una citazione presente nella risposta;
    - ``min_results``: passaggi recuperati dall'agente >= soglia richiesta.

    Ritorna un dict con ``score`` (frazione di controlli superati),
    ``passed`` (True solo se TUTTI i controlli sono superati) e il
    dettaglio dei singoli controlli.

    QUESITI ANTI-ALLUCINAZIONE (``expects_no_answer: true``): la risposta
    attesa è un'ASTENSIONE (:func:`is_abstention`); il controllo sostituisce
    i tre standard — una risposta "nel merito" su un fatto assente dal
    corpus è il fallimento che la batteria deve intercettare.
    """
    if expected.get("expects_no_answer"):
        abstained = is_abstention(answer)
        return {
            "score": 1.0 if abstained else 0.0,
            "passed": abstained,
            "checks": {
                "expects_no_answer": {
                    "passed": abstained,
                    "abstained": abstained,
                }
            },
            "latency_ms": latency_ms,
        }
    lowered = answer.lower()
    substrings = [str(s) for s in (expected.get("expected_substrings") or [])]
    missing = [s for s in substrings if s.lower() not in lowered]
    min_results = int(expected.get("min_results") or 0)
    checks: dict[str, dict[str, Any]] = {
        "expected_substrings": {
            "passed": not missing,
            "found": [s for s in substrings if s not in missing],
            "missing": missing,
        },
        "must_cite": {
            "passed": not expected.get("must_cite") or bool(citations),
            "required": bool(expected.get("must_cite")),
            "citations": len(citations),
        },
        "min_results": {
            "passed": retrieved_count >= min_results,
            "required": min_results,
            "actual": retrieved_count,
        },
    }
    passed_count = sum(1 for check in checks.values() if check["passed"])
    return {
        "score": round(passed_count / len(checks), 3),
        "passed": passed_count == len(checks),
        "checks": checks,
        "latency_ms": latency_ms,
    }
