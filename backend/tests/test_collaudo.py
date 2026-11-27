"""Test dello script di collaudo end-to-end: LLM e fasi sempre mockati.

Nessuna chiamata reale: la probe, l'agente e le singole fasi sono
monkeypatchate; lo script non deve mai partire in esecuzione reale.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from scripts import collaudo  # noqa: E402


def _fase_ok(nome: str) -> Any:
    """Finta fase conclusa con successo (stessa firma delle fasi reali)."""

    async def _fase(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"nome": nome, "esito": "PASS", "durata_s": 0.0}

    return _fase


def test_verifica_risposta_controlli() -> None:
    """La funzione pura segnala percorso errato, zero citazioni, aderenza grave."""
    assert (
        collaudo.verifica_risposta(
            path="fast", expected_path="fast", n_citations=1, warnings=[]
        )
        == []
    )
    assert collaudo.verifica_risposta(
        path="fast", expected_path="agentic", n_citations=1, warnings=[]
    )
    assert collaudo.verifica_risposta(
        path="agentic", expected_path="agentic", n_citations=0, warnings=[]
    )
    assert collaudo.verifica_risposta(
        path="agentic",
        expected_path="agentic",
        n_citations=1,
        warnings=["affermazioni non pienamente supportate dai passaggi citati: x"],
    )


def test_probe_fallita_exit_2(monkeypatch, tmp_path) -> None:
    """Probe LLM fallita (es. 429 account sospeso) → exit 2, nessun report."""

    async def _probe_ko() -> None:
        raise RuntimeError("Error code: 429 - saldo insufficiente")

    monkeypatch.setattr(collaudo, "_setup_env", lambda data_dir: None)
    monkeypatch.setattr(collaudo, "probe_llm", _probe_ko)
    rc = collaudo.main(["--data-dir", str(tmp_path)])
    assert rc == 2
    assert not (tmp_path / collaudo.REPORT_NAME).exists()


def test_flusso_completo_fasi_mockate(monkeypatch, tmp_path) -> None:
    """Tutte le fasi PASS → exit 0 e report JSON scritto nella data-dir."""

    async def _probe_ok() -> None:
        return None

    monkeypatch.setattr(collaudo, "_setup_env", lambda data_dir: None)
    monkeypatch.setattr(collaudo, "probe_llm", _probe_ok)
    monkeypatch.setattr(collaudo, "fase_ingest", _fase_ok("ingest"))
    monkeypatch.setattr(collaudo, "fase_domande", _fase_ok("domande"))
    monkeypatch.setattr(collaudo, "fase_kg", _fase_ok("knowledge-graph"))
    monkeypatch.setattr(collaudo, "fase_eval", _fase_ok("eval"))

    rc = collaudo.main(["--data-dir", str(tmp_path)])
    assert rc == 0
    report = json.loads((tmp_path / collaudo.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["esito_complessivo"] == "PASS"
    assert [f["nome"] for f in report["fasi"]] == [
        "ingest",
        "domande",
        "knowledge-graph",
        "eval",
    ]


def test_flusso_completo_con_fase_fail(monkeypatch, tmp_path) -> None:
    """Una fase FAIL → exit 1 anche se le altre passano."""

    async def _probe_ok() -> None:
        return None

    monkeypatch.setattr(collaudo, "_setup_env", lambda data_dir: None)
    monkeypatch.setattr(collaudo, "probe_llm", _probe_ok)
    monkeypatch.setattr(collaudo, "fase_ingest", _fase_ok("ingest"))

    async def _domande_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"nome": "domande", "esito": "FAIL", "durata_s": 0.0}

    monkeypatch.setattr(collaudo, "fase_domande", _domande_fail)
    monkeypatch.setattr(collaudo, "fase_kg", _fase_ok("knowledge-graph"))
    monkeypatch.setattr(collaudo, "fase_eval", _fase_ok("eval"))

    rc = collaudo.main(["--data-dir", str(tmp_path), "--skip-eval"])
    assert rc == 1
    report = json.loads((tmp_path / collaudo.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["esito_complessivo"] == "FAIL"
    esiti = {f["nome"]: f["esito"] for f in report["fasi"]}
    assert esiti["domande"] == "FAIL"
    assert esiti["eval"] == "SALTATA"


def test_percorso_sbagliato_segnala_fail(monkeypatch) -> None:
    """run_agent mockato con percorso 'fast' ovunque: le domande agentiche
    devono risultare FAIL (percorso atteso diverso)."""
    import app.agent
    from app.agent import AgentResult, Citation

    fake = AgentResult(
        answer="La garanzia dura 24 mesi [1].",
        path="fast",
        citations=[Citation(n=1, document_id=1, title="Contratto 2025", page=1)],
        steps=[{"step": "classify", "detail": "percorso=fast", "ts": "2026-01-01"}],
        latency_ms=10,
    )

    async def _fake_run_agent(question: str, **kwargs: Any) -> AgentResult:
        return fake

    monkeypatch.setattr(app.agent, "run_agent", _fake_run_agent)
    esito = asyncio.run(collaudo.fase_domande())
    assert esito["esito"] == "FAIL"
    fallite = [d for d in esito["domande"] if d["esito"] == "FAIL"]
    # Le 3 domande agentiche falliscono per percorso errato; le fast passano
    assert len(fallite) == 3
    assert all(any("percorso" in p for p in d["problemi"]) for d in fallite)
