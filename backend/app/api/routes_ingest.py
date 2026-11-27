"""Route ingestion: upload, sincronizzazione manuale e stato della coda.

Autorizzazioni:
- ``upload`` e ``status``: qualunque utente autenticato (lo status alimenta il
  filtro fonti della Ricerca nel frontend, per tutti gli utenti);
- ``sync``: solo admin (scelta progettuale: la sincronizzazione tocca intere
  sorgenti documentali ed è un'operazione di amministrazione).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.gdrive import GDriveConnector
from ..connectors.local_fs import SUPPORTED_EXTENSIONS, LocalFsConnector
from ..connectors.upload import save_upload
from ..db import get_db, get_session_factory
from ..ingestion import pipeline
from ..models import Document, Source, SourceType, User
from .deps import get_current_user, require_admin

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

# Numero massimo di esiti recenti esposti dallo status
_RECENT_MAX = 20

# Task di sincronizzazione in background (fire-and-forget, ma referenziati)
_background_syncs: set[asyncio.Task[None]] = set()

# Connettori sincronizzabili da POST /api/ingest/sync, per tipo di sorgente
_SYNC_CONNECTORS: dict[str, type[LocalFsConnector] | type[GDriveConnector]] = {
    SourceType.local_fs.value: LocalFsConnector,
    SourceType.gdrive.value: GDriveConnector,
}


@router.post("/upload", status_code=202)
async def upload(
    user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """Carica un documento nella sorgente ``upload`` e lo accoda per l'ingest."""
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="File mancante")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo di file non supportato: {suffix or '(senza estensione)'}",
        )
    settings = get_settings()
    max_bytes = settings.upload_max_mb * 1024 * 1024
    # Controllo anticipato sulla dimensione nota dallo spool multipart: un file
    # oltre il limite è rifiutato senza leggerne il corpo in memoria
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File troppo grande: massimo {settings.upload_max_mb} MB",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="File vuoto")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File troppo grande: massimo {settings.upload_max_mb} MB",
        )
    document_id = await save_upload(data, file.filename, get_session_factory())
    return {"document_id": document_id, "status": "pending"}


class SyncIn(BaseModel):
    """Richiesta di sincronizzazione: una sorgente o tutte quelle abilitate."""

    source_id: int | None = None


async def _sync_sources(source_ids: list[int]) -> None:
    """Esegue la sync delle sorgenti (per tipo) in un task di background."""
    session_factory = get_session_factory()
    for source_id in source_ids:
        try:
            async with session_factory() as db:
                source = await db.get(Source, source_id)
                if source is None:
                    continue
                connector_cls = _SYNC_CONNECTORS.get(source.type)
                if connector_cls is None:
                    log.warning(
                        "sorgente non sincronizzabile",
                        source_id=source_id,
                        type=source.type,
                    )
                    continue
                connector = connector_cls(dict(source.config_json or {}))
            report = await connector.sync(source, session_factory)
            log.info(
                "sincronizzazione sorgente completata",
                source_id=source_id,
                scanned=report["scanned"],
                ingested=len(report["ingested"]),
                deleted=len(report["deleted"]),
                errors=len(report["errors"]),
            )
        except Exception as exc:
            log.error(
                "sincronizzazione sorgente fallita", source_id=source_id, error=str(exc)
            )


@router.post("/sync", status_code=202)
async def sync(
    body: SyncIn,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Avvia la sincronizzazione manuale (admin-only) in background."""
    if body.source_id is not None:
        source = await db.get(Source, body.source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Sorgente non trovata")
        if source.type not in _SYNC_CONNECTORS:
            raise HTTPException(
                status_code=400,
                detail=f"Sorgenti di tipo '{source.type}' non sincronizzabili manualmente",
            )
        source_ids = [source.id]
    else:
        source_ids = list(
            (
                await db.execute(
                    select(Source.id).where(
                        Source.enabled.is_(True),
                        Source.type.in_(sorted(_SYNC_CONNECTORS)),
                    )
                )
            )
            .scalars()
            .all()
        )
    if source_ids:
        task = asyncio.create_task(_sync_sources(source_ids))
        # Riferimento mantenuto finché la sync non termina (evita GC prematuro del task)
        _background_syncs.add(task)
        task.add_done_callback(_background_syncs.discard)
    return {"started": True, "sources": source_ids}


@router.get("/status")
async def ingest_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Stato della coda di indicizzazione (tutti gli utenti autenticati)."""
    status = pipeline.pipeline_status()

    counts: dict[int, dict[str, int]] = {}
    rows = (
        await db.execute(
            select(Document.source_id, Document.status, func.count()).group_by(
                Document.source_id, Document.status
            )
        )
    ).all()
    for source_id, doc_status, n in rows:
        counts.setdefault(source_id, {})[doc_status] = n

    sources = (
        (await db.execute(select(Source).order_by(Source.id))).scalars().all()
    )
    per_source = [
        {
            "source_id": s.id,
            "name": s.name,
            "enabled": s.enabled,
            "last_sync_at": s.last_sync_at,
            "indexed": counts.get(s.id, {}).get("indexed", 0),
            "failed": counts.get(s.id, {}).get("failed", 0),
        }
        for s in sources
    ]
    recent = [
        {
            "document_id": r["document_id"],
            "title": r["title"],
            "status": r["status"],
            "error": r["error"],
        }
        for r in list(status["recent"])[-_RECENT_MAX:]
    ][::-1]
    return {
        "queue": {
            "pending": len(status["pending"]),
            "processing": 1 if status["processing"] is not None else 0,
        },
        "per_source": per_source,
        "recent": recent,
    }


@router.post("/requeue", status_code=202)
async def requeue(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Riaccoda i documenti ``pending``/``failed`` il cui file è su disco.

    La coda di ingestion vive in memoria: un riavvio dell'app a metà ingest
    lascia i documenti scaricati in stato pending senza che nessuno li
    processi. Usa la stessa logica della ripresa automatica all'avvio
    (:func:`pipeline.resume_interrupted`), ma con ``include_failed=True``:
    questo route È l'azione esplicita che rilavora anche i ``failed``
    (all'avvio automatico restano failed). I file sono ritrovati per
    ``content_hash`` e i residui ``processing`` marcati failed.
    """
    return await pipeline.resume_interrupted(
        get_session_factory(), include_failed=True
    )
