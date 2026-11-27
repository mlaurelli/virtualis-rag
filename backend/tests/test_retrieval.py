"""Test del retrieval (Fase 3): filtri, ricerca ibrida, reranking, servizio.

L'integrazione usa Qdrant embedded in tmp_path e l'Embedder reale (modelli
già in cache dalla Fase 2). Il reranker NON viene mai scaricato nei test:
si verificano il graceful degradation e il riordino con un mock.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.config import Settings
from app.db import get_session_factory, init_db
from app.ingestion.chunker import ChunkData
from app.ingestion.embeddings import Embedder
from app.ingestion.indexer import VectorIndex
from app.models import Chunk
from app.retrieval import (
    HybridSearcher,
    Reranker,
    SearchFilters,
    SearchResult,
    make_snippet,
    retrieve,
)

# --- corpus sintetico -------------------------------------------------------

_SIGLA_ID = 203
_RIMBORSO_IDS = {200, 201, 202, 204, 205, 206, 207}

_SIGLA_TEXT = (
    "La riunione del comitato tecnico ha esaminato la documentazione relativa "
    "al componente identificato con la sigla VRX-204, approvandone le specifiche "
    "costruttive e il piano di collaudo. Sono state inoltre discusse le tempistiche "
    "di consegna, le responsabilità dei fornitori, la gestione delle non conformità "
    "riscontrate durante le prove funzionali e le procedure di accettazione finale."
)

_TEXTS = [
    (200, "Le spese di trasferta sono rimborsate previa presentazione delle ricevute originali."),
    (201, "Il rimborso del biglietto ferroviario richiede l'approvazione del responsabile."),
    (202, "Le diarie giornaliere per le missioni all'estero sono stabilite da tabella allegata."),
    (203, _SIGLA_TEXT),
    (204, "Le spese alberghiere sono rimborsate fino al limite massimo previsto per categoria."),
    (205, "I pasti durante la trasferta sono coperti dalla diaria giornaliera."),
    (206, "Le richieste di rimborso incomplete sono respinte dall'ufficio amministrativo."),
    (207, "Il rimborso del parcheggio è ammesso solo in assenza di trasporto pubblico."),
]

_MODIFIED = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def searcher(tmp_path_factory):
    """HybridSearcher su Qdrant embedded con il corpus sintetico indicizzato.

    Scope modulo: l'indicizzazione (embedding reale) è fatta una sola volta.
    """
    embedder = Embedder.get()
    client = QdrantClient(path=str(tmp_path_factory.mktemp("qdrant")))
    index = VectorIndex(client=client, embedder=embedder)
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
        for i, (_, text) in enumerate(_TEXTS)
    ]
    meta = {
        "document_id": 1,
        "title": "Regolamento trasferte",
        "source_id": 1,
        "source_name": "cartella",
        "doc_type": "pdf",
        "author": "Amministrazione",
        "modified_at": _MODIFIED,
    }
    asyncio.run(
        index.upsert_chunks(
            meta,
            chunks,
            chunk_ids=[cid for cid, _ in _TEXTS],
            dense_vectors=embedder.embed_passages([t for _, t in _TEXTS]),
            sparse_vectors=embedder.embed_sparse([t for _, t in _TEXTS]),
        )
    )
    yield HybridSearcher(embedder, index)
    client.close()


# --- filtri -----------------------------------------------------------------


def test_filtri_vuoti_danno_none() -> None:
    """Nessun criterio → nessun filtro Qdrant."""
    assert SearchFilters().to_qdrant_filter() is None


def test_filtri_convertiti_in_qdrant_filter() -> None:
    """source_id/doc_type → MatchValue; date → Range su modified_at (timestamp)."""
    f = SearchFilters(
        source_id=7,
        doc_type="pdf",
        date_from=datetime(2024, 1, 1, tzinfo=UTC),
        date_to=datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
    )
    qf = f.to_qdrant_filter()
    assert isinstance(qf, qm.Filter)
    assert len(qf.must) == 3
    by_key = {cond.key: cond for cond in qf.must}
    assert by_key["source_id"].match.value == 7
    assert by_key["doc_type"].match.value == "pdf"
    assert by_key["modified_at"].range.gte == datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    assert by_key["modified_at"].range.lte == datetime(
        2024, 12, 31, 23, 59, 59, tzinfo=UTC
    ).timestamp()


def test_filtro_solo_date_from() -> None:
    """Range aperto: solo il limite inferiore è valorizzato."""
    qf = SearchFilters(date_from=datetime(2024, 6, 1, tzinfo=UTC)).to_qdrant_filter()
    assert len(qf.must) == 1
    cond = qf.must[0]
    assert cond.key == "modified_at"
    assert cond.range.gte is not None
    assert cond.range.lte is None


# --- snippet ------------------------------------------------------------------


def test_snippet_troncato_con_ellissi() -> None:
    """Oltre ~300 caratteri: taglio su parola intera + ellissi."""
    text = "parola " * 100
    snippet = make_snippet(text)
    assert len(snippet) <= 301  # 300 + ellissi
    assert snippet.endswith("…")
    assert not snippet[:-1].endswith(" ")


def test_snippet_corto_invariato() -> None:
    assert make_snippet("  testo breve. ") == "testo breve."


def test_snippet_pulisce_prefisso_sezione_e_markdown() -> None:
    """Il prefisso [Sezione: ...] e i marcatori markdown non finiscono in UI."""
    text = "[Sezione: Contratto > Garanzia]\nLa **durata** è di 24 mesi."
    assert make_snippet(text) == "La durata è di 24 mesi."


# --- ricerca ibrida (integrazione reale) --------------------------------------


async def test_ricerca_semantica_trova_il_tema_giusto(searcher) -> None:
    """Query semantica in italiano: i chunk sul tema rimborso in testa."""
    results = await searcher.search("come chiedere il rimborso delle spese di viaggio")
    assert results
    # i primi due risultati sono chunk sul tema rimborso/trasferte
    assert results[0].chunk_id in _RIMBORSO_IDS
    assert results[1].chunk_id in _RIMBORSO_IDS
    # il chunk della sigla (comitato tecnico) non è tra i più rilevanti
    top_ids = [r.chunk_id for r in results[:2]]
    assert _SIGLA_ID not in top_ids


async def test_ricerca_lessicale_trova_la_sigla(searcher) -> None:
    """Query lessicale: il chunk con 'VRX-204' è primo anche se il suo testo
    è semanticamente distante dalla forma della query."""
    results = await searcher.search("VRX-204")
    assert results[0].chunk_id == _SIGLA_ID
    # metadati del payload mappati correttamente
    r = results[0]
    assert r.document_id == 1
    assert r.title == "Regolamento trasferte"
    assert r.source_name == "cartella"
    assert r.doc_type == "pdf"
    assert r.page == 4
    assert r.section_path == ["Regolamento"]
    assert r.modified_at == datetime(2024, 3, 1, 12, 0, 0)
    assert r.bboxes == []  # chunk indicizzati senza coordinate: retrocompatibile
    assert "VRX-204" in r.snippet or len(r.snippet) <= 301


async def test_ibrida_batte_densa_sul_caso_lessicale(searcher) -> None:
    """Con un contesto semantico fuorviante la sola densa perde il chunk con
    la sigla, l'ibrida (grazie a BM25) lo tiene primo."""
    query = "modulo VRX-204 rimborso trasferta"
    hybrid = await searcher.search(query)
    hybrid_ids = [r.chunk_id for r in hybrid]

    # classifica della sola densa, stessa query
    embedder = Embedder.get()
    dense_vec = embedder.embed_queries([query])
    response = searcher._index._client.query_points(
        collection_name=searcher._index.collection,
        query=[float(v) for v in dense_vec[0]],
        using="dense",
        limit=10,
        with_payload=True,
    )
    dense_ids = [p.payload["chunk_id"] for p in response.points]

    hybrid_rank = hybrid_ids.index(_SIGLA_ID)
    dense_rank = dense_ids.index(_SIGLA_ID)
    assert hybrid_rank == 0
    assert hybrid_rank < dense_rank


async def test_filtri_applicati_alla_ricerca(searcher) -> None:
    """I filtri restringono davvero i risultati (doc_type e intervallo date)."""
    results = await searcher.search("rimborso", filters=SearchFilters(doc_type="pdf"))
    assert results
    assert all(r.doc_type == "pdf" for r in results)

    # doc_type inesistente → nessun risultato
    assert await searcher.search("rimborso", filters=SearchFilters(doc_type="md")) == []

    # finestra temporale che include modified_at → risultati
    dentro = SearchFilters(
        date_from=datetime(2024, 1, 1, tzinfo=UTC),
        date_to=datetime(2024, 12, 31, tzinfo=UTC),
    )
    assert await searcher.search("rimborso", filters=dentro)

    # finestra temporale che esclude modified_at → nessun risultato
    fuori = SearchFilters(
        date_from=datetime(2025, 1, 1, tzinfo=UTC),
        date_to=datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert await searcher.search("rimborso", filters=fuori) == []


async def test_limit_e_prefetch_k(searcher) -> None:
    """limit riduce i risultati; dense_k/sparse_k=1 tengono comunque il chunk giusto."""
    results = await searcher.search("rimborso", limit=3)
    assert len(results) == 3
    results = await searcher.search("VRX-204", limit=4, dense_k=1, sparse_k=1)
    assert results[0].chunk_id == _SIGLA_ID


# --- reranker -----------------------------------------------------------------


def _results(n: int) -> list[SearchResult]:
    return [
        SearchResult(
            chunk_id=i,
            document_id=1,
            title=f"doc {i}",
            snippet=f"testo {i}",
            context_text=f"contenuto del chunk numero {i}",
            score=1.0 / (i + 1),
            page=1,
        )
        for i in range(n)
    ]


async def test_reranker_disabilitato_restituisce_inalterati() -> None:
    """reranker_enabled=False → nessun caricamento, risultati inalterati."""
    reranker = Reranker(Settings(reranker_enabled=False))
    results = _results(4)
    out = await reranker.rerank("query", results, top_n=2)
    assert [r.chunk_id for r in out] == [0, 1, 2, 3]
    assert reranker._model is None


async def test_reranker_modello_non_disponibile_degrada_senza_errori() -> None:
    """Download/import fallito (modello inesistente) → graceful degradation:
    i risultati tornano inalterati e la ricerca non fallisce."""
    reranker = Reranker(Settings(reranker_model="org-inesistente/modello-inesistente-xyz"))
    results = _results(3)
    out = await reranker.rerank("query", results, top_n=2)
    assert [r.chunk_id for r in out] == [0, 1, 2]
    # il fallimento è memorizzato: non si ritenta il download
    assert reranker._available is False
    out2 = await reranker.rerank("query", results, top_n=2)
    assert [r.chunk_id for r in out2] == [0, 1, 2]


class _FakeCrossEncoder:
    """CrossEncoder finto: punteggi decrescenti col chunk_id (ordine inverso)."""

    def predict(self, pairs):
        # il testo è "contenuto del chunk numero {i}" → score alto per i alti
        return [float(p[1].rsplit(" ", 1)[-1]) for p in pairs]


async def test_reranker_riordina_con_mock(monkeypatch) -> None:
    """Con un cross-encoder (mockato) i risultati sono riordinati per score
    del reranker, lo score è aggiornato e si restituiscono i primi top_n."""
    monkeypatch.setattr(Reranker, "_load_sync", lambda self: _FakeCrossEncoder())
    reranker = Reranker(Settings())
    results = _results(4)  # score iniziali: 1.0, 0.5, 0.33, 0.25 (0 davanti)
    out = await reranker.rerank("query", results, top_n=2)
    assert [r.chunk_id for r in out] == [3, 2]
    assert out[0].score == 3.0
    assert out[1].score == 2.0


async def test_reranker_lista_vuota() -> None:
    reranker = Reranker(Settings(reranker_enabled=False))
    assert await reranker.rerank("query", [], top_n=5) == []


# --- servizio retrieve --------------------------------------------------------


class _StubSearcher:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results

    async def search(self, query, *, filters=None, limit=10, dense_k=20, sparse_k=20):
        return list(self._results)


class _StubReranker:
    def __init__(self) -> None:
        self.calls = []

    async def rerank(self, query, results, top_n):
        self.calls.append((query, top_n))
        return list(reversed(results))[:top_n]


async def test_retrieve_senza_rerank_restituisce_pool() -> None:
    """rerank=False → i risultati dell'ibrida passano inalterati."""
    stub = _StubSearcher(_results(4))
    out = await retrieve("q", rerank=False, searcher=stub)
    assert [r.chunk_id for r in out] == [0, 1, 2, 3]


async def test_retrieve_con_rerank_applica_top_n() -> None:
    """rerank=True → il reranker riordina e si taglia a rerank_top_n."""
    stub_reranker = _StubReranker()
    out = await retrieve(
        "q", rerank=True, rerank_top_n=2, searcher=_StubSearcher(_results(4)),
        reranker=stub_reranker,
    )
    assert stub_reranker.calls == [("q", 2)]
    assert [r.chunk_id for r in out] == [3, 2]


async def test_retrieve_scarta_chunk_orfani(tmp_settings) -> None:
    """Con sessione DB, i chunk non più presenti nel DB sono scartati."""
    await init_db()
    async with get_session_factory()() as session:
        session.add(Chunk(id=1, document_id=1, ord=0, text="esiste"))
        await session.commit()

    results = _results(2)
    results[0].chunk_id = 1
    results[1].chunk_id = 999  # orfano
    async with get_session_factory()() as session:
        out = await retrieve("q", rerank=False, searcher=_StubSearcher(results), session=session)
    assert [r.chunk_id for r in out] == [1]
