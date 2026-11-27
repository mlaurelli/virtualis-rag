"""Test di integrazione end-to-end della pipeline di ingestion.

Usa l'embedder reale (intfloat/multilingual-e5-large + Qdrant/bm25): i modelli
sono scaricati una tantum da HuggingFace al primo utilizzo (~2 GB, pochi minuti)
e poi tenuti in cache locale da fastembed. Qdrant gira embedded in tmp_path.
"""


import asyncio

import pytest
from docx import Document as DocxDocument
from qdrant_client.http import models as qm
from sqlalchemy import select

from app.db import get_session_factory, init_db
from app.ingestion import pipeline
from app.models import Chunk, Document, DocumentStatus, Source


async def _make_source(session_factory) -> int:
    async with session_factory() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source.id


def _qdrant_count(index, document_id: int) -> int:
    return index._client.count(
        collection_name=index.collection,
        count_filter=qm.Filter(
            must=[
                qm.FieldCondition(
                    key="document_id", match=qm.MatchValue(value=document_id)
                )
            ]
        ),
    ).count


@pytest.fixture
async def session_factory(tmp_settings):
    await init_db()
    sf = get_session_factory()
    await pipeline.start_worker(sf)
    yield sf
    await pipeline.stop_worker()


async def test_ingest_markdown_end_to_end(session_factory, tmp_path) -> None:
    """File .md: ingest → indexed, chunk nel DB e punti in Qdrant."""
    sf = session_factory
    source_id = await _make_source(sf)
    md = tmp_path / "manuale.md"
    md.write_text(
        "# Manuale operativo\n\n"
        "Il sistema gestisce documenti eterogenei in locale.\n\n"
        "## Installazione\n\n"
        "Installare le dipendenze con uv e avviare il backend.\n\n"
        "## Configurazione\n\n"
        "Copiare il file .env.example in .env e valorizzare le chiavi.\n",
        encoding="utf-8",
    )

    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()

    async with sf() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value
        assert document.error is None
        chunks = (
            (await db.execute(select(Chunk).where(Chunk.document_id == document_id)))
            .scalars()
            .all()
        )
        assert len(chunks) >= 1
        assert chunks[0].ord == 0
        assert chunks[0].text
        assert chunks[0].context_text
        assert chunks[0].token_count > 0

    index = pipeline._get_index()
    assert _qdrant_count(index, document_id) == len(chunks)


async def test_ingest_docx_end_to_end(session_factory, tmp_path) -> None:
    """File .docx generato con python-docx: conversione Docling + indicizzazione."""
    sf = session_factory
    source_id = await _make_source(sf)
    path = tmp_path / "report.docx"
    doc = DocxDocument()
    doc.add_heading("Report trimestrale", level=1)
    doc.add_paragraph("Il trimestre ha registrato una crescita sostenuta.")
    table = doc.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.cell(0, 0).text = "Mese"
    table.cell(0, 1).text = "Fatturato"
    table.cell(1, 0).text = "Gennaio"
    table.cell(1, 1).text = "1000"
    doc.save(path)

    document_id = await pipeline.ingest_file(path, source_id, str(path), sf)
    await pipeline.wait_idle()

    async with sf() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value, document.error
        assert document.doc_type == "docx"
        chunks = (
            (await db.execute(select(Chunk).where(Chunk.document_id == document_id)))
            .scalars()
            .all()
        )
        assert any("Fatturato" in chunk.text for chunk in chunks)
        assert any(
            "Report trimestrale" in " > ".join(chunk.section_path) + chunk.text
            for chunk in chunks
        )


async def test_reingest_identico_saltato(session_factory, tmp_path) -> None:
    """Re-ingest dello stesso contenuto: skip per content_hash, niente doppioni."""
    sf = session_factory
    source_id = await _make_source(sf)
    md = tmp_path / "stabile.md"
    md.write_text("# Titolo\n\nContenuto stabile nel tempo.\n", encoding="utf-8")

    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()

    again = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()
    assert again == document_id
    assert pipeline.pipeline_status()["recent"][-1]["status"] == "skipped"

    index = pipeline._get_index()
    async with sf() as db:
        chunks = (
            (await db.execute(select(Chunk).where(Chunk.document_id == document_id)))
            .scalars()
            .all()
        )
    assert _qdrant_count(index, document_id) == len(chunks)


async def test_cancellazione_rimuove_punti(session_factory, tmp_path) -> None:
    """delete_document rimuove tutti i punti Qdrant del documento."""
    sf = session_factory
    source_id = await _make_source(sf)
    md = tmp_path / "eliminabile.md"
    md.write_text("# Temporaneo\n\nDocumento destinato alla cancellazione.\n",
                  encoding="utf-8")

    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()
    index = pipeline._get_index()
    assert _qdrant_count(index, document_id) > 0

    await index.delete_document(document_id)
    assert _qdrant_count(index, document_id) == 0


async def test_pipeline_status(session_factory, tmp_path) -> None:
    """pipeline_status espone pending/processing/recent."""
    sf = session_factory
    source_id = await _make_source(sf)
    md = tmp_path / "stato.md"
    md.write_text("# Stato\n\nVerifica dello stato della pipeline.\n",
                  encoding="utf-8")
    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()
    status = pipeline.pipeline_status()
    assert status["pending"] == []
    assert status["processing"] is None
    last = status["recent"][-1]
    assert last["document_id"] == document_id
    assert last["status"] == "indexed"


async def test_fallimento_a_meta_ingest_errore_sanitizzato_e_retry(
    session_factory, tmp_path, monkeypatch
) -> None:
    """Fallimento a metà ingest (dopo la conversione): il documento resta
    ``failed`` con un errore SENZA path interni né dettagli d'implementazione
    (quelli restano nel log); ripristinato il componente, il re-ingest dello
    stesso file va a buon fine (retry possibile, stato coerente)."""
    sf = session_factory
    source_id = await _make_source(sf)
    md = tmp_path / "fragile.md"
    md.write_text("# Fragile\n\nContenuto qualsiasi del documento.\n",
                  encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError(f"indice corrotto in {tmp_path}/qdrant/storage-privato")

    monkeypatch.setattr(pipeline, "chunk_document", boom)
    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()

    async with sf() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.failed.value
        assert document.error  # messaggio presente ma sanitizzato
        assert str(tmp_path) not in document.error
        assert "storage-privato" not in document.error
    last = pipeline.pipeline_status()["recent"][-1]
    assert last["status"] == "failed"
    assert "storage-privato" not in (last["error"] or "")

    # Retry: ripristinato il componente, lo stesso file è re-indicizzato
    monkeypatch.undo()
    again = await pipeline.ingest_file(md, source_id, str(md), sf)
    assert again == document_id
    await pipeline.wait_idle()
    async with sf() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value
        assert document.error is None


# --- ripresa dopo riavvio e arresto del worker -----------------------------------


async def _seed_document(
    session_factory, source_id: int, path, status: DocumentStatus, title: str
) -> int:
    """Riga Document già presente nel DB con lo status dato (file su disco)."""
    async with session_factory() as db:
        document = Document(
            source_id=source_id,
            external_id=str(path),
            title=title,
            doc_type="md",
            status=status.value,
            error="errore precedente" if status is DocumentStatus.failed else None,
        )
        db.add(document)
        await db.commit()
        await db.refresh(document)
        return document.id


async def test_resume_interrupted_non_riaccoda_i_failed(tmp_settings, tmp_path) -> None:
    """All'avvio: i ``pending`` sono riaccodati, i ``processing`` marcati
    failed, i ``failed`` RESTANO failed (nessuna rilavorazione automatica)."""
    await init_db()
    sf = get_session_factory()
    async with sf() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        source_id = source.id

    files = {}
    for name in ("pending", "failed", "processing"):
        f = tmp_path / f"{name}.md"
        f.write_text(f"# {name}\n\ncontenuto {name}.\n", encoding="utf-8")
        files[name] = f
    ids = {
        name: await _seed_document(
            sf, source_id, files[name], DocumentStatus[name], name
        )
        for name in files
    }

    stats = await pipeline.resume_interrupted(sf)
    assert stats == {"requeued": 1, "marked_failed": 1, "missing": 0}

    async with sf() as db:
        pending = await db.get(Document, ids["pending"])
        failed = await db.get(Document, ids["failed"])
        processing = await db.get(Document, ids["processing"])
    assert pending.status == DocumentStatus.pending.value
    # Il failed non è stato toccato: stesso status e stesso errore
    assert failed.status == DocumentStatus.failed.value
    assert failed.error == "errore precedente"
    assert processing.status == DocumentStatus.failed.value
    assert "interrotta" in processing.error

    # Azione ESPLICITA (route /api/ingest/requeue): anche i failed ripartono
    stats = await pipeline.resume_interrupted(sf, include_failed=True)
    assert stats["requeued"] == 3  # pending + i due failed
    async with sf() as db:
        failed = await db.get(Document, ids["failed"])
    assert failed.status == DocumentStatus.pending.value


async def test_resume_interrupted_pending_senza_file_diventa_failed(
    tmp_settings, tmp_path
) -> None:
    """Un pending il cui file non esiste più non resta congelato per sempre:
    diventa failed con errore chiaro (la sync della sorgente lo riscarica)."""
    await init_db()
    sf = get_session_factory()
    async with sf() as db:
        source = Source(name="test", type="gdrive", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        db.add(
            Document(
                source_id=source.id,
                external_id="gdrive://file-andato",
                title="Perso.pdf",
                status=DocumentStatus.pending.value,
                content_hash="hash-non-presente-su-disco",
            )
        )
        await db.commit()

    stats = await pipeline.resume_interrupted(sf)

    assert stats["missing"] == 1
    assert stats["requeued"] == 0
    async with sf() as db:
        doc = (await db.execute(select(Document))).scalar_one()
    assert doc.status == DocumentStatus.failed.value
    assert "non più disponibile" in doc.error


async def test_watchdog_rianima_worker_morto_e_riaccoda_orfani(
    tmp_settings, tmp_path, monkeypatch
) -> None:
    """Worker morto + pending nel DB senza job in memoria: il watchdog
    riavvia il worker e riaccoda l'orfano (dopo 2 giri antirumore)."""
    await init_db()
    sf = get_session_factory()
    source_id = await _make_source(sf)
    f = tmp_path / "orfano.md"
    f.write_text("# Orfano\n\ncontenuto orfano.\n", encoding="utf-8")
    doc_id = await _seed_document(sf, source_id, f, DocumentStatus.pending, "orfano")

    processed: list[int] = []

    async def fake_process(job, session_factory):
        processed.append(job.document_id)
        pipeline._state.processing = None
        pipeline._state.processing_since = None

    monkeypatch.setattr(pipeline, "_process", fake_process)

    await pipeline.start_worker(sf)
    try:
        # Simula la morte del worker: task cancellato (done) con coda vuota
        pipeline._worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pipeline._worker_task

        await pipeline._watchdog_tick(sf)  # rianima il worker, strike 1
        assert pipeline._worker_task is not None and not pipeline._worker_task.done()
        await pipeline._watchdog_tick(sf)  # strike 2: riaccoda l'orfano

        for _ in range(50):
            if doc_id in processed:
                break
            await asyncio.sleep(0.1)
        assert doc_id in processed
    finally:
        await pipeline.stop_worker()


async def test_stop_worker_non_attende_lo_svuotamento_della_coda(
    tmp_settings, tmp_path, monkeypatch
) -> None:
    """stop_worker cancella il worker subito: con la coda ancora piena non
    deve restare appeso su un join (i documenti non processati riparte
    resume_interrupted al riavvio)."""
    await init_db()
    sf = get_session_factory()
    started = asyncio.Event()

    async def slow_process(job, session_factory):
        started.set()
        await asyncio.sleep(3600)  # lavorazione che non finisce mai

    monkeypatch.setattr(pipeline, "_process", slow_process)
    await pipeline.start_worker(sf)
    source_id = await _make_source(sf)
    for name in ("primo.md", "secondo.md"):
        f = tmp_path / name
        f.write_text(f"# {name}\n\ncontenuto.\n", encoding="utf-8")
        await pipeline.ingest_file(f, source_id, str(f), sf)
    await asyncio.wait_for(started.wait(), timeout=10)

    # Coda NON vuota: senza il fix questo await resterebbe appeso sul join
    await asyncio.wait_for(pipeline.stop_worker(), timeout=5)
