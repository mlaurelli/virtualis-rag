"""Route documenti: elenco, dettaglio, file originale e cancellazione."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..ingestion import pipeline
from ..knowledge.extractor import delete_document_graph
from ..models import Chunk, Document, DocumentStatus, Source, SourceType, User
from .deps import get_current_user, get_db, require_admin

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _doc_out(document: Document) -> dict[str, Any]:
    """Serializza un documento nei campi del contratto."""
    return {
        "id": document.id,
        "source_id": document.source_id,
        "external_id": document.external_id,
        "title": document.title,
        "doc_type": document.doc_type,
        "author": document.author,
        "language": document.language,
        "status": document.status,
        "error": document.error,
        "page_count": document.page_count,
        "size_bytes": document.size_bytes,
        "modified_at": document.modified_at,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


async def _get_document(db: AsyncSession, document_id: int) -> Document:
    """Documento per id o 404."""
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Documento non trovato")
    return document


@router.get("")
async def list_documents(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    doc_status: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    source_id: Annotated[int | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Elenco documenti con filtri opzionali e paginazione semplice."""
    conditions = []
    if doc_status is not None:
        conditions.append(Document.status == doc_status.value)
    if source_id is not None:
        conditions.append(Document.source_id == source_id)
    total = await db.scalar(
        select(func.count()).select_from(Document).where(*conditions)
    )
    documents = (
        (
            await db.execute(
                select(Document)
                .where(*conditions)
                .order_by(Document.updated_at.desc(), Document.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"documents": [_doc_out(d) for d in documents], "total": total or 0}


@router.get("/{document_id}")
async def get_document(
    document_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Dettaglio documento (campi dell'elenco + numero di chunk)."""
    document = await _get_document(db, document_id)
    chunk_count = await db.scalar(
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.document_id == document.id)
    )
    return {**_doc_out(document), "chunk_count": chunk_count or 0}


def _allowed_roots(document: Document, source: Source | None) -> list[Path]:
    """Radici entro cui il file del documento può essere servito.

    - sorgente ``upload``: solo ``upload_dir``;
    - sorgente ``local_fs``: solo la radice ``config.path`` della sorgente;
    - altre sorgenti (gdrive, m365): nessun file locale servibile.
    """
    settings = get_settings()
    if source is None:
        return []
    if source.type == SourceType.upload.value:
        return [settings.upload_dir.resolve()]
    if source.type == SourceType.local_fs.value:
        root = (source.config_json or {}).get("path")
        if root:
            return [Path(root).expanduser().resolve()]
    return []


@router.get("/{document_id}/file")
async def get_document_file(
    document_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    """File originale del documento, servito inline (apribile con ``#page=N``).

    Accessibile a qualunque utente autenticato: il frontend lo scarica come
    blob autenticato e lo apre nel viewer. Per la sorgente ``upload`` il file
    è in ``upload_dir``; per ``local_fs`` è il path sorgente (``external_id``).

    Difesa in profondità: il path risolto deve restare dentro la radice
    consentita per la sorgente del documento, altrimenti 403 (``external_id``
    non è input utente diretto, ma il vincolo è verificato a ogni richiesta).
    """
    document = await _get_document(db, document_id)
    source = await db.get(Source, document.source_id)
    resolved = Path(document.external_id).resolve()
    roots = _allowed_roots(document, source)
    if not any(resolved == root or root in resolved.parents for root in roots):
        if roots:
            log.warning(
                "file serving negato: path fuori dalle radici consentite",
                document_id=document_id,
                path=document.external_id,
            )
            raise HTTPException(status_code=403, detail="File non accessibile")
        raise HTTPException(
            status_code=404, detail="File non disponibile per questa sorgente"
        )
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File non trovato")
    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    filename = document.title or resolved.stem
    if not filename.lower().endswith(resolved.suffix.lower()):
        filename += resolved.suffix
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Elimina documento, chunk, punti Qdrant e (se upload) il file (solo admin)."""
    document = await _get_document(db, document_id)
    source = await db.get(Source, document.source_id)
    title, external_id = document.title, document.external_id

    await pipeline._get_index().delete_document(document.id)
    # Rimozione esplicita: il cascade ORM farebbe lazy loading (vietato in async)
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    # Knowledge graph: mention, relation ed entità orfane del documento
    await delete_document_graph(document.id, session=db)
    await db.delete(document)
    await db.commit()

    # Il file su disco si elimina solo per la sorgente upload (mai gli
    # originali local_fs degli utenti), e solo se dentro upload_dir
    if source is not None and source.type == SourceType.upload.value:
        settings = get_settings()
        path = Path(external_id)
        try:
            if (
                path.is_file()
                and path.parent.resolve() == settings.upload_dir.resolve()
            ):
                path.unlink()
        except OSError as exc:
            log.warning("file upload non eliminato", path=external_id, error=str(exc))
    log.info(
        "documento eliminato via API",
        document_id=document_id,
        title=title,
        user_id=user.id,
    )
    return Response(status_code=204)
