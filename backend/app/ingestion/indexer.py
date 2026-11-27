"""Indice vettoriale Qdrant: collection con vettori named densi + sparsi.

Connessione: se ``settings.qdrant_url`` è valorizzato si usa un server
Qdrant remoto/locale, altrimenti Qdrant embedded su file (``data_dir/qdrant``).
In modalità embedded gli accessi sono serializzati con un ``asyncio.Lock``
(Qdrant locale non tollera accessi concorrenti multi-processo).

La ricerca ibrida (``hybrid_query``) è implementata nella Fase 3: qui ci sono
solo ``ensure_collection`` / ``upsert_chunks`` / ``delete_document``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import structlog
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import Settings, get_settings
from .chunker import ChunkData
from .embeddings import Embedder

log = structlog.get_logger(__name__)

# Namespace fisso per gli UUID5 dei punti (id deterministici)
_POINT_NAMESPACE = uuid.UUID("6f1c2a3e-9b4d-4c5a-8e7f-0a1b2c3d4e5f")

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"


def point_id(document_id: int, ord_: int) -> str:
    """UUID5 deterministico da (document_id, ord): upsert idempotenti."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{ord_}"))


class VectorIndex:
    """Facade minima su Qdrant per l'indicizzazione dei chunk."""

    def __init__(
        self,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
        *,
        client: QdrantClient | None = None,
        dense_dim: int | None = None,
    ) -> None:
        """Crea l'indice.

        ``client`` e ``dense_dim`` sono override pensati per i test (evitano
        il caricamento dei modelli di embedding).
        """
        self._settings = settings or get_settings()
        self._embedder = embedder
        self._dense_dim_override = dense_dim
        self._lock = asyncio.Lock()
        if client is not None:
            self._client = client
        elif self._settings.qdrant_url:
            log.info("connessione a Qdrant remoto", url=self._settings.qdrant_url)
            self._client = QdrantClient(url=self._settings.qdrant_url)
        else:
            path = self._settings.data_dir / "qdrant"
            path.mkdir(parents=True, exist_ok=True)
            log.info("Qdrant embedded", path=str(path))
            self._client = QdrantClient(path=str(path))

    @property
    def collection(self) -> str:
        return self._settings.qdrant_collection

    def collection_exists(self) -> bool:
        """True se la collection configurata esiste (usato dagli healthcheck)."""
        return self._client.collection_exists(self.collection)

    def _dense_dim(self) -> int:
        if self._dense_dim_override is not None:
            return self._dense_dim_override
        embedder = self._embedder or Embedder.get()
        return embedder.dense_dim

    def ensure_collection(self) -> None:
        """Crea la collection se manca: denso (cosine) + sparso (IDF).

        Il vettore denso usa **quantizzazione scalare int8** (allegato 6.2):
        ~4x meno RAM per i vettori a fronte di una perdita di accuratezza
        trascurabile sul ranking (i rescoring usa i vettori originali);
        ``always_ram`` tiene l'indice quantizzato in memoria. Si applica alle
        collection create da ora in poi: quelle esistenti restano com'è
        (la ricerca funziona in entrambi i casi).
        """
        if self._client.collection_exists(self.collection):
            return
        dim = self._dense_dim()
        log.info("creazione collection Qdrant", collection=self.collection, dense_dim=dim)
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config={
                DENSE_VECTOR_NAME: qm.VectorParams(
                    size=dim,
                    distance=qm.Distance.COSINE,
                    quantization_config=qm.ScalarQuantization(
                        scalar=qm.ScalarQuantizationConfig(
                            type=qm.ScalarType.INT8,
                            quantile=0.99,
                            always_ram=True,
                        )
                    ),
                )
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: qm.SparseVectorParams(modifier=qm.Modifier.IDF)
            },
        )

    async def upsert_chunks(
        self,
        document_meta: dict[str, Any],
        chunks: list[ChunkData],
        *,
        chunk_ids: list[int],
        dense_vectors: Any,
        sparse_vectors: list[dict[str, list]],
    ) -> None:
        """Inserisce/aggiorna i punti di un documento.

        ``document_meta`` contiene i metadati del documento (title, source_id,
        source_name, doc_type, author, modified_at). ``chunk_ids`` sono gli id
        delle righe ``chunks`` nel DB, nello stesso ordine di ``chunks``.
        """
        async with self._lock:
            await asyncio.to_thread(self.ensure_collection)
        modified_at = document_meta.get("modified_at")
        ts = (
            int(modified_at.timestamp())
            if isinstance(modified_at, datetime)
            else None
        )
        points = []
        for chunk, chunk_id, dense, sparse in zip(
            chunks, chunk_ids, dense_vectors, sparse_vectors, strict=True
        ):
            payload = {
                "document_id": document_meta["document_id"],
                "chunk_id": chunk_id,
                "ord": chunk.ord,
                "text": chunk.text,
                "context_text": chunk.context_text,
                "section_path": chunk.section_path,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "bboxes": chunk.bboxes,
                "title": document_meta.get("title", ""),
                "source_id": document_meta.get("source_id"),
                "source_name": document_meta.get("source_name", ""),
                "doc_type": document_meta.get("doc_type", ""),
                "author": document_meta.get("author", ""),
                "modified_at": ts,
            }
            points.append(
                qm.PointStruct(
                    id=point_id(document_meta["document_id"], chunk.ord),
                    vector={
                        DENSE_VECTOR_NAME: [float(v) for v in dense],
                        SPARSE_VECTOR_NAME: qm.SparseVector(
                            indices=sparse["indices"], values=sparse["values"]
                        ),
                    },
                    payload=payload,
                )
            )
        if not points:
            return
        # Client sincrono: eseguito in thread per non bloccare l'event loop;
        # il lock serializza le scritture (l'embedded non è thread-safe)
        async with self._lock:
            await asyncio.to_thread(
                self._client.upsert, collection_name=self.collection, points=points
            )
        log.info(
            "chunk indicizzati",
            document_id=document_meta["document_id"],
            count=len(points),
        )

    async def delete_documents(self, document_ids: list[int]) -> None:
        """Elimina i punti di PIÙ documenti in una sola chiamata (MatchAny).

        Usata dal reset del materiale: con migliaia di documenti una delete
        per documento moltiplicherebbe inutilmente i round-trip sull'indice.
        """
        if not document_ids:
            return
        async with self._lock:
            exists = await asyncio.to_thread(
                self._client.collection_exists, self.collection
            )
            if not exists:
                return
            await asyncio.to_thread(
                self._client.delete,
                collection_name=self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="document_id",
                                match=qm.MatchAny(any=list(document_ids)),
                            )
                        ]
                    )
                ),
            )
        log.info("punti eliminati in batch", documents=len(document_ids))

    async def delete_document(self, document_id: int) -> None:
        """Elimina tutti i punti di un documento (filtro sul payload)."""
        async with self._lock:
            exists = await asyncio.to_thread(
                self._client.collection_exists, self.collection
            )
            if not exists:
                return
            await asyncio.to_thread(
                self._client.delete,
                collection_name=self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="document_id",
                                match=qm.MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
            )
        log.info("punti eliminati", document_id=document_id)
