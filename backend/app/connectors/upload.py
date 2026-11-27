"""Connettore per i file caricati via API (upload diretto).

I file sono salvati in ``upload_dir`` con nome sicuro (UUID + estensione
originale, mai il nome fornito dal client) e registrati sulla sorgente
"upload" di default, creata al primo utilizzo.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings, get_settings
from ..ingestion import pipeline
from ..models import Source, SourceType

log = structlog.get_logger(__name__)

UPLOAD_SOURCE_NAME = "upload"


async def get_or_create_upload_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Id della sorgente "upload" di default (creata se assente)."""
    async with session_factory() as db:
        result = await db.execute(
            select(Source).where(
                Source.type == SourceType.upload.value,
                Source.name == UPLOAD_SOURCE_NAME,
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            source = Source(
                name=UPLOAD_SOURCE_NAME,
                type=SourceType.upload.value,
                config_json={},
            )
            db.add(source)
            await db.commit()
            await db.refresh(source)
        return source.id


async def save_upload(
    data: bytes,
    filename: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
) -> int:
    """Salva un file caricato e avvia l'ingest. Ritorna il ``document_id``.

    Il nome su disco è ``<uuid4><estensione originale>``: il nome originale
    è usato solo come titolo del documento.
    """
    settings = settings or get_settings()
    upload_dir = settings.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Estensione originale (solo l'ultimo suffisso, sanitizzato)
    suffix = Path(filename).suffix.lower()
    safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:16]
    safe_name = f"{uuid.uuid4().hex}{safe_suffix}"
    dest = upload_dir / safe_name
    dest.write_bytes(data)
    log.info("upload salvato", path=str(dest), original=filename, size=len(data))

    source_id = await get_or_create_upload_source(session_factory)
    document_id = await pipeline.ingest_file(
        dest,
        source_id,
        str(dest),
        session_factory,
        doc_meta={"title": Path(filename).stem or safe_name},
    )
    return document_id
