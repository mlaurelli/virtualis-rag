"""Test del knowledge graph ATTIVO nel ciclo agentico (nodo ``kg_lookup``).

Il KimiClient è sempre MOCKATO (stessa meccanica di test_agent): risposte
JSON in coda per tipo di prompt e token di sintesi prefissati, con spy sul
payload inviato alla sintesi. La ricerca ibrida è un fake che registra
query e filtri; il knowledge graph è popolato sul DB di test reale
(``tmp_settings``), così ``entity_subgraph``/``documents_for_entities``
girano sul serio.
"""

from collections import deque
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import run_agent
from app.agent import tools as agent_tools
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.knowledge import query as kg_query
from app.models import Chunk, Document, Entity, EntityMention, Relation, Source

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _PLAN, _EVALUATE, _VERIFY = (
    "classificatore",
    "pianificatore",
    "valutatore",
    "verificatore",
)


class _FakeKimi:
    """KimiClient finto: risposte JSON per tipo di prompt + token di sintesi
    prefissati. Registra i messaggi della sintesi (spy sul payload LLM)."""

    def __init__(self, responses: dict[str, list[str]], stream_tokens: list[str]) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_tokens = stream_tokens
        self.stream_messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]
        raise AssertionError(f"prompt di sistema non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        self.stream_messages.append(messages)
        for token in self._stream_tokens:
            yield token


class _FakeSearch:
    """Sostituto di tools.hybrid_search: registra (query, filtri) e serve
    hit diverse per la ricerca vincolata ai documenti del KG."""

    def __init__(
        self, hits: list[dict[str, Any]], kg_hits: list[dict[str, Any]] | None = None
    ) -> None:
        self._hits = hits
        self._kg_hits = kg_hits or []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.calls.append((query, filters))
        if filters and filters.get("document_ids"):
            return [dict(h) for h in self._kg_hits]
        return [dict(h) for h in self._hits]


def _hit(chunk_id: int, document_id: int, score: float) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": f"Documento {document_id}",
        "snippet": f"snippet {chunk_id}",
        # Il contesto menziona le entità del quesito di test: il check
        # deterministico dei termini distintivi (anti-allucinazione) richiede
        # che almeno un passaggio parli dell'oggetto della domanda
        "context_text": (
            f"contenuto esteso del passaggio {chunk_id}: Mario Rossi ha "
            "firmato il contratto 2024/15"
        ),
        "score": score,
        "page": 1,
        "section_path": ["Capitolo 1"],
        "source_name": "archivio",
        "doc_type": "pdf",
        "modified_at": "2024-03-01T12:00:00",
    }


@pytest.fixture
def fake_env(monkeypatch):
    """Monta KimiClient e hybrid_search finti nel grafo."""

    def install(
        fake_kimi: _FakeKimi,
        hits: list[dict[str, Any]],
        kg_hits: list[dict[str, Any]] | None = None,
    ) -> tuple[_FakeKimi, _FakeSearch]:
        fake_search = _FakeSearch(hits, kg_hits)
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)
        monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)
        return fake_kimi, fake_search

    return install


def _enable_kg(monkeypatch, enabled: bool = True) -> None:
    """KG on/off via env (ha precedenza su .env) + settings ricaricate."""
    monkeypatch.setenv("KG_ENABLED", "true" if enabled else "false")
    get_settings.cache_clear()


async def _seed_kg() -> None:
    """Grafo di test: due documenti, entità con mention e una relazione."""
    await init_db()
    async with get_session_factory()() as session:
        session.add(Source(id=5, name="archivio", type="local_fs"))
        session.add_all(
            [
                Document(
                    id=60,
                    source_id=5,
                    external_id="verbale.pdf",
                    title="Verbale CDA 2024",
                    doc_type="pdf",
                    status="indexed",
                ),
                Document(
                    id=61,
                    source_id=5,
                    external_id="contratto.pdf",
                    title="Contratto 2024/15",
                    doc_type="pdf",
                    status="indexed",
                ),
            ]
        )
        session.add_all(
            [
                Chunk(id=600, document_id=60, ord=0, text="v", context_text="v"),
                Chunk(id=610, document_id=61, ord=0, text="c", context_text="c"),
            ]
        )
        session.add_all(
            [
                Entity(id=1, name="mario rossi", display_name="Mario Rossi", type="persona"),
                Entity(
                    id=2,
                    name="contratto 2024/15",
                    display_name="Contratto 2024/15",
                    type="contratto",
                ),
                Entity(id=3, name="alfa", display_name="Alfa", type="progetto"),
                Entity(id=4, name="cde", display_name="CDE", type="organizzazione"),
                Entity(id=5, name="acme s.p.a.", display_name="ACME S.p.A.", type="organizzazione"),
            ]
        )
        session.add_all(
            [
                EntityMention(entity_id=1, document_id=60),
                EntityMention(entity_id=1, document_id=61),
                EntityMention(entity_id=2, document_id=61),
            ]
        )
        session.add(
            Relation(
                subject_id=1,
                predicate="ha_firmato",
                object_id=2,
                document_id=61,
            )
        )
        await session.commit()


def _agentic_fake(stream_tokens: list[str]) -> _FakeKimi:
    return _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "quesito multi-entità"}'],
            _PLAN: [
                '{"sub_questions": ['
                '{"query": "firma contratto 2024/15 mario rossi", "purpose": "chi ha firmato"}'
                "]}"
            ],
            _EVALUATE: ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=stream_tokens,
    )


_QUESTION = "che relazione c'è tra Mario Rossi e il Contratto 2024/15?"


# --- percorso agentic con KG attivo -------------------------------------------


async def test_kg_lookup_alimenta_sintesi_e_ricerche(tmp_settings, fake_env, monkeypatch) -> None:
    """KG attivo e popolato: kg_lookup riconosce le entità, le relazioni
    arrivano al prompt di sintesi (spy) e il documento collegato entra nei
    risultati tramite una ricerca vincolata."""
    _enable_kg(monkeypatch)
    await _seed_kg()
    fake = _agentic_fake(["Mario Rossi ha firmato il contratto [1]."])
    _, search = fake_env(fake, [_hit(610, 61, 0.9)], kg_hits=[_hit(600, 60, 0.8)])
    async with get_session_factory()() as session:
        result = await run_agent(_QUESTION, session=session)

    # nodo tracciato tra plan e retrieve, con entità e relazioni usate
    assert [s["step"] for s in result.steps] == [
        "classify",
        "plan",
        "kg_lookup",
        "retrieve",
        "evaluate",
        "synthesize",
        "verify",
    ]
    kg_step = next(s for s in result.steps if s["step"] == "kg_lookup")
    assert "2 entità riconosciute" in kg_step["detail"]
    assert "Mario Rossi" in kg_step["detail"]
    assert "1 relazioni" in kg_step["detail"]

    # il contesto del grafo arriva al prompt di sintesi come guida
    user_msg = fake.stream_messages[0][1]["content"]
    assert "CONTESTO DAL KNOWLEDGE GRAPH" in user_msg
    assert "Mario Rossi —ha_firmato→ Contratto 2024/15 (da «Contratto 2024/15»)" in user_msg

    # il documento collegato via KG (60, non coperto dalle ricerche ordinarie)
    # entra nei risultati tramite la ricerca vincolata
    assert result.retrieved_chunk_ids == [600, 610]
    kg_calls = [f for _, f in search.calls if f and f.get("document_ids")]
    assert kg_calls == [{"document_ids": [60]}]
    retrieve_step = next(s for s in result.steps if s["step"] == "retrieve")
    assert "knowledge graph" in retrieve_step["detail"]


async def test_kg_disabilitato_flusso_identico(tmp_settings, fake_env, monkeypatch) -> None:
    """KG spento (anche se popolato): nessuno step kg_lookup, nessuna ricerca
    vincolata, nessun contesto nel prompt di sintesi."""
    _enable_kg(monkeypatch, enabled=False)
    await _seed_kg()
    fake = _agentic_fake(["Risposta [1]."])
    _, search = fake_env(fake, [_hit(610, 61, 0.9)])
    async with get_session_factory()() as session:
        result = await run_agent(_QUESTION, session=session)

    assert [s["step"] for s in result.steps] == [
        "classify",
        "plan",
        "retrieve",
        "evaluate",
        "synthesize",
        "verify",
    ]
    assert all(not (f and f.get("document_ids")) for _, f in search.calls)
    assert "KNOWLEDGE GRAPH" not in fake.stream_messages[0][1]["content"]
    assert result.retrieved_chunk_ids == [610]


async def test_kg_vuoto_nessuno_step(tmp_settings, fake_env, monkeypatch) -> None:
    """KG attivo ma grafo vuoto: comportamento identico a oggi."""
    _enable_kg(monkeypatch)
    await init_db()  # tabelle create, nessuna entità
    fake = _agentic_fake(["Risposta [1]."])
    _, search = fake_env(fake, [_hit(610, 61, 0.9)])
    async with get_session_factory()() as session:
        result = await run_agent(_QUESTION, session=session)

    assert "kg_lookup" not in [s["step"] for s in result.steps]
    assert all(not (f and f.get("document_ids")) for _, f in search.calls)
    assert "KNOWLEDGE GRAPH" not in fake.stream_messages[0][1]["content"]


async def test_fast_path_nessuna_latenza_kg(tmp_settings, fake_env, monkeypatch) -> None:
    """Fast path: kg_lookup non viene eseguito affatto (il quesito cita pure
    un'entità del grafo, ma il fast path resta una singola ricerca)."""
    _enable_kg(monkeypatch)
    await _seed_kg()
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Risposta [1]."],
    )
    _, search = fake_env(fake, [_hit(610, 61, 0.9)])
    async with get_session_factory()() as session:
        result = await run_agent("il Contratto 2024/15 chi l'ha firmato?", session=session)

    assert result.path == "fast"
    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    assert len(search.calls) == 1
    assert search.calls[0][1] is None  # nessun filtro KG
    assert "KNOWLEDGE GRAPH" not in fake.stream_messages[0][1]["content"]


# --- match deterministico delle entità nel quesito ------------------------------


async def test_match_entities_confini_di_parola_e_nomi_corti(tmp_settings) -> None:
    """match_entities_in_text: case-insensitive, su parola intera, mai nomi
    più corti di 4 caratteri."""
    await _seed_kg()
    async with get_session_factory()() as session:

        async def found(text: str) -> set[str]:
            matches = await kg_query.match_entities_in_text(text, session=session)
            return {m["name"] for m in matches}

        assert await found("chi è Mario Rossi?") == {"mario rossi"}
        # "alfa" NON matcha dentro "alfabetico" (confine di parola)
        assert await found("ordine alfabetico dei fascicoli") == set()
        # ma matcha come parola intera, anche con casing diverso
        assert await found("stato del progetto Alfa") == {"alfa"}
        # nomi < 4 caratteri mai considerati, anche a parola intera
        assert await found("il cde convoca il CDE") == set()
        # segni non alfanumerici ai bordi del nome non ostacolano il match
        assert await found("chi rappresenta ACME S.p.A. in causa?") == {"acme s.p.a."}
        # nomi composti e caratteri speciali nel nome
        assert await found(_QUESTION) == {"mario rossi", "contratto 2024/15"}


async def test_match_entities_cache_e_invalidazione(tmp_settings) -> None:
    """L'elenco entità è cacheato in memoria; le scritture del grafo (o
    l'invalidazione esplicita) rendono visibili le entità nuove."""
    await _seed_kg()
    async with get_session_factory()() as session:
        matches = await kg_query.match_entities_in_text(
            "chi è Mario Rossi?", session=session
        )
        assert {m["name"] for m in matches} == {"mario rossi"}

        # Entità scritta direttamente sul DB: la cache (entro il TTL) non la
        # vede ancora...
        session.add(
            Entity(name="commessa beta", display_name="Commessa Beta", type="progetto")
        )
        await session.commit()
        stale = await kg_query.match_entities_in_text(
            "stato della commessa beta?", session=session
        )
        assert stale == []

        # ...finché la cache non viene invalidata (come fanno estrattore,
        # cancellazioni e reset dopo ogni scrittura del grafo)
        kg_query.invalidate_entity_cache()
        fresh = await kg_query.match_entities_in_text(
            "stato della commessa beta?", session=session
        )
        assert {m["name"] for m in fresh} == {"commessa beta"}
