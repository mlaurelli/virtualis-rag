"""Test della logica di scoring della batteria di valutazione (cap. 12).

Solo la funzione pura ``score_answer`` e la validazione dei quesiti:
nessuna chiamata LLM, nessuna esecuzione dell'agente.
"""

import json

import pytest

from app.evaluation import load_questions, score_answer

_CITATION = {"n": 1, "chunk_id": 10, "document_id": 7, "title": "doc", "page": 3}


def test_score_tutto_superato() -> None:
    """Tutti i controlli superati → score 1.0 e passed=True."""
    expected = {
        "question": "q",
        "expected_substrings": ["rimborso", "trasferta"],
        "must_cite": True,
        "min_results": 2,
    }
    out = score_answer(
        "Il rimborso delle spese di trasferta è previsto [1].",
        [_CITATION],
        expected,
        retrieved_count=3,
        latency_ms=1234,
    )
    assert out["passed"] is True
    assert out["score"] == 1.0
    assert out["latency_ms"] == 1234
    assert all(check["passed"] for check in out["checks"].values())


def test_score_substring_mancante() -> None:
    """Una sottostringa assente fa fallire solo il suo controllo (score 2/3)."""
    expected = {
        "question": "q",
        "expected_substrings": ["rimborso", "scadenza"],
        "must_cite": True,
        "min_results": 1,
    }
    out = score_answer(
        "Il rimborso è previsto [1].",
        [_CITATION],
        expected,
        retrieved_count=1,
    )
    assert out["passed"] is False
    assert out["score"] == pytest.approx(2 / 3, abs=1e-3)
    check = out["checks"]["expected_substrings"]
    assert check["found"] == ["rimborso"]
    assert check["missing"] == ["scadenza"]


def test_score_substring_case_insensitive() -> None:
    """Il confronto delle sottostringhe è case-insensitive."""
    expected = {"question": "q", "expected_substrings": ["Rimborso SPESE"]}
    out = score_answer("il rimborso spese è ammesso", [], expected)
    assert out["checks"]["expected_substrings"]["passed"] is True


def test_score_must_cite_senza_citazioni() -> None:
    """must_cite=True senza citazioni → controllo fallito."""
    expected = {"question": "q", "must_cite": True}
    out = score_answer("risposta senza citazioni", [], expected)
    check = out["checks"]["must_cite"]
    assert check["passed"] is False
    assert check["citations"] == 0
    # must_cite=False (o assente): il controllo passa sempre
    assert score_answer("testo", [], {"question": "q"})["checks"]["must_cite"]["passed"]


def test_score_min_results_non_raggiunto() -> None:
    """Passaggi recuperati sotto la soglia → controllo min_results fallito."""
    expected = {"question": "q", "min_results": 3}
    out = score_answer("risposta", [_CITATION], expected, retrieved_count=1)
    check = out["checks"]["min_results"]
    assert check["passed"] is False
    assert check["required"] == 3
    assert check["actual"] == 1
    # soglia raggiunta esattamente → superato
    assert score_answer("r", [], expected, retrieved_count=3)["checks"]["min_results"]["passed"]


def test_score_nessuna_attesa() -> None:
    """Quesito senza attese specifiche: tutti i controlli superati."""
    out = score_answer("qualunque risposta", [], {"question": "q"})
    assert out["passed"] is True
    assert out["score"] == 1.0


def test_load_questions_valido(tmp_path) -> None:
    """File ben formato → lista di quesiti."""
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps([{"question": "Q?", "expected_substrings": ["x"]}]),
        encoding="utf-8",
    )
    assert load_questions(path) == [{"question": "Q?", "expected_substrings": ["x"]}]


def test_load_questions_non_validi(tmp_path) -> None:
    """File vuoti, non-lista o senza 'question' → ValueError."""
    for content in ("[]", '{"question": "Q?"}', '[{"must_cite": true}]'):
        path = tmp_path / "questions.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_questions(path)
