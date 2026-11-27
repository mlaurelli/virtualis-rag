"""Servizio di retrieval ad alto livello: ricerca ibrida + reranking.

Usato dall'agente (Fase 4) e dalla route ``/api/search`` (Fase 5).

Il :class:`~app.ingestion.indexer.VectorIndex` è quello condiviso con la
pipeline di ingestion: in modalità embedded il path Qdrant è bloccato dal
primo client che lo apre, quindi NON va mai creato un secondo client sullo
stesso path nello stesso processo.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingestion.embeddings import Embedder
from ..ingestion.pipeline import _get_index
from ..models import Chunk
from .hybrid import HybridSearcher, SearchFilters, SearchResult
from .rerank import Reranker

log = structlog.get_logger(__name__)

_searcher: HybridSearcher | None = None


def _get_searcher() -> HybridSearcher:
    """HybridSearcher condiviso (lazy), sull'indice della pipeline."""
    global _searcher
    if _searcher is None:
        _searcher = HybridSearcher(Embedder.get(), _get_index())
    return _searcher


def reset_state() -> None:
    """Azzera il searcher condiviso (usato dai test)."""
    global _searcher
    _searcher = None


async def retrieve(
    query: str,
    *,
    filters: SearchFilters | None = None,
    limit: int = 10,
    rerank: bool = True,
    rerank_top_n: int = 5,
    session: AsyncSession | None = None,
    searcher: HybridSearcher | None = None,
    reranker: Reranker | None = None,
) -> list[SearchResult]:
    """Ricerca ibrida seguita (opzionalmente) da reranking.

    - ``limit``: ampiezza del pool restituito dalla ricerca ibrida.
    - ``rerank_top_n``: numero di risultati finali quando ``rerank`` è attivo.
    - ``session``: se fornita, scarta i chunk non più presenti nel DB
      (deriva indice/DB, es. documenti eliminati).
    - ``searcher``/``reranker``: override per i test.
    """
    searcher = searcher or _get_searcher()
    results = await searcher.search(query, filters=filters, limit=limit)

    if session is not None and results:
        chunk_ids = [r.chunk_id for r in results]
        existing = set(
            (await session.execute(select(Chunk.id).where(Chunk.id.in_(chunk_ids))))
            .scalars()
            .all()
        )
        dropped = len(results) - len(existing)
        results = [r for r in results if r.chunk_id in existing]
        if dropped:
            log.warning("chunk orfani scartati dai risultati", count=dropped)

    if rerank:
        reranker = reranker or Reranker.get()
        results = await reranker.rerank(query, results, rerank_top_n)
        return results[:rerank_top_n]
    return results
