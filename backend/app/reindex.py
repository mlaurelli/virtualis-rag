"""Reindicizzazione dell'indice vettoriale (Qdrant) dai chunk già in DB.

Job in-process con la stessa struttura dell'import M365: un solo job alla
volta, stato in memoria, avvio da ``POST /api/admin/reindex`` e polling su
``GET /api/admin/reindex/status``. Per ogni documento ``indexed`` ricostruisce
i punti Qdrant ri-embeddando (denso + sparso) i chunk salvati in DB col
formato corrente del testo indicizzato (titolo + testo, vedi
:func:`app.ingestion.embeddings.build_passage_text`): serve dopo un cambio di
formato, senza riconvertire i file sorgente.

Vincoli di progetto:

- il DB SQLite è in SOLA LETTURA: si riscrive solo Qdrant (delete + upsert
  per documento, con gli stessi id deterministici dell'ingestion);
- si usano l'indice e l'embedder GIÀ inizializzati dell'app: il client
  Qdrant local-mode non tollera un secondo processo sullo stesso path,
  quindi niente script standalone (``scripts/reindex.py`` è solo un client
  HTTP di questa route);
- la coda di ingestion ha la precedenza: con documenti pending/processing
  il job si mette in pausa e riprende a coda vuota;
- riprendibile: l'ultimo document_id completato è salvato in
  ``data_dir/reindex_state.json`` e una nuova esecuzione riparte da lì
  (``restart=true`` per ignorarlo). A lavoro completato il file è rimosso.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import get_settings
from .ingestion import pipeline
from .ingestion.chunker import ChunkData
from .ingestion.embeddings import Embedder, build_passage_text
from .ingestion.indexer import VectorIndex
from .models import Chunk, Document, DocumentStatus, Source

log = structlog.get_logger(__name__)

# File di stato per la ripresa (in data_dir, quindi in /data nel container)
_STATE_FILENAME = "reindex_state.json"

# Chunk embeddati/upsertati per batch (limita RAM e round-trip su Qdrant)
_EMBED_BATCH = 200

# Documenti tra un log di progresso e il successivo
_LOG_EVERY = 25

# Attesa tra i controlli della coda di ingestion quando il job è in pausa
_INGEST_PAUSE_S = 15.0


class ReindexRunningError(Exception):
    """Sollevata all'avvio se una reindicizzazione è già in corso."""


def _load_resume_id(path: Path) -> int:
    """Ultimo document_id completato nel run precedente (0 se assente)."""
    try:
        return int(json.loads(path.read_text())["last_document_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _save_resume_id(path: Path, document_id: int) -> None:
    """Salva lo stato di ripresa in modo atomico (scrittura + rename)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "last_document_id": document_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    tmp.replace(path)


def _chunk_data(row: Chunk) -> ChunkData:
    """ChunkData ricostruito dalla riga DB (stessa forma dell'ingestion)."""
    try:
        bboxes = json.loads(row.bboxes_json or "[]")
    except ValueError:
        bboxes = []
    return ChunkData(
        ord=row.ord,
        text=row.text,
        context_text=row.context_text,
        section_path=list(row.section_path or []),
        page_start=row.page_start,
        page_end=row.page_end,
        token_count=row.token_count,
        bboxes=bboxes,
    )


@dataclass
class _JobState:
    """Stato osservabile del job (esposto da /api/admin/reindex/status)."""

    state: str = "idle"  # idle | running | done | failed | cancelled
    dry_run: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    total_documents: int = 0  # documenti indexed totali
    skipped_documents: int = 0  # già completati in un run precedente (ripresa)
    done_documents: int = 0
    failed_documents: int = 0
    total_chunks: int = 0  # chunk da lavorare in QUESTO run
    done_chunks: int = 0
    current_document_id: int | None = None
    last_completed_document_id: int | None = None
    paused_for_ingest: bool = False
    started_monotonic: float | None = None

    def snapshot(self) -> dict[str, Any]:
        elapsed = (
            time.monotonic() - self.started_monotonic
            if self.started_monotonic is not None
            else 0.0
        )
        rate = self.done_chunks / elapsed if elapsed > 0 and self.done_chunks else 0.0
        remaining = max(self.total_chunks - self.done_chunks, 0)
        eta = remaining / rate if rate > 0 else None
        percent = (
            round(100 * self.done_chunks / self.total_chunks, 1)
            if self.total_chunks
            else (100.0 if self.state == "done" else 0.0)
        )
        return {
            "state": self.state,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "total_documents": self.total_documents,
            "skipped_documents": self.skipped_documents,
            "done_documents": self.done_documents,
            "failed_documents": self.failed_documents,
            "total_chunks": self.total_chunks,
            "done_chunks": self.done_chunks,
            "current_document_id": self.current_document_id,
            "last_completed_document_id": self.last_completed_document_id,
            "paused_for_ingest": self.paused_for_ingest,
            "percent": percent,
            "rate_chunks_per_s": round(rate, 2),
            "eta_seconds": round(eta) if eta is not None else None,
        }


class ReindexJob:
    """Job di reindicizzazione: enumera i documenti indexed e li rilavora."""

    def __init__(self) -> None:
        self._state = _JobState()
        self._task: asyncio.Task | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._dry_run = False
        self._restart = False

    # --- API pubblica ---

    def status(self) -> dict[str, Any]:
        return self._state.snapshot()

    def start(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dry_run: bool = False,
        restart: bool = False,
    ) -> None:
        """Avvia il job in background (solleva se ce n'è già uno attivo)."""
        if self._task is not None and not self._task.done():
            raise ReindexRunningError("Una reindicizzazione è già in esecuzione")
        self._state = _JobState(
            state="running",
            dry_run=dry_run,
            started_at=datetime.now(UTC).isoformat(),
            started_monotonic=time.monotonic(),
        )
        self._session_factory = session_factory
        self._dry_run = dry_run
        self._restart = restart
        self._task = asyncio.create_task(self._run())

    async def wait(self) -> None:
        """Attende la fine del job in corso (usato dai test)."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # --- corpo del job ---

    async def _run(self) -> None:
        st = self._state
        state_path = get_settings().data_dir / _STATE_FILENAME
        assert self._session_factory is not None
        try:
            resume_from = 0
            if self._restart:
                state_path.unlink(missing_ok=True)
            else:
                resume_from = _load_resume_id(state_path)

            async with self._session_factory() as db:
                doc_ids = list(
                    (
                        await db.execute(
                            select(Document.id)
                            .where(Document.status == DocumentStatus.indexed.value)
                            .order_by(Document.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                total_chunks = (
                    await db.scalar(
                        select(func.count())
                        .select_from(Chunk)
                        .join(Document, Chunk.document_id == Document.id)
                        .where(
                            Document.status == DocumentStatus.indexed.value,
                            Document.id > resume_from,
                        )
                    )
                ) or 0
            todo = [doc_id for doc_id in doc_ids if doc_id > resume_from]
            st.total_documents = len(doc_ids)
            st.skipped_documents = len(doc_ids) - len(todo)
            st.total_chunks = int(total_chunks)
            log.info(
                "reindicizzazione avviata",
                documenti=len(todo),
                chunk=st.total_chunks,
                ripresa_da_document_id=resume_from or None,
                dry_run=self._dry_run,
            )

            if self._dry_run:
                st.state = "done"
                return

            index = pipeline._get_index()
            embedder = Embedder.get()
            for pos, doc_id in enumerate(todo, start=1):
                await self._wait_ingest_idle()
                st.current_document_id = doc_id
                try:
                    await self._reindex_document(doc_id, index, embedder)
                    st.done_documents += 1
                    st.last_completed_document_id = doc_id
                    _save_resume_id(state_path, doc_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Errore per-documento: log e si prosegue col successivo
                    st.failed_documents += 1
                    log.error(
                        "reindicizzazione documento fallita",
                        document_id=doc_id,
                        error=str(exc),
                    )
                if pos % _LOG_EVERY == 0 or pos == len(todo):
                    snap = st.snapshot()
                    log.info(
                        "reindicizzazione in corso",
                        percento=snap["percent"],
                        documenti=f"{pos}/{len(todo)}",
                        chunk_al_secondo=snap["rate_chunks_per_s"],
                        eta_s=snap["eta_seconds"],
                    )

            st.state = "done"
            # Lavoro completo: il prossimo run riparte dall'inizio. I
            # documenti falliti si rilavorano con restart=true.
            state_path.unlink(missing_ok=True)
            log.info(
                "reindicizzazione completata",
                documenti=st.done_documents,
                falliti=st.failed_documents,
                chunk=st.done_chunks,
            )
        except asyncio.CancelledError:
            # Arresto dell'app: lo stato salvato consente la ripresa
            st.state = "cancelled"
            st.error = "Reindicizzazione interrotta (arresto dell'applicazione)"
            log.warning(
                "reindicizzazione interrotta: alla prossima esecuzione "
                "riparte dall'ultimo documento completato"
            )
            raise
        except Exception as exc:
            st.state = "failed"
            st.error = str(exc)
            log.error("reindicizzazione fallita", error=str(exc))
        finally:
            st.current_document_id = None
            st.paused_for_ingest = False
            st.finished_at = datetime.now(UTC).isoformat()

    async def _wait_ingest_idle(self) -> None:
        """Cede il passo alla coda di ingestion: pausa finché ci sono
        documenti pending o in lavorazione (l'ingest ha la precedenza)."""
        st = self._state
        while True:
            queue = pipeline.pipeline_status()
            if not queue["pending"] and queue["processing"] is None:
                break
            if not st.paused_for_ingest:
                st.paused_for_ingest = True
                log.info("reindicizzazione in pausa: coda di ingestion attiva")
            await asyncio.sleep(_INGEST_PAUSE_S)
        if st.paused_for_ingest:
            st.paused_for_ingest = False
            log.info("reindicizzazione ripresa: coda di ingestion vuota")

    async def _reindex_document(
        self, document_id: int, index: VectorIndex, embedder: Embedder
    ) -> None:
        """Ricostruisce i punti Qdrant di un documento dai chunk in DB."""
        st = self._state
        assert self._session_factory is not None
        async with self._session_factory() as db:
            document = await db.get(Document, document_id)
            if (
                document is None
                or document.status != DocumentStatus.indexed.value
            ):
                return  # sparito o cambiato di stato nel frattempo
            source = await db.get(Source, document.source_id)
            rows = list(
                (
                    await db.execute(
                        select(Chunk)
                        .where(Chunk.document_id == document_id)
                        .order_by(Chunk.ord)
                    )
                )
                .scalars()
                .all()
            )
            document_meta: dict[str, Any] = {
                "document_id": document_id,
                "title": document.title,
                "source_id": document.source_id,
                "source_name": source.name if source else "",
                "doc_type": document.doc_type,
                "author": document.author,
                "modified_at": document.modified_at,
            }
        if not rows:
            return
        # Delete prima dell'upsert: gli id dei punti sono deterministici per
        # (document_id, ord), ma la delete elimina eventuali punti residui di
        # versioni con più chunk di quelli oggi in DB
        await index.delete_document(document_id)
        title = document_meta["title"]
        for start in range(0, len(rows), _EMBED_BATCH):
            batch = rows[start : start + _EMBED_BATCH]
            chunks = [_chunk_data(row) for row in batch]
            # Stesso formato dell'ingestion: titolo + testo, solo nell'embedding
            texts = [build_passage_text(title, chunk.text) for chunk in chunks]
            dense_vectors, sparse_vectors = await asyncio.gather(
                asyncio.to_thread(embedder.embed_passages, texts),
                asyncio.to_thread(embedder.embed_sparse, texts),
            )
            await index.upsert_chunks(
                document_meta,
                chunks,
                chunk_ids=[row.id for row in batch],
                dense_vectors=dense_vectors,
                sparse_vectors=sparse_vectors,
            )
            st.done_chunks += len(batch)


_job: ReindexJob | None = None


def get_job() -> ReindexJob:
    """Singleton del job di reindicizzazione."""
    global _job
    if _job is None:
        _job = ReindexJob()
    return _job


def reset_job() -> None:
    """Azzera il singleton (usato dai test)."""
    global _job
    _job = None
