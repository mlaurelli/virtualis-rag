"""Pipeline di ingestion asincrona: coda in-process + worker asyncio.

Flusso per ogni file:

1. :func:`ingest_file` registra/aggiorna la riga ``Document`` (status
   ``pending``) e accoda il lavoro. La deduplicazione via ``content_hash``
   (sha256) evita di reindicizzare file identici già ``indexed``.
2. Il worker (uno per processo, avviato con :func:`start_worker` nel
   lifespan dell'app) esegue: conversione → rimozione vecchi chunk (DB e
   indice) → chunking → embedding → upsert Qdrant → salvataggio chunk →
   status ``indexed`` (con ``page_count``) oppure ``failed`` con errore.

Lo stato è consultabile con :func:`pipeline_status`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_settings
from ..knowledge.extractor import delete_document_graph, extract_for_document
from ..llm import get_kimi_client
from ..models import Chunk, Document, DocumentStatus, Source
from . import convert_worker
from .chunker import chunk_document
from .converter import ConversionError, convert_document
from .embeddings import Embedder, build_passage_text
from .indexer import VectorIndex

log = structlog.get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass
class _Job:
    """Elemento di lavoro in coda."""

    document_id: int
    path: Path


@dataclass
class _PipelineState:
    """Stato osservabile della pipeline (in-process)."""

    queue: asyncio.Queue[_Job] = field(default_factory=asyncio.Queue)
    pending: dict[int, str] = field(default_factory=dict)  # document_id -> titolo
    processing: int | None = None
    processing_since: float | None = None  # monotonic all'inizio della lavorazione
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=50))


_state = _PipelineState()
_worker_task: asyncio.Task | None = None
_watchdog_task: asyncio.Task | None = None
_session_factory: SessionFactory | None = None
_index: VectorIndex | None = None


def content_hash(path: Path) -> str:
    """SHA-256 del contenuto del file (lettura a blocchi)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _public_error(exc: Exception) -> str:
    """Messaggio di errore mostrabile via API (campo ``error`` del documento).

    Le eccezioni "di dominio" (conversione, configurazione connettore) hanno
    messaggi già pensati per l'utente; tutto il resto è sostituito da un
    messaggio generico con il solo tipo, perché ``str(exc)`` può contenere
    path interni o dettagli d'infrastruttura. L'errore completo resta nel log.
    """
    from ..connectors.base import ConnectorNotConfiguredError

    if isinstance(exc, (ConversionError, ConnectorNotConfiguredError)):
        return str(exc)
    return f"Errore interno durante l'indicizzazione ({type(exc).__name__})"


def _get_index() -> VectorIndex:
    """VectorIndex condiviso dal worker (creato lazy)."""
    global _index
    if _index is None:
        _index = VectorIndex()
    return _index


def reset_state() -> None:
    """Azzera lo stato globale (usato dai test tra un caso e l'altro)."""
    global _state, _worker_task, _watchdog_task, _session_factory, _index
    _state = _PipelineState()
    _worker_task = None
    _watchdog_task = None
    _session_factory = None
    _index = None


async def ingest_file(
    path: Path | str,
    source_id: int,
    external_id: str,
    session_factory: SessionFactory,
    *,
    doc_meta: dict[str, Any] | None = None,
) -> int:
    """Registra/aggiorna il Document e lo accoda per l'indicizzazione.

    Ritorna il ``document_id``. Se il file è identico (stesso content_hash)
    a un documento già ``indexed``, l'ingest è saltato (dedup).
    """
    path = Path(path)
    doc_meta = doc_meta or {}
    # Hash bloccante (lettura dell'intero file): in thread, fuori dal loop
    file_hash = await asyncio.to_thread(content_hash, path)
    stat = path.stat()

    async with session_factory() as db:
        result = await db.execute(
            select(Document).where(
                Document.source_id == source_id,
                Document.external_id == external_id,
            )
        )
        document = result.scalar_one_or_none()

        if (
            document is not None
            and document.content_hash == file_hash
            and document.status == DocumentStatus.indexed.value
        ):
            log.info("ingest saltato: contenuto identico", document_id=document.id)
            _state.recent.append(
                {
                    "document_id": document.id,
                    "title": document.title,
                    "status": "skipped",
                    "error": None,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            return document.id

        if document is None:
            document = Document(source_id=source_id, external_id=external_id)
            db.add(document)

        document.title = doc_meta.get("title") or path.stem
        document.doc_type = path.suffix.lower().lstrip(".")
        document.author = doc_meta.get("author") or ""
        document.content_hash = file_hash
        document.size_bytes = stat.st_size
        document.modified_at = doc_meta.get("modified_at") or datetime.fromtimestamp(
            stat.st_mtime, tz=UTC
        ).replace(tzinfo=None)
        document.status = DocumentStatus.pending.value
        document.error = None
        await db.commit()
        await db.refresh(document)
        document_id = document.id
        title = document.title

    _state.pending[document_id] = title
    await _state.queue.put(_Job(document_id=document_id, path=path))
    log.info("file accodato per ingest", document_id=document_id, path=str(path))
    return document_id


async def _process(job: _Job, session_factory: SessionFactory) -> None:
    """Esegue l'intera catena di indicizzazione per un documento."""
    settings = get_settings()
    document_id = job.document_id

    async def set_status(
        status: DocumentStatus, *, error: str | None = None, page_count: int | None = None
    ) -> None:
        async with session_factory() as db:
            document = await db.get(Document, document_id)
            if document is None:
                return
            document.status = status.value
            document.error = error
            if page_count is not None:
                document.page_count = page_count
            await db.commit()

    _state.pending.pop(document_id, None)
    _state.processing = document_id
    _state.processing_since = time.monotonic()
    error: str | None = None
    try:
        await set_status(DocumentStatus.processing)

        # 1. Conversione — nel sottoprocesso isolato (limite di memoria
        # proprio) quando configurato: un documento patologico fallisce da
        # solo (ConversionError → failed) senza far uccidere l'intera app
        # dall'OOM killer del container; il worker è ricreato al prossimo uso
        if settings.convert_in_subprocess:
            norm = await asyncio.to_thread(
                convert_worker.convert, job.path, timeout=_CONVERT_TIMEOUT_S
            )
        else:
            norm = convert_document(job.path, doc_meta={})

        # 2. Rimozione vecchi chunk (DB + indice) prima del re-ingest
        index = _get_index()
        await index.delete_document(document_id)
        async with session_factory() as db:
            document = await db.get(Document, document_id)
            if document is None:
                raise ConversionError(f"Document {document_id} sparito dal DB")
            source = await db.get(Source, document.source_id)
            # Rimozione esplicita: `document.chunks.clear()` farebbe lazy
            # loading, non consentito nel contesto async di SQLAlchemy
            await db.execute(delete(Chunk).where(Chunk.document_id == document_id))
            # Knowledge graph: mention/relation della versione precedente
            await delete_document_graph(document_id, session=db)
            await db.flush()

            # 3. Chunking
            chunks = chunk_document(
                norm,
                target_tokens=settings.chunk_target_tokens,
                overlap_tokens=settings.chunk_overlap_tokens,
            )

            # 4. Righe chunk nel DB (gli id servono nel payload Qdrant)
            rows = [
                Chunk(
                    document_id=document_id,
                    ord=chunk.ord,
                    text=chunk.text,
                    section_path=chunk.section_path,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    context_text=chunk.context_text,
                    token_count=chunk.token_count,
                    bboxes_json=json.dumps(chunk.bboxes),
                )
                for chunk in chunks
            ]
            db.add_all(rows)
            await db.flush()
            chunk_ids = [row.id for row in rows]
            document_meta: dict[str, Any] = {
                "document_id": document_id,
                "title": document.title,
                "source_id": document.source_id,
                "source_name": source.name if source else "",
                "doc_type": document.doc_type,
                "author": document.author,
                "modified_at": document.modified_at,
            }
            # Commit delle righe chunk (sostituzione completa delle vecchie)
            await db.commit()

        # 5. Embedding (denso + sparso) in batch. Il testo embeddato antepone
        # il titolo del documento (per local_fs/M365 include il percorso
        # relativo): i chunk "muti" (es. tabelle di soli MAC address) restano
        # così agganciati al loro contesto anche in BM25. Payload Qdrant e DB
        # conservano il testo pulito (citazioni e snippet invariati).
        texts = [
            build_passage_text(document_meta["title"], chunk.text)
            for chunk in chunks
        ]
        if texts:
            embedder = Embedder.get()
            dense_vectors, sparse_vectors = await asyncio.gather(
                asyncio.to_thread(embedder.embed_passages, texts),
                asyncio.to_thread(embedder.embed_sparse, texts),
            )
            # 6. Upsert su Qdrant
            await index.upsert_chunks(
                document_meta,
                chunks,
                chunk_ids=chunk_ids,
                dense_vectors=dense_vectors,
                sparse_vectors=sparse_vectors,
            )

        # Commit finale delle righe chunk
        async with session_factory() as db:
            document = await db.get(Document, document_id)
            if document is not None:
                document.status = DocumentStatus.indexed.value
                document.page_count = norm.page_count
                await db.commit()
        log.info("documento indicizzato", document_id=document_id, chunks=len(chunks))

        # 7. Knowledge graph (best-effort): estrazione entità/relazioni.
        # Qualunque fallimento è solo loggato: lo status resta ``indexed``.
        if settings.kg_enabled:
            try:
                llm = get_kimi_client()
                async with session_factory() as db:
                    report = await extract_for_document(
                        document_id, session=db, llm=llm
                    )
                log.info(
                    "knowledge graph aggiornato",
                    document_id=document_id,
                    n_entities=report.get("n_entities"),
                    n_relations=report.get("n_relations"),
                    error=report.get("error"),
                )
            except Exception as exc:
                log.warning(
                    "estrazione knowledge graph fallita (ingest comunque riuscito)",
                    document_id=document_id,
                    error=str(exc),
                )
    except asyncio.CancelledError:
        # Cancellato dal timeout del worker (o da shutdown): è un fallimento,
        # non un successo — lo status e il recent devono dirlo
        error = "Lavorazione interrotta (timeout o arresto)"
        log.error("ingest interrotto", document_id=document_id)
        await set_status(DocumentStatus.failed, error=error)
        raise
    except Exception as exc:
        error = _public_error(exc)
        # Il log conserva l'errore completo; al documento va la versione pubblica
        log.error("ingest fallito", document_id=document_id, error=str(exc))
        await set_status(DocumentStatus.failed, error=error)
    finally:
        _state.processing = None
        _state.processing_since = None
        _state.recent.append(
            {
                "document_id": document_id,
                "title": job.path.name,
                "status": "failed" if error else "indexed",
                "error": error,
                "at": datetime.now(UTC).isoformat(),
            }
        )


# Tempo massimo per l'indicizzazione di un singolo documento: se _process
# si appende (attesa senza timeout su una risorsa esterna, es. chiamata LLM
# con connessione morta), il documento viene marcato failed e il worker
# prosegue invece di stallare l'intera coda silenziosamente
_PROCESS_TIMEOUT_S = 20 * 60

# Timeout della sola conversione nel worker in sottoprocesso: sotto il
# timeout complessivo del documento, così il fallimento arriva pulito dal
# worker (ConversionError, worker ricreato) invece che dal wait_for esterno
_CONVERT_TIMEOUT_S = _PROCESS_TIMEOUT_S - 2 * 60


async def _worker_loop(session_factory: SessionFactory) -> None:
    """Corpo del worker: consuma la coda finché non viene cancellato."""
    while True:
        job = await _state.queue.get()
        try:
            await asyncio.wait_for(
                _process(job, session_factory), timeout=_PROCESS_TIMEOUT_S
            )
        except TimeoutError:
            # _process è rimasto appeso oltre il timeout: il documento resta
            # failed, il worker sopravvive e passa al successivo
            log.error(
                "ingest scaduto: documento marcato failed dopo il timeout",
                document_id=job.document_id,
                timeout_s=_PROCESS_TIMEOUT_S,
            )
            _state.processing = None
            _state.recent.append(
                {
                    "document_id": job.document_id,
                    "title": job.path.name,
                    "status": "failed",
                    "error": f"Timeout di lavorazione ({_PROCESS_TIMEOUT_S // 60} min)",
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            try:
                async with session_factory() as db:
                    document = await db.get(Document, job.document_id)
                    if document is not None:
                        document.status = DocumentStatus.failed.value
                        document.error = (
                            f"Timeout di lavorazione ({_PROCESS_TIMEOUT_S // 60} min)"
                        )
                        await db.commit()
            except Exception as exc:
                log.error("salvataggio stato failed fallito", error=str(exc))
        except Exception:
            # Rete di sicurezza: un errore non gestito non deve mai uccidere
            # il worker (la coda si fermerebbe silenziosamente)
            log.exception("worker: errore non gestito sul documento")
        finally:
            _state.queue.task_done()


# Intervallo del watchdog: verifica periodica che il worker sia vivo e che
# ogni ``pending`` del DB abbia un corrispettivo nella coda in memoria (la
# coda NON è persistente: un job perso o un worker morto, senza questo
# controllo, lascerebbero i documenti fermi per sempre — incidente del
# 26-27/08 con la coda gdrive congelata a decine di pending)
_WATCHDOG_INTERVAL_S = 60
# Il conteggio orfani deve persistere per due giri prima di intervenire:
# ingest_file scrive prima il DB e poi accoda, e il watchdog non deve
# duplicare un job in quella finestra (microsecondi, ma la certezza costa
# un minuto di attesa in più)
_WATCHDOG_ORPHAN_STRIKES = 2
_orphan_strikes = 0


async def _watchdog_loop(session_factory: SessionFactory) -> None:
    """Sorveglia il worker: lo rianima se morto e riaccoda i job orfani."""
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL_S)
        try:
            await _watchdog_tick(session_factory)
        except Exception:
            log.exception("watchdog ingest: errore non gestito (riprovo al giro dopo)")


async def _watchdog_tick(session_factory: SessionFactory) -> None:
    global _worker_task, _orphan_strikes
    # 1. Worker morto (eccezione sfuggita alla rete, cancel inatteso):
    #    log della causa e riavvio — la coda in memoria viene consumata
    #    dal nuovo task, gli eventuali pending orfani dal punto 3
    if _worker_task is None or _worker_task.done():
        cause: str | None = None
        if _worker_task is not None and _worker_task.done():
            if _worker_task.cancelled():
                cause = "cancellato"
            elif _worker_task.exception() is not None:
                cause = repr(_worker_task.exception())
        log.error("watchdog: worker di ingestion morto, riavvio", causa=cause)
        _worker_task = None
        await start_worker(session_factory)

    # 2. Lavorazione oltre il DOPPIO del timeout previsto (il wait_for del
    #    worker non è scattato: loop bloccato altrove): worker cancellato e
    #    ricreato, documento marcato failed — altrimenti coda congelata
    if (
        _state.processing is not None
        and _state.processing_since is not None
        and time.monotonic() - _state.processing_since > _PROCESS_TIMEOUT_S * 2
    ):
        document_id = _state.processing
        log.error(
            "watchdog: lavorazione oltre il doppio del timeout, worker riavviato",
            document_id=document_id,
        )
        if _worker_task is not None:
            _worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _worker_task
            _worker_task = None
        _state.processing = None
        _state.processing_since = None
        try:
            async with session_factory() as db:
                document = await db.get(Document, document_id)
                if (
                    document is not None
                    and document.status == DocumentStatus.processing.value
                ):
                    document.status = DocumentStatus.failed.value
                    document.error = "Lavorazione bloccata: interrotta dal watchdog"
                    await db.commit()
        except Exception as exc:
            log.error("watchdog: salvataggio stato failed fallito", error=str(exc))
        await start_worker(session_factory)
        return

    # 3. Pending nel DB senza corrispettivo in memoria (job persi):
    #    resume_interrupted li riaccoda; quelli senza file diventano failed
    #    così la sync della sorgente li riscarica alla prossima esecuzione
    if _state.processing is None:
        async with session_factory() as db:
            n_pending = (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.status == DocumentStatus.pending.value
                    )
                )
            ).scalar_one()
        if n_pending > _state.queue.qsize():
            _orphan_strikes += 1
            if _orphan_strikes >= _WATCHDOG_ORPHAN_STRIKES:
                stats = await resume_interrupted(session_factory)
                log.warning("watchdog: documenti orfani ripresi", **stats)
                _orphan_strikes = 0
        else:
            _orphan_strikes = 0
    else:
        _orphan_strikes = 0


async def start_worker(session_factory: SessionFactory | None = None) -> None:
    """Avvia il worker di ingestion (da chiamare nel lifespan dell'app)."""
    global _worker_task, _session_factory
    if _worker_task is not None and not _worker_task.done():
        return
    if session_factory is not None:
        _session_factory = session_factory
    if _session_factory is None:
        from ..db import get_session_factory

        _session_factory = get_session_factory()
    # Prepara la collection all'avvio (scarica il modello denso al primo uso)
    _get_index().ensure_collection()
    _worker_task = asyncio.create_task(_worker_loop(_session_factory))
    log.info("worker di ingestion avviato")
    global _watchdog_task
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_watchdog_loop(_session_factory))
        log.info("watchdog di ingestion avviato", intervallo_s=_WATCHDOG_INTERVAL_S)


async def stop_worker() -> None:
    """Ferma il worker SENZA attendere lo svuotamento della coda.

    Con migliaia di documenti accodati un ``queue.join()`` terrebbe in vita
    il processo per ore allo shutdown (fino al SIGKILL dell'orchestratore):
    il worker viene cancellato subito e i documenti non processati (rimasti
    ``pending``/``processing`` nel DB) sono ripresi al riavvio da
    :func:`resume_interrupted`.
    """
    global _worker_task, _watchdog_task
    if _watchdog_task is not None:
        _watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _watchdog_task
        _watchdog_task = None
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _worker_task
    _worker_task = None
    log.info("worker di ingestion arrestato")


async def wait_idle() -> None:
    """Attende che la coda sia vuota (usato dai test)."""
    await _state.queue.join()


async def resume_interrupted(
    session_factory: SessionFactory, *, include_failed: bool = False
) -> dict[str, int]:
    """Ripresa automatica dopo un riavvio/crash del processo.

    La coda vive in memoria: ogni avvio la trova vuota mentre il DB può
    contenere documenti rimasti indietro. Questa funzione (chiamata nel
    lifespan e dal route ``/api/ingest/requeue``):

    - riaccoda i documenti ``pending`` ritrovando i file in ``upload_dir``
      per ``content_hash`` (o per path diretto, sorgente upload); quelli il
      cui file non esiste più diventano ``failed`` ("file non disponibile")
      invece di restare pending per sempre: la sync della sorgente li
      riscarica col retry dei failed;
    - marca ``failed`` i documenti rimasti in ``processing``: erano in
      lavorazione quando il processo è morto e sono i primi sospetti se la
      morte è stato un crash nativo — NON vengono riaccodati automaticamente;
    - i documenti ``failed`` restano failed: sono riaccodati SOLO con
      ``include_failed=True`` (azione esplicita, route ``/api/ingest/requeue``),
      mai automaticamente all'avvio — un lotto di fallimenti sistematici
      rilavorato a ogni riavvio brucerebbe ore di lavorazione inutile.
    """
    settings = get_settings()
    upload_dir = settings.upload_dir
    by_hash: dict[str, Path] = {}
    if upload_dir.is_dir():
        for upload_file in upload_dir.iterdir():
            if upload_file.is_file():
                # Hash bloccante: in thread, fuori dall'event loop
                by_hash[await asyncio.to_thread(content_hash, upload_file)] = (
                    upload_file
                )

    statuses = [DocumentStatus.pending.value, DocumentStatus.processing.value]
    if include_failed:
        statuses.append(DocumentStatus.failed.value)
    stats = {"requeued": 0, "marked_failed": 0, "missing": 0}
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Document).where(Document.status.in_(statuses))
                )
            )
            .scalars()
            .all()
        )
        for document in rows:
            if document.status == DocumentStatus.processing.value:
                document.status = DocumentStatus.failed.value
                document.error = "Lavorazione interrotta dal riavvio del servizio"
                stats["marked_failed"] += 1
                continue
            path: Path | None = None
            candidate = Path(document.external_id)
            if candidate.is_file():
                path = candidate
            elif document.content_hash in by_hash:
                path = by_hash[document.content_hash]
            if path is None:
                # File non più disponibile (staging pulito, riavvio a metà
                # download): lasciarlo pending lo congelerebbe per sempre —
                # la coda in memoria non ne sa nulla. Diventa failed con un
                # errore chiaro: la sync della sorgente lo riscarica (retry
                # dei failed) o l'import va rilanciato
                document.status = DocumentStatus.failed.value
                document.error = (
                    "File non più disponibile sul disco: da risincronizzare "
                    "la sorgente o ripetere l'import"
                )
                stats["missing"] += 1
                continue
            await ingest_file(
                path,
                document.source_id,
                document.external_id,
                session_factory,
                doc_meta={"title": document.title},
            )
            stats["requeued"] += 1
        await db.commit()
    if stats["requeued"] or stats["marked_failed"]:
        log.info("ripresa ingest dopo riavvio", **stats)
    return stats


def pipeline_status() -> dict[str, Any]:
    """Stato corrente: documenti in coda, in lavorazione e ultimi esiti."""
    return {
        "pending": [
            {"document_id": doc_id, "title": title}
            for doc_id, title in _state.pending.items()
        ],
        "processing": _state.processing,
        "recent": list(_state.recent),
    }
