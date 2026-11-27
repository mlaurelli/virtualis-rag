"""Test dell'agente (Fase 4): citazioni, grafo LangGraph, strumenti, server MCP.

Il KimiClient è sempre MOCKATO (nessuna rete): un fake risponde in base al
marcatore presente nel system prompt ("classificatore", "pianificatore",
"valutatore", "verificatore") e produce token di sintesi prefissati. La
ricerca ibrida è sostituita da un fake nei test del grafo; i test degli
strumenti usano invece Qdrant embedded + Embedder reale (come test_retrieval).
"""

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest
from qdrant_client import QdrantClient

from app.agent import (
    AgentResult,
    build_citation_map,
    parse_citations,
    run_agent,
    strip_or_report_uncited,
)
from app.agent import graph as agent_graph
from app.agent import tools as agent_tools
from app.agent.tools import (
    hybrid_search,
    list_documents,
    read_document,
    serialize_result,
)
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.ingestion.chunker import ChunkData
from app.ingestion.embeddings import Embedder
from app.ingestion.indexer import VectorIndex
from app.llm import LLMNotConfiguredError
from app.models import Chunk, Document, Source
from app.retrieval import service as retrieval_service
from app.retrieval.hybrid import HybridSearcher, SearchResult
from app.retrieval.rerank import Reranker

# --- citazioni ----------------------------------------------------------------


def _result(chunk_id: int, score: float, document_id: int = 1) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        title=f"doc {document_id}",
        snippet=f"snippet {chunk_id}",
        context_text=f"contenuto {chunk_id}",
        score=score,
        page=chunk_id,
        section_path=["Sezione"],
        source_name="cartella",
        doc_type="pdf",
    )


def test_build_citation_map_ordina_per_score_e_dedup() -> None:
    """Numerazione [1..N] per score decrescente; chunk duplicati contati una
    volta (tenuto lo score migliore)."""
    results = [
        _result(10, 0.3),
        _result(20, 0.9),
        _result(10, 0.8),  # duplicato di 10 con score migliore
        _result(30, 0.5),
    ]
    cmap = build_citation_map(results)
    assert [cmap[n].chunk_id for n in (1, 2, 3)] == [20, 10, 30]
    assert len(cmap) == 3
    assert cmap[2].score == 0.8


def test_parse_citations_risolve_e_segnala_non_risolvibili() -> None:
    """Marcatori in ordine di prima comparsa, deduplicati; quelli senza
    passaggio finiscono in invalid_markers."""
    cmap = build_citation_map([_result(10, 0.9), _result(20, 0.5)])
    parsed = parse_citations("Fatto A [2]. Fatto B [1]. Ancora [2] e [9].", cmap)
    assert [c.n for c in parsed.citations] == [2, 1]
    assert parsed.invalid_markers == [9]
    cit = parsed.citations[0]
    assert cit.document_id == 1
    assert cit.snippet == "snippet 20"
    assert cit.source_name == "cartella"
    assert cit.page == 20
    assert cit.section_path == ["Sezione"]


def test_parse_citations_senza_marcatori() -> None:
    parsed = parse_citations("Testo senza citazioni.", build_citation_map([_result(1, 1.0)]))
    assert parsed.citations == []
    assert parsed.invalid_markers == []


def test_parse_citations_propaga_bboxes() -> None:
    """I bbox del SearchResult arrivano nella Citation; default vuoto."""
    with_bbox = _result(1, 1.0)
    with_bbox.bboxes = [{"page": 1, "l": 10.0, "t": 700.0, "r": 300.0, "b": 680.0}]
    parsed = parse_citations("Fatto [1].", build_citation_map([with_bbox]))
    assert parsed.citations[0].bboxes == [
        {"page": 1, "l": 10.0, "t": 700.0, "r": 300.0, "b": 680.0}
    ]
    parsed = parse_citations("Fatto [1].", build_citation_map([_result(2, 1.0)]))
    assert parsed.citations[0].bboxes == []


def test_serialize_result_roundtrip_bboxes() -> None:
    """serialize_result → _to_search_result: i bbox sopravvivono allo stato."""
    result = _result(1, 1.0)
    result.bboxes = [{"page": 1, "l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0}]
    data = serialize_result(result)
    assert data["bboxes"] == result.bboxes
    assert agent_graph._to_search_result(data).bboxes == result.bboxes
    # risultati senza bbox (indice pre-esistente): lista vuota
    assert agent_graph._to_search_result(serialize_result(_result(2, 1.0))).bboxes == []


def test_strip_or_report_uncited_warn_su_risposta_articolata() -> None:
    cmap = build_citation_map([_result(1, 1.0)])
    lunga = (
        "La procedura prevede diversi passaggi articolati e dettagliati. "
        "Ogni passaggio va eseguito con cura e documentato per esteso."
    )
    assert strip_or_report_uncited(lunga, cmap)  # warning euristico
    assert strip_or_report_uncited(lunga + " [1]", cmap) == []  # citata
    assert strip_or_report_uncited("Risposta secca.", cmap) == []  # banale


# --- fake LLM e fake search ----------------------------------------------------

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _PLAN, _EVALUATE, _VERIFY = (
    "classificatore",
    "pianificatore",
    "valutatore",
    "verificatore",
)


class _FakeKimi:
    """KimiClient finto: risposte JSON in coda per tipo di prompt + token di
    sintesi prefissati. Registra le chiamate per le asserzioni."""

    def __init__(
        self,
        responses: dict[str, list[str]],
        stream_tokens: list[str],
    ) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_tokens = stream_tokens
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
        for token in self._stream_tokens:
            yield token


class _FakeSearch:
    """Sostituto di tools.hybrid_search: stesse hit per ogni query, tiene
    traccia delle interrogazioni eseguite."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
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
def fake_env(monkeypatch):
    """Monta il KimiClient e la hybrid_search finti nel grafo; restituisce
    una factory per configurarli test per test."""

    def install(
        fake_kimi: _FakeKimi, hits: list[dict[str, Any]]
    ) -> tuple[_FakeKimi, _FakeSearch]:
        fake_search = _FakeSearch(hits)
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)
        monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)
        return fake_kimi, fake_search

    return install


# --- grafo ---------------------------------------------------------------------


async def test_grafo_percorso_fast_una_sola_ricerca(fake_env) -> None:
    """Percorso fast: classifica → singola ricerca → sintesi con citazioni."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "quesito puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Le spese di trasferta ", "sono rimborsate [1]."],
    )
    _, search = fake_env(
        fake, [_hit(10, 7, 0.9), _hit(11, 8, 0.4)]
    )
    tokens: list[str] = []
    result = await run_agent(
        "come sono rimborsate le spese di trasferta?",
        session=None,
        token_sink=tokens.append,
    )

    assert isinstance(result, AgentResult)
    assert result.path == "fast"
    # una sola ricerca, con il quesito originale
    assert search.queries == ["come sono rimborsate le spese di trasferta?"]
    assert result.queries_run == search.queries
    # token ricevuti in ordine e risposta completa nello stato
    assert tokens == ["Le spese di trasferta ", "sono rimborsate [1]."]
    assert result.answer == "Le spese di trasferta sono rimborsate [1]."
    # citazione [1] risolta nei metadati del passaggio migliore
    assert len(result.citations) == 1
    cit = result.citations[0]
    assert cit.n == 1
    assert cit.document_id == 7
    assert cit.title == "Documento 7"
    assert cit.page == 3
    assert cit.section_path == ["Capitolo 1"]
    assert cit.source_name == "Archivio locale"
    assert result.warnings == []
    assert result.retrieved_chunk_ids == [10, 11]
    # sequenza dei nodi tracciata
    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    assert all("ts" in s for s in result.steps)
    assert result.latency_ms >= 0
    # niente pianificazione/valutazione nel percorso fast
    assert _PLAN not in fake.calls
    assert _EVALUATE not in fake.calls


async def test_grafo_agentic_loop_e_max_iterazioni(fake_env) -> None:
    """Percorso agentic: plan → retrieve → evaluate insufficient →
    riformulazioni eseguite, loop fermato a max_agent_iterations (3)."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "confronto tra documenti"}'],
            _PLAN: [
                '{"sub_questions": ['
                '{"query": "rimborso trasferte regolamento 2023", "purpose": "versione 2023"}, '
                '{"query": "rimborso trasferte regolamento 2024", "purpose": "versione 2024"}'
                "]}"
            ],
            _EVALUATE: [
                '{"sufficient": false, "missing": "manca la diaria", '
                '"refined_queries": ["diaria giornaliera missioni estero"]}',
                '{"sufficient": false, "missing": "mancano i limiti", '
                '"refined_queries": ["limiti rimborso albergo categoria"]}',
                '{"sufficient": false, "missing": "ancora lacune", '
                '"refined_queries": ["query mai eseguita"]}',
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Confronto ", "tra versioni [1]."],
    )
    _, search = fake_env(fake, [_hit(20, 7, 0.9)])
    queue: asyncio.Queue[str] = asyncio.Queue()
    result = await run_agent("confronta i rimborsi 2023 e 2024", session=None, token_sink=queue)

    assert result.path == "agentic"
    # sotto-quesiti + prime due riformulazioni; la terza NON è eseguita
    # perché iterations ha raggiunto max_agent_iterations (3)
    assert result.queries_run == [
        "rimborso trasferte regolamento 2023",
        "rimborso trasferte regolamento 2024",
        "diaria giornaliera missioni estero",
        "limiti rimborso albergo categoria",
    ]
    assert search.queries == result.queries_run
    # evaluate chiamato esattamente max_agent_iterations volte
    assert fake.calls.count(_EVALUATE) == 3
    eval_steps = [s for s in result.steps if s["step"] == "evaluate"]
    assert len(eval_steps) == 3
    assert "iterazione 3" in eval_steps[-1]["detail"]
    # token consegnati alla Queue in ordine
    drained: list[str] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert "".join(drained) == result.answer == "Confronto tra versioni [1]."
    # sequenza completa dei nodi
    assert [s["step"] for s in result.steps] == [
        "classify",
        "plan",
        "retrieve",
        "evaluate",
        "retrieve",
        "evaluate",
        "retrieve",
        "evaluate",
        "synthesize",
        "verify",
    ]


async def test_grafo_verify_segnala_affermazioni_non_supportate(fake_env) -> None:
    """Verify con supported=false → warning con le affermazioni contestate."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: [
                '{"supported": false, "unsupported_claims": ["scadenza 30 giorni"]}'
            ],
        },
        stream_tokens=["Il reso va richiesto entro 30 giorni [1]."],
    )
    fake_env(fake, [_hit(30, 7, 0.9)])
    result = await run_agent("termini per il reso?", session=None)
    assert any("30 giorni" in w for w in result.warnings)
    assert result.steps[-1]["step"] == "verify"
    assert "non supportate" in result.steps[-1]["detail"]


async def test_grafo_llm_non_configurato_risposta_di_cortesia(monkeypatch) -> None:
    """LLMNotConfiguredError → risposta di cortesia + warning, mai traceback."""

    def _raise():
        raise LLMNotConfiguredError()

    monkeypatch.setattr(agent_graph, "get_kimi_client", _raise)
    tokens: list[str] = []
    result = await run_agent("qualunque quesito", session=None, token_sink=tokens.append)
    assert "MOONSHOT_API_KEY" in result.answer
    assert result.warnings
    assert result.path == ""
    assert result.queries_run == []
    assert tokens == [result.answer]  # cortesia emessa anche sullo stream


async def test_grafo_nessun_passaggio_risposta_onesta(fake_env) -> None:
    """Ricerche a vuoto → la sintesi non inventa: dichiara l'assenza di fonti."""
    fake = _FakeKimi(
        responses={_CLASSIFY: ['{"path": "fast", "reason": "puntuale"}']},
        stream_tokens=[],
    )
    fake_env(fake, hits=[])
    result = await run_agent("quesito senza fonti", session=None)
    assert "Non ho trovato passaggi pertinenti" in result.answer
    assert "nessun passaggio recuperato" in result.warnings[0]
    assert result.citations == []
    # verify non consulta il modello senza citazioni
    assert _VERIFY not in fake.calls
    assert result.steps[-1]["step"] == "verify"


# --- strumenti su db + Qdrant embedded ------------------------------------------

_TOOL_CHUNKS = [
    (300, "Le spese di trasferta sono rimborsate previa presentazione delle ricevute originali."),
    (301, "Il rimborso del biglietto ferroviario richiede l'approvazione del responsabile."),
]


@pytest.fixture(scope="module")
def embedded_searcher(tmp_path_factory):
    """HybridSearcher reale su Qdrant embedded con due chunk (come test_retrieval)."""
    embedder = Embedder.get()
    client = QdrantClient(path=str(tmp_path_factory.mktemp("qdrant-agent")))
    index = VectorIndex(client=client, embedder=embedder)
    meta = {
        "document_id": 50,
        "title": "Regolamento trasferte",
        "source_id": 5,
        "source_name": "cartella",
        "doc_type": "pdf",
        "author": "Amministrazione",
        "modified_at": datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
    }
    chunks = [
        ChunkData(
            ord=i,
            text=text,
            context_text=text,
            section_path=["Regolamento"],
            page_start=i + 1,
            page_end=i + 1,
            token_count=10,
        )
        for i, (_, text) in enumerate(_TOOL_CHUNKS)
    ]
    asyncio.run(
        index.upsert_chunks(
            meta,
            chunks,
            chunk_ids=[cid for cid, _ in _TOOL_CHUNKS],
            dense_vectors=embedder.embed_passages([t for _, t in _TOOL_CHUNKS]),
            sparse_vectors=embedder.embed_sparse([t for _, t in _TOOL_CHUNKS]),
        )
    )
    yield HybridSearcher(embedder, index)
    client.close()


async def _seed_docs() -> None:
    """Popola il DB di test con sorgente, documenti e chunk coerenti con l'indice."""
    await init_db()
    async with get_session_factory()() as session:
        session.add(Source(id=5, name="cartella", type="local_fs"))
        session.add(
            Document(
                id=50,
                source_id=5,
                external_id="regolamento.pdf",
                title="Regolamento trasferte",
                doc_type="pdf",
                status="indexed",
                page_count=4,
                modified_at=datetime(2024, 3, 1, 12, 0, 0),
            )
        )
        session.add(
            Document(
                id=51,
                source_id=5,
                external_id="nota.md",
                title="Nota operativa",
                doc_type="md",
                status="indexed",
                modified_at=datetime(2024, 5, 1, 9, 0, 0),
            )
        )
        session.add_all(
            Chunk(id=cid, document_id=50, ord=i, text=text, context_text=text)
            for i, (cid, text) in enumerate(_TOOL_CHUNKS)
        )
        session.add(Chunk(id=302, document_id=51, ord=0, text="nota", context_text="nota"))
        await session.commit()


async def test_tool_hybrid_search_integrazione(
    tmp_settings, embedded_searcher, monkeypatch
) -> None:
    """hybrid_search su indice embedded reale: risultati serializzati con
    metadati e chunk orfani scartati dalla sessione DB."""
    monkeypatch.setattr(retrieval_service, "_searcher", embedded_searcher)
    # Il reranker NON viene caricato nei test: si monta un fake passthrough
    # (hybrid_search lo attiva di default, vedi agent_rerank_enabled)
    monkeypatch.setattr(Reranker, "get", classmethod(lambda cls: _PassthroughReranker()))
    await _seed_docs()
    async with get_session_factory()() as session:
        hits = await hybrid_search("rimborso spese di trasferta", session=session)
    assert hits
    assert {h["chunk_id"] for h in hits} <= {300, 301}
    top = hits[0]
    assert top["title"] == "Regolamento trasferte"
    assert top["source_name"] == "cartella"
    assert top["doc_type"] == "pdf"
    assert top["modified_at"] == "2024-03-01T12:00:00"
    assert "rimbors" in top["context_text"]


class _PassthroughReranker:
    """Reranker finto: restituisce i risultati inalterati (no download)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def rerank(self, query, results, top_n):
        self.calls.append((query, top_n))
        return list(results)[:top_n]


async def test_hybrid_search_rispetta_agent_rerank_enabled(
    tmp_settings, monkeypatch
) -> None:
    """Il flag ``rerank`` passato a retrieve segue AGENT_RERANK_ENABLED."""
    captured: list[dict[str, Any]] = []

    async def fake_retrieve(query, **kwargs):
        captured.append(kwargs)
        return []

    monkeypatch.setattr(agent_tools, "retrieve", fake_retrieve)

    # default: reranker attivo nel ciclo agentico
    await hybrid_search("quesito", session=None)
    assert captured[-1]["rerank"] is True

    monkeypatch.setenv("AGENT_RERANK_ENABLED", "false")
    get_settings.cache_clear()
    await hybrid_search("quesito", session=None)
    assert captured[-1]["rerank"] is False


async def test_hybrid_search_usa_il_reranker_quando_abilitato(
    tmp_settings, monkeypatch
) -> None:
    """Con la setting attiva il reranker (fake) è invocato sull'intero pool;
    disattivandola il reranker non viene mai chiamato."""
    results = [_result(1, 0.9), _result(2, 0.8)]

    async def fake_search(self, query, filters=None, limit=10):
        return list(results)

    searcher = type("_StubSearcher", (), {"search": fake_search})()
    fake_reranker = _PassthroughReranker()
    monkeypatch.setattr(retrieval_service, "_searcher", searcher)
    monkeypatch.setattr(Reranker, "get", classmethod(lambda cls: fake_reranker))

    hits = await hybrid_search("quesito", limit=2, session=None)
    assert fake_reranker.calls == [("quesito", 2)]
    assert [h["chunk_id"] for h in hits] == [1, 2]

    monkeypatch.setenv("AGENT_RERANK_ENABLED", "false")
    get_settings.cache_clear()
    hits = await hybrid_search("quesito", limit=2, session=None)
    assert len(fake_reranker.calls) == 1  # nessuna nuova chiamata
    assert [h["chunk_id"] for h in hits] == [1, 2]


async def test_tool_read_document(tmp_settings) -> None:
    """read_document: metadati + context_text concatenati in ordine ordinale."""
    await _seed_docs()
    async with get_session_factory()() as session:
        doc = await read_document(50, session=session)
    assert doc["title"] == "Regolamento trasferte"
    assert doc["source_name"] == "cartella"
    assert doc["chunk_count"] == 2
    # ordine dei chunk rispettato nella concatenazione
    first, second = (text for _, text in _TOOL_CHUNKS)
    assert doc["content"] == f"{first}\n\n{second}"

    async with get_session_factory()() as session:
        with pytest.raises(ValueError, match="non trovato"):
            await read_document(999, session=session)


async def test_tool_list_documents(tmp_settings) -> None:
    """list_documents: ordinamento per modified_at desc e filtri."""
    await _seed_docs()
    async with get_session_factory()() as session:
        tutti = await list_documents(session=session)
    assert [d["document_id"] for d in tutti] == [51, 50]  # nota più recente prima
    assert tutti[1]["source_name"] == "cartella"

    async with get_session_factory()() as session:
        pdf = await list_documents(session=session, doc_type="pdf")
        nessuno = await list_documents(session=session, source_id=99)
    assert [d["document_id"] for d in pdf] == [50]
    assert nessuno == []


# --- server MCP -----------------------------------------------------------------


async def test_mcp_server_registra_i_tre_tool() -> None:
    """Import senza side-effect pesanti e tool registrati con descrizioni."""
    from app import mcp_server

    tools = await mcp_server.mcp.list_tools()
    by_name = {t.name: t for t in tools}
    assert {"ricerca_documenti", "leggi_documento", "elenca_documenti"} <= set(by_name)
    assert all(by_name[name].description for name in by_name)
