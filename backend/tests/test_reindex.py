"""Test della reindicizzazione vettoriale: job in-app e route admin.

L'app gira su DB SQLite e Qdrant embedded temporanei (come in test_api);
l'Embedder è sostituito da un fake che registra i testi ricevuti: si
verifica così che il reindex ri-embeddi ``titolo + testo`` mantenendo
pulito il payload Qdrant (citazioni e snippet).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app import reindex
from app.config import get_settings
from app.db import get_session_factory
from app.ingestion import pipeline
from app.ingestion.embeddings import Embedder
from app.models import Chunk, Document, DocumentStatus, Source

# --- fixture dell'app ---------------------------------------------------------


class FakeEmbedder:
    """Embedder finto: vettori fissi, testi densi registrati (nessun ONNX)."""

    dense_dim = 4

    def __init__(self) -> None:
        self.passages: list[str] = []

    def embed_passages(self, texts, **kwargs):
        self.passages.extend(texts)
        return np.ones((len(texts), 4), dtype=np.float32)

    def embed_sparse(self, texts, **kwargs):
        return [{"indices": [0, 1], "values": [0.5, 0.5]} for _ in texts]


@asynccontextmanager
async def _lifespan(app):
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
def fake_embedder(monkeypatch) -> FakeEmbedder:
    fake = FakeEmbedder()
    monkeypatch.setattr(Embedder, "get", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
async def client(
    tmp_path, monkeypatch, fake_embedder
) -> AsyncIterator[AsyncClient]:
    """App di test su directory temporanee, embedder fake, job reindex pulito."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    # Pausa breve del reindex sulla coda ingest: i test non devono attendere
    monkeypatch.setattr(reindex, "_INGEST_PAUSE_S", 0.01)
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()
    reindex.reset_job()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    db_module.reset_engine()
    pipeline.reset_state()
    reindex.reset_job()
    get_settings.cache_clear()


async def _login(client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed_documents() -> list[int]:
    """Due documenti ``indexed`` con 3 chunk l'uno, più un ``failed``."""
    async with get_session_factory()() as db:
        source = Source(name="cartella", type="local_fs", config_json={})
        db.add(source)
        await db.flush()
        doc_ids: list[int] = []
        for n in (1, 2):
            doc = Document(
                source_id=source.id,
                external_id=f"/docs/doc{n}.pdf",
                title=f"RISTOGEST OSTELLO/PLANIMETRIE/Planim doc{n}",
                doc_type="pdf",
                status=DocumentStatus.indexed.value,
            )
            db.add(doc)
            await db.flush()
            doc_ids.append(doc.id)
            for ord_ in range(3):
                db.add(
                    Chunk(
                        document_id=doc.id,
                        ord=ord_,
                        text=f"chunk {ord_} del documento {n}",
                        context_text=f"contesto {ord_}",
                        section_path=["Sezione"],
                        token_count=5,
                    )
                )
        failed = Document(
            source_id=source.id,
            external_id="/docs/rotto.pdf",
            title="Documento rotto",
            status=DocumentStatus.failed.value,
        )
        db.add(failed)
        await db.commit()
        return doc_ids


# --- test ---------------------------------------------------------------------


async def test_reindex_richiede_conferma(client: AsyncClient) -> None:
    """Senza ``confirm: true`` l'avvio è rifiutato con 400."""
    headers = await _login(client)
    resp = await client.post("/api/admin/reindex", json={}, headers=headers)
    assert resp.status_code == 400


async def test_reindex_solo_admin(client: AsyncClient) -> None:
    """Le route reindex sono protette: senza token → 401."""
    resp = await client.post("/api/admin/reindex", json={"confirm": True})
    assert resp.status_code == 401
    resp = await client.get("/api/admin/reindex/status")
    assert resp.status_code == 401


async def test_reindex_end_to_end_con_pausa_ingest_e_doppio_avvio(
    client: AsyncClient, fake_embedder: FakeEmbedder
) -> None:
    """Avvio (202) → pausa con coda ingest attiva → doppio avvio (409) →
    completamento: punti riscritti con testo embeddato ``titolo + testo``
    e payload Qdrant pulito."""
    headers = await _login(client)
    doc_ids = await _seed_documents()

    # Coda di ingestion "attiva" (documento pending fittizio): il job parte
    # ma resta in pausa cedendo il passo all'ingest
    pipeline._state.pending[999] = "documento in coda"
    resp = await client.post(
        "/api/admin/reindex", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 202
    assert resp.json() == {"started": True, "dry_run": False}

    # Un secondo avvio mentre il job è attivo viene rifiutato con 409
    resp = await client.post(
        "/api/admin/reindex", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 409

    # Lo status riflette la pausa (il job non lavora finché la coda è piena)
    await asyncio.sleep(0.05)
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["state"] == "running"
    assert status["paused_for_ingest"] is True
    assert fake_embedder.passages == []

    # Coda svuotata: il job riparte e completa
    pipeline._state.pending.clear()
    await reindex.get_job().wait()
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["state"] == "done"
    assert status["total_documents"] == 2  # il failed è escluso
    assert status["done_documents"] == 2
    assert status["failed_documents"] == 0
    assert status["done_chunks"] == 6
    assert status["percent"] == 100.0

    # Il testo embeddato antepone il titolo (dopo il prefisso e5 "passage: ")
    assert len(fake_embedder.passages) == 6
    assert (
        "RISTOGEST OSTELLO/PLANIMETRIE/Planim doc1\nchunk 0 del documento 1"
        in fake_embedder.passages
    )

    # I payload Qdrant restano puliti (niente titolo nel testo delle citazioni)
    qdrant = pipeline._get_index()
    points, _ = qdrant._client.scroll(
        collection_name=qdrant.collection, with_payload=True, limit=20
    )
    assert len(points) == 6
    texts = {p.payload["text"] for p in points}
    assert "chunk 0 del documento 1" in texts
    assert all(not text.startswith("RISTOGEST") for text in texts)
    assert {p.payload["document_id"] for p in points} == set(doc_ids)

    # Lavoro completato: lo stato di ripresa è stato rimosso
    assert not (get_settings().data_dir / "reindex_state.json").exists()


async def test_reindex_riprende_dallo_stato_salvato(
    client: AsyncClient, fake_embedder: FakeEmbedder
) -> None:
    """Con uno stato di ripresa salvato il job salta i documenti già fatti;
    con ``restart: true`` riparte invece dall'inizio."""
    headers = await _login(client)
    doc_ids = await _seed_documents()
    state_path = get_settings().data_dir / "reindex_state.json"
    state_path.write_text(json.dumps({"last_document_id": doc_ids[0]}))

    resp = await client.post(
        "/api/admin/reindex", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 202
    await reindex.get_job().wait()
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["state"] == "done"
    assert status["skipped_documents"] == 1
    assert status["done_documents"] == 1
    assert status["done_chunks"] == 3

    # restart=true: si riparte da zero e si rilavorano entrambi i documenti
    fake_embedder.passages.clear()
    resp = await client.post(
        "/api/admin/reindex",
        json={"confirm": True, "restart": True},
        headers=headers,
    )
    assert resp.status_code == 202
    await reindex.get_job().wait()
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["done_documents"] == 2
    assert len(fake_embedder.passages) == 6


async def test_reindex_dry_run_conta_senza_toccare_l_indice(
    client: AsyncClient, fake_embedder: FakeEmbedder
) -> None:
    """``dry_run`` conta documenti e chunk senza embeddare né scrivere."""
    headers = await _login(client)
    await _seed_documents()
    resp = await client.post(
        "/api/admin/reindex",
        json={"confirm": True, "dry_run": True},
        headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json()["dry_run"] is True
    await reindex.get_job().wait()
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["state"] == "done"
    assert status["dry_run"] is True
    assert status["total_documents"] == 2
    assert status["total_chunks"] == 6
    assert status["done_chunks"] == 0
    assert fake_embedder.passages == []


async def test_reindex_errore_su_un_documento_non_ferma_il_job(
    client: AsyncClient, fake_embedder: FakeEmbedder, monkeypatch
) -> None:
    """Un errore su un documento è loggato e contato: il job prosegue."""
    headers = await _login(client)
    doc_ids = await _seed_documents()

    original = reindex.ReindexJob._reindex_document

    async def flaky(self, document_id, index, embedder):
        if document_id == doc_ids[0]:
            raise RuntimeError("embedding esploso")
        return await original(self, document_id, index, embedder)

    monkeypatch.setattr(reindex.ReindexJob, "_reindex_document", flaky)
    resp = await client.post(
        "/api/admin/reindex", json={"confirm": True}, headers=headers
    )
    assert resp.status_code == 202
    await reindex.get_job().wait()
    status = (await client.get("/api/admin/reindex/status", headers=headers)).json()
    assert status["state"] == "done"
    assert status["failed_documents"] == 1
    assert status["done_documents"] == 1
    assert status["done_chunks"] == 3
