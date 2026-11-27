"""Test della reflexion (autocorrezione su verdict "fragile") e del retrieve
parallelo (architettura agentica v2).

LLM e ricerca sono finti (nessuna rete): il fake risponde in base al marcatore
del system prompt ("classificatore", "verificatore", "critico") e produce
round di token di sintesi prefissati, diversi tra bozza e rigenerazione.
"""

import asyncio
from collections import deque
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import run_agent
from app.agent import tools as agent_tools
from app.config import get_settings

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _VERIFY, _CRITIC = "classificatore", "verificatore", "critico"


class _FakeKimi:
    """KimiClient finto: risposte JSON in coda per tipo di prompt e round di
    token di streaming distinti (bozza di ``synthesize``, rigenerazione di
    ``refine``). Registra le chiamate per le asserzioni."""

    def __init__(
        self,
        responses: dict[str, list[str]],
        stream_rounds: list[list[str]],
    ) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_rounds = deque(stream_rounds)
        self.calls: list[str] = []

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                self.calls.append(marker)
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]  # l'ultima risposta si ripete
        raise AssertionError(f"prompt di sistema non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        if len(self._stream_rounds) > 1:
            tokens = self._stream_rounds.popleft()
        else:
            tokens = self._stream_rounds[0]
        for token in tokens:
            yield token


class _FakeSearch:
    """Sostituto di tools.hybrid_search: stesse hit per ogni query, traccia
    interrogazioni e concorrenza massima osservata."""

    def __init__(self, hits: list[dict[str, Any]], delay: float = 0.0) -> None:
        self._hits = hits
        self._delay = delay
        self.queries: list[str] = []
        self.current = 0
        self.max_seen = 0

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        if self._delay:
            await asyncio.sleep(self._delay)
        self.current -= 1
        self.queries.append(query)
        return [dict(h) for h in self._hits]


def _hit(chunk_id: int, document_id: int, score: float) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": f"Documento {document_id}",
        "snippet": f"snippet {chunk_id}",
        "context_text": f"contenuto esteso del passaggio {chunk_id}",
        "score": score,
        "page": 3,
        "section_path": ["Capitolo 1"],
        "source_name": "Archivio locale",
        "doc_type": "pdf",
        "modified_at": "2024-03-01T12:00:00",
    }


@pytest.fixture
def reflex_env(monkeypatch):
    """Monta fake LLM e ricerca finta con i flag v2/reflexion configurati."""

    def install(
        fake_kimi: _FakeKimi,
        hits: list[dict[str, Any]],
        *,
        critic: bool = True,
        reflexion: bool = True,
        search_delay: float = 0.0,
    ) -> tuple[_FakeKimi, _FakeSearch]:
        monkeypatch.setenv("AGENT_MODE", "graph")
        monkeypatch.setenv("CRITIC_ENABLED", "true" if critic else "false")
        monkeypatch.setenv("REFLEXION_ENABLED", "true" if reflexion else "false")
        get_settings.cache_clear()
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)
        search = _FakeSearch(hits, delay=search_delay)
        monkeypatch.setattr(agent_tools, "hybrid_search", search)
        return fake_kimi, search

    return install


# --- reflexion -----------------------------------------------------------------


async def test_reflexion_fragile_rigenera_e_riverifica(reflex_env) -> None:
    """Verdict "fragile" → la risposta è rigenerata con le criticità come
    feedback e riverificata; il secondo verdict (solido) fa fede."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: [
                '{"verdict": "fragile", "issues": ["la scadenza citata non '
                'compare nei passaggi"]}',
                '{"verdict": "solido", "issues": []}',
            ],
        },
        stream_rounds=[["Bozza fragile [1]."], ["Risposta corretta [1]."]],
    )
    reflex_env(fake, [_hit(10, 7, 0.9)])
    tokens: list[str] = []
    result = await run_agent(
        "entro quando va richiesto il reso?", session=None, token_sink=tokens.append
    )

    # la risposta finale è la versione rigenerata, non la bozza
    assert result.answer == "Risposta corretta [1]."
    # entrambi i round sono stati streamati (il client azzera all'evento reset)
    assert tokens == ["Bozza fragile [1].", "Risposta corretta [1]."]
    # refine tra le due verifiche; il secondo verdict (solido) fa fede
    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
        "refine",
        "verify",
    ]
    assert result.confidence == "alta"
    assert fake.calls.count(_CRITIC) == 2
    assert any("1 criticità" in s["detail"] for s in result.steps if s["step"] == "refine")


async def test_reflexion_una_sola_rigenerazione_anche_se_sempre_fragile(reflex_env) -> None:
    """Se anche la risposta rigenerata è fragile il grafo termina comunque:
    la rigenerazione è una tantum (flag ``refined``), niente loop."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: ['{"verdict": "fragile", "issues": ["sempre fragile"]}'],
        },
        stream_rounds=[["Bozza [1]."], ["Rigenerata [1]."]],
    )
    reflex_env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("quesito?", session=None)

    steps = [s["step"] for s in result.steps]
    assert steps.count("refine") == 1
    assert steps.count("verify") == 2
    assert result.confidence == "bassa"
    assert result.answer == "Rigenerata [1]."


async def test_reflexion_disabilitata_niente_refine(reflex_env) -> None:
    """REFLEXION_ENABLED=false: il verdict fragile produce solo confidence
    bassa e warning, come prima (comportamento precedente)."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: ['{"verdict": "fragile", "issues": ["dato non trovato"]}'],
        },
        stream_rounds=[["Bozza [1]."]],
    )
    reflex_env(fake, [_hit(10, 7, 0.9)], reflexion=False)
    result = await run_agent("quesito?", session=None)

    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    assert result.answer == "Bozza [1]."
    assert result.confidence == "bassa"


async def test_reflexion_verdict_solido_niente_refine(reflex_env) -> None:
    """Verdict solido/parziale: nessuna rigenerazione, nessuna latenza extra."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: ['{"verdict": "solido", "issues": []}'],
        },
        stream_rounds=[["Risposta [1]."]],
    )
    reflex_env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("quesito?", session=None)

    assert "refine" not in [s["step"] for s in result.steps]
    assert result.answer == "Risposta [1]."
    assert result.confidence == "alta"


# --- retrieve parallelo ----------------------------------------------------------


def _plan_tre_quesiti() -> str:
    return (
        '{"sub_questions": ['
        '{"query": "rimborso trasferte 2023", "purpose": "versione 2023"}, '
        '{"query": "rimborso trasferte 2024", "purpose": "versione 2024"}, '
        '{"query": "diaria giornaliera estero", "purpose": "diaria"}'
        "]}"
    )


async def test_retrieve_interrogazioni_in_parallelo(reflex_env) -> None:
    """Le interrogazioni pianificate corrono in concorrenza (max_seen > 1) e
    la fusione resta deterministica: queries_run nell'ordine del piano."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "confronto"}'],
            "pianificatore": [_plan_tre_quesiti()],
            "valutatore": ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_rounds=[["Confronto completo [1]."]],
    )
    _, search = reflex_env(fake, [_hit(10, 7, 0.9)], critic=False, search_delay=0.02)
    result = await run_agent("confronta i rimborsi 2023 e 2024 e la diaria", session=None)

    # parallelismo reale: più ricerche in volo contemporaneamente
    assert search.max_seen > 1
    # ordine deterministico delle interrogazioni tracciate
    assert result.queries_run == [
        "rimborso trasferte 2023",
        "rimborso trasferte 2024",
        "diaria giornaliera estero",
    ]
    assert sorted(search.queries) == sorted(result.queries_run)


async def test_retrieve_tetto_di_concorrenza(reflex_env, monkeypatch) -> None:
    """RETRIEVE_MAX_CONCURRENCY limita le ricerche contemporanee."""
    monkeypatch.setenv("RETRIEVE_MAX_CONCURRENCY", "2")
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "confronto"}'],
            "pianificatore": [_plan_tre_quesiti()],
            "valutatore": ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_rounds=[["Confronto completo [1]."]],
    )
    _, search = reflex_env(fake, [_hit(10, 7, 0.9)], critic=False, search_delay=0.02)
    result = await run_agent("confronta i rimborsi 2023 e 2024 e la diaria", session=None)

    assert search.max_seen == 2
    assert len(result.queries_run) == 3
