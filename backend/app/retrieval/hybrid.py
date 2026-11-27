"""Ricerca ibrida (denso + sparso) con fusione RRF nativa di Qdrant.

qdrant-client 1.18 supporta ``query_points`` con ``Prefetch`` multipli e
``FusionQuery(fusion=Fusion.RRF)`` anche in modalità embedded: la fusione
Reciprocal Rank Fusion è quindi delegata a Qdrant (nessuna implementazione
Python locale necessaria).

La query viene eseguita sotto lo stesso ``asyncio.Lock`` del
:class:`~app.ingestion.indexer.VectorIndex` (in modalità embedded Qdrant non
tollera accessi concorrenti) e in ``asyncio.to_thread``: il client è sincrono
e non deve bloccare l'event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from qdrant_client.http import models as qm

from ..ingestion.embeddings import Embedder
from ..ingestion.indexer import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME, VectorIndex

log = structlog.get_logger(__name__)

SNIPPET_MAX_LEN = 300


@dataclass
class SearchFilters:
    """Filtri opzionali sulla ricerca (applicati ai metadati del payload)."""

    source_id: int | None = None
    doc_type: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    # Vincolo a un insieme chiuso di documenti (es. quelli collegati dal
    # knowledge graph nel ciclo agentico): match su QUALUNQUE id elencato
    document_ids: list[int] | None = None

    def to_qdrant_filter(self) -> qm.Filter | None:
        """Converte i filtri in un ``qm.Filter`` (None se non ci sono criteri).

        Le date sono confrontate con il campo ``modified_at`` del payload,
        memorizzato come timestamp Unix (secondi).
        """
        # list[Any]: il tipo atteso da qm.Filter è un'unione ampia e list è invariante
        must: list[Any] = []
        if self.source_id is not None:
            must.append(
                qm.FieldCondition(
                    key="source_id", match=qm.MatchValue(value=self.source_id)
                )
            )
        if self.doc_type:
            must.append(
                qm.FieldCondition(
                    key="doc_type", match=qm.MatchValue(value=self.doc_type)
                )
            )
        if self.document_ids:
            must.append(
                qm.FieldCondition(
                    key="document_id",
                    match=qm.MatchAny(any=list(self.document_ids)),
                )
            )
        if self.date_from is not None or self.date_to is not None:
            must.append(
                qm.FieldCondition(
                    key="modified_at",
                    range=qm.Range(
                        gte=(
                            self.date_from.timestamp()
                            if self.date_from is not None
                            else None
                        ),
                        lte=(
                            self.date_to.timestamp()
                            if self.date_to is not None
                            else None
                        ),
                    ),
                )
            )
        return qm.Filter(must=must) if must else None


@dataclass
class SearchResult:
    """Un chunk rilevante restituito dalla ricerca."""

    chunk_id: int
    document_id: int
    title: str
    snippet: str
    context_text: str
    score: float
    page: int | None
    section_path: list[str] = field(default_factory=list)
    source_name: str = ""
    doc_type: str = ""
    modified_at: datetime | None = None
    # Rettangoli di layout del chunk (vuoti per documenti senza coordinate)
    bboxes: list[dict[str, Any]] = field(default_factory=list)
    # Testo grezzo del chunk (usato dal reranker; non finisce nei prompt)
    text: str = ""
    # True se ``score`` è del cross-encoder di reranking (scala sigmoid
    # [0,1]); False per gli score RRF grezzi (~0.01-0.03): le soglie di
    # pertinenza dell'agente si applicano solo agli score rerankati
    reranked: bool = False


def make_snippet(text: str, max_len: int = SNIPPET_MAX_LEN) -> str:
    """Primi ``max_len`` caratteri del testo, con taglio su parola intera.

    Il testo è ripulito per la visualizzazione: il prefisso di sezione
    ``[Sezione: ...]`` (presente nei chunk a beneficio dell'embedding) e i
    marcatori markdown letterali non devono comparire negli snippet mostrati
    all'utente; la gerarchia è già esposta dal campo ``section_path``.
    """
    snippet = text.strip()
    if snippet.startswith("[Sezione:"):
        end = snippet.find("]")
        if end != -1:
            snippet = snippet[end + 1 :].lstrip()
    snippet = snippet.replace("**", "").replace("__", "")
    if len(snippet) <= max_len:
        return snippet
    cut = snippet[:max_len]
    last_space = cut.rfind(" ")
    if last_space > max_len // 2:
        cut = cut[:last_space]
    return cut.rstrip(".,;:") + "…"


class HybridSearcher:
    """Ricerca ibrida denso+sparso con fusione RRF (nativa Qdrant)."""

    def __init__(self, embedder: Embedder, index: VectorIndex) -> None:
        self._embedder = embedder
        self._index = index

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
        dense_k: int = 20,
        sparse_k: int = 20,
    ) -> list[SearchResult]:
        """Cerca i chunk più rilevanti per ``query``.

        Esegue due prefetch (denso e sparso, ciascuno con i propri top-k)
        fusi da Qdrant con Reciprocal Rank Fusion. I filtri sono applicati
        sia ai prefetch sia alla query finale.
        """
        # ensure_collection può creare la collection (scrittura): stesso lock
        # dell'indice, e in thread perché il client Qdrant è sincrono
        async with self._index._lock:
            await asyncio.to_thread(self._index.ensure_collection)
        qdrant_filter = filters.to_qdrant_filter() if filters else None

        # Embedding bloccanti: eseguiti in thread per non fermare il loop
        dense, sparse = await asyncio.gather(
            asyncio.to_thread(self._embedder.embed_queries, [query]),
            asyncio.to_thread(self._embedder.embed_sparse, [query]),
        )

        async with self._index._lock:
            response = await asyncio.to_thread(
                self._index._client.query_points,
                collection_name=self._index.collection,
                prefetch=[
                    qm.Prefetch(
                        query=[float(v) for v in dense[0]],
                        using=DENSE_VECTOR_NAME,
                        limit=dense_k,
                        filter=qdrant_filter,
                    ),
                    qm.Prefetch(
                        query=qm.SparseVector(
                            indices=sparse[0]["indices"],
                            values=sparse[0]["values"],
                        ),
                        using=SPARSE_VECTOR_NAME,
                        limit=sparse_k,
                        filter=qdrant_filter,
                    ),
                ],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                query_filter=qdrant_filter,
                limit=limit,
                with_payload=True,
            )
        results = [self._to_result(point) for point in response.points]
        log.info(
            "ricerca ibrida completata",
            query=query[:80],
            risultati=len(results),
            filtri=qdrant_filter is not None,
        )
        return results

    @staticmethod
    def _to_result(point: qm.ScoredPoint) -> SearchResult:
        """Converte uno ``ScoredPoint`` Qdrant in :class:`SearchResult`."""
        payload = point.payload or {}
        ts = payload.get("modified_at")
        return SearchResult(
            chunk_id=int(payload.get("chunk_id") or 0),
            document_id=int(payload.get("document_id") or 0),
            title=payload.get("title") or "",
            snippet=make_snippet(payload.get("text") or ""),
            text=payload.get("text") or "",
            context_text=payload.get("context_text") or "",
            score=float(point.score),
            page=payload.get("page_start"),
            section_path=list(payload.get("section_path") or []),
            source_name=payload.get("source_name") or "",
            doc_type=payload.get("doc_type") or "",
            bboxes=list(payload.get("bboxes") or []),
            modified_at=(
                datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)
                if ts is not None
                else None
            ),
        )
