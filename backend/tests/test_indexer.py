"""Test del VectorIndex su Qdrant embedded (tmp_path, senza modelli reali).

``dense_dim`` è passato come override: il caricamento del modello di
embedding non serve per questi test.
"""

from datetime import UTC

import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.ingestion.chunker import ChunkData
from app.ingestion.indexer import DENSE_VECTOR_NAME, VectorIndex, point_id


@pytest.fixture
def index(tmp_settings, tmp_path):
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    yield VectorIndex(
        settings=tmp_settings, client=client, dense_dim=4
    )
    client.close()


def _chunk(ord_: int, text: str) -> ChunkData:
    return ChunkData(
        ord=ord_,
        text=text,
        context_text=text,
        section_path=["Sezione"],
        page_start=1,
        page_end=1,
        token_count=5,
    )


def _vectors(chunks):
    dense = np.ones((len(chunks), 4), dtype=np.float32)
    sparse = [{"indices": [0, 2], "values": [0.7, 0.3]} for _ in chunks]
    return dense, sparse


def _meta(document_id: int) -> dict:
    from datetime import datetime

    return {
        "document_id": document_id,
        "title": "Doc di test",
        "source_id": 1,
        "source_name": "cartella",
        "doc_type": "md",
        "author": "Autore",
        "modified_at": datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    }


def test_ensure_collection_crea_named_vectors(index) -> None:
    """La collection ha un vettore denso (cosine) e uno sparso (IDF)."""
    index.ensure_collection()
    # idempotente
    index.ensure_collection()
    info = index._client.get_collection(index.collection)
    dense = info.config.params.vectors[DENSE_VECTOR_NAME]
    assert dense.size == 4
    assert dense.distance == qm.Distance.COSINE
    assert "sparse" in info.config.params.sparse_vectors
    assert info.config.params.sparse_vectors["sparse"].modifier == qm.Modifier.IDF


def test_ensure_collection_quantizzazione_scalare(index) -> None:
    """Il vettore denso ha quantizzazione scalare int8 (contenimento memoria,
    allegato 6.2) con i vettori quantizzati sempre in RAM."""
    index.ensure_collection()
    info = index._client.get_collection(index.collection)
    quantization = info.config.params.vectors[DENSE_VECTOR_NAME].quantization_config
    assert quantization is not None
    assert quantization.scalar.type == qm.ScalarType.INT8
    assert quantization.scalar.always_ram is True


async def test_upsert_e_payload(index) -> None:
    """Upsert di chunk con payload completo di metadati."""
    index.ensure_collection()
    chunks = [_chunk(0, "primo chunk"), _chunk(1, "secondo chunk")]
    dense, sparse = _vectors(chunks)
    await index.upsert_chunks(
        _meta(10), chunks, chunk_ids=[101, 102],
        dense_vectors=dense, sparse_vectors=sparse,
    )
    points, _ = index._client.scroll(
        collection_name=index.collection, with_payload=True, limit=10
    )
    assert len(points) == 2
    payload = next(p.payload for p in points if p.payload["ord"] == 0)
    assert payload["document_id"] == 10
    assert payload["chunk_id"] == 101
    assert payload["text"] == "primo chunk"
    assert payload["context_text"] == "primo chunk"
    assert payload["section_path"] == ["Sezione"]
    assert payload["page_start"] == 1
    assert payload["title"] == "Doc di test"
    assert payload["source_id"] == 1
    assert payload["source_name"] == "cartella"
    assert payload["doc_type"] == "md"
    assert payload["author"] == "Autore"
    assert payload["modified_at"] == 1704110400  # 2024-01-01T12:00:00 UTC
    assert payload["bboxes"] == []  # chunk senza coordinate: retrocompatibile


async def test_upsert_payload_bboxes(index) -> None:
    """I bbox del chunk finiscono nel payload Qdrant."""
    index.ensure_collection()
    bbox = {"page": 1, "l": 10.0, "t": 700.0, "r": 300.0, "b": 680.0}
    chunk = _chunk(0, "chunk con bbox")
    chunk.bboxes = [bbox]
    dense, sparse = _vectors([chunk])
    await index.upsert_chunks(
        _meta(11), [chunk], chunk_ids=[201],
        dense_vectors=dense, sparse_vectors=sparse,
    )
    points, _ = index._client.scroll(
        collection_name=index.collection, with_payload=True, limit=10
    )
    assert points[0].payload["bboxes"] == [bbox]


def test_point_id_deterministico() -> None:
    """Stesso (document_id, ord) → stesso UUID5; documenti diversi → id diversi."""
    assert point_id(10, 0) == point_id(10, 0)
    assert point_id(10, 0) != point_id(10, 1)
    assert point_id(10, 0) != point_id(11, 0)


async def test_upsert_idempotente(index) -> None:
    """Re-upsert degli stessi chunk non duplica i punti (id deterministici)."""
    index.ensure_collection()
    chunks = [_chunk(0, "testo")]
    dense, sparse = _vectors(chunks)
    for _ in range(2):
        await index.upsert_chunks(
            _meta(10), chunks, chunk_ids=[101],
            dense_vectors=dense, sparse_vectors=sparse,
        )
    count = index._client.count(collection_name=index.collection).count
    assert count == 1


async def test_delete_document_filtra_per_documento(index) -> None:
    """delete_document rimuove solo i punti del documento indicato."""
    index.ensure_collection()
    for doc_id in (10, 20):
        chunks = [_chunk(0, f"doc {doc_id}")]
        dense, sparse = _vectors(chunks)
        await index.upsert_chunks(
            _meta(doc_id), chunks, chunk_ids=[doc_id],
            dense_vectors=dense, sparse_vectors=sparse,
        )
    await index.delete_document(10)
    points, _ = index._client.scroll(
        collection_name=index.collection, with_payload=True, limit=10
    )
    assert len(points) == 1
    assert points[0].payload["document_id"] == 20


async def test_lock_rilasciato_su_eccezione(index) -> None:
    """Se l'upsert Qdrant fallisce, il lock embedded è comunque rilasciato:
    le operazioni successive non restano bloccate in attesa."""
    import numpy as np

    index.ensure_collection()
    chunks = [_chunk(0, "testo")]

    def boom(*args, **kwargs):
        raise RuntimeError("qdrant irraggiungibile")

    index._client.upsert = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="qdrant irraggiungibile"):
        await index.upsert_chunks(
            _meta(10),
            chunks,
            chunk_ids=[101],
            dense_vectors=np.ones((1, 4), dtype=np.float32),
            sparse_vectors=[{"indices": [0], "values": [1.0]}],
        )
    assert not index._lock.locked()
