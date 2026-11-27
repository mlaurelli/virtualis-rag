"""Connettore per cartelle locali o NAS montati.

Configurazione (``Source.config_json``)::

    {"path": "/percorso/della/cartella", "glob": "**/*"}

La scansione è ricorsiva e limitata ai formati supportati. Il confronto sul
``content_hash`` (sha256) distingue le modifiche reali dai semplici touch:

- file nuovo o hash diverso → ingest;
- stesso hash su path diverso → rinomina: si aggiorna ``external_id`` senza
  reindicizzare;
- file sparito → cancellazione dall'indice e dal DB.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..ingestion import pipeline
from ..knowledge.extractor import delete_document_graph
from ..models import Document, DocumentStatus, Source
from .base import Connector, ConnectorNotConfiguredError, RemoteItem

log = structlog.get_logger(__name__)

# Formati gestiti dalla pipeline di ingestion
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".md", ".markdown",
    ".txt", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".asciidoc",
}


class LocalFsConnector(Connector):
    """Connettore per una cartella del filesystem locale (o NAS montato)."""

    name = "local_fs"

    def __init__(self, config: dict[str, Any], settings: Settings | None = None) -> None:
        super().__init__(config, settings)
        root = self.config.get("path")
        if not root:
            raise ConnectorNotConfiguredError(
                "Connettore local_fs: manca 'path' nella configurazione della sorgente"
            )
        self.root = Path(root).expanduser()
        self.pattern = self.config.get("glob", "**/*")

    async def enumerate(self) -> list[RemoteItem]:
        """Scansione ricorsiva dei formati supportati sotto ``root``."""
        if not self.root.is_dir():
            raise ConnectorNotConfiguredError(
                f"Connettore local_fs: la cartella {self.root} non esiste"
            )

        def _scan() -> list[RemoteItem]:
            items: list[RemoteItem] = []
            for path in sorted(self.root.glob(self.pattern)):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                stat = path.stat()
                # Titolo = percorso relativo senza estensione ("Cartella/file"):
                # conserva la struttura delle sottocartelle (come l'import M365).
                # Path() scarta da sé il "." per i file nella radice.
                relative = path.relative_to(self.root)
                title = str(relative.parent / relative.stem)
                items.append(
                    RemoteItem(
                        external_id=str(path),
                        title=title[:256],
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=UTC
                        ).replace(tzinfo=None),
                        size=stat.st_size,
                    )
                )
            return items

        return await asyncio.to_thread(_scan)

    async def fetch(self, item: RemoteItem) -> Path:
        """Il file è già locale: nessuna copia, si usa il path originale."""
        return Path(item.external_id)

    async def sync(
        self,
        source: Source,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, Any]:
        """Sincronizza la sorgente: ingest di nuovi/modificati, rinomine, cancellazioni.

        Ritorna un report con conteggi ed elenchi per ispezione.
        """
        items = await self.enumerate()
        by_external_id = {item.external_id: item for item in items}

        async with session_factory() as db:
            result = await db.execute(
                select(Document).where(Document.source_id == source.id)
            )
            existing = list(result.scalars())

        # Guardia anti-svuotamento: se la scansione non trova NULLA ma il DB ha
        # documenti, con ogni probabilità la cartella è un mount di rete non
        # disponibile (share smontata, NAS spento): meglio fermarsi che
        # cancellare l'intero indice della sorgente.
        if not items and existing:
            raise ConnectorNotConfiguredError(
                f"Connettore local_fs: la cartella {self.root} è vuota ma la "
                f"sorgente ha {len(existing)} documenti indicizzati; "
                "sincronizzazione annullata per sicurezza (mount non disponibile?)"
            )

        by_path = {doc.external_id: doc for doc in existing}
        by_hash = {
            doc.content_hash: doc for doc in existing if doc.content_hash
        }

        report: dict[str, Any] = {
            "scanned": len(items),
            "ingested": [],
            "renamed": [],
            "unchanged": 0,
            "deleted": [],
            "errors": [],
        }

        # Ingest di nuovi/modificati e rinomine
        for external_id, item in by_external_id.items():
            document = by_path.get(external_id)
            try:
                file_hash = await asyncio.to_thread(
                    pipeline.content_hash, Path(external_id)
                )
            except OSError as exc:
                report["errors"].append({"external_id": external_id, "error": str(exc)})
                continue

            if document is not None:
                if document.content_hash == file_hash:
                    report["unchanged"] += 1
                    continue
                # Modifica reale: re-ingest (la pipeline sostituisce i chunk)
                await pipeline.ingest_file(
                    Path(external_id), source.id, external_id, session_factory,
                    doc_meta={"title": item.title, "modified_at": item.modified_at},
                )
                report["ingested"].append(external_id)
                continue

            # Rinomina? Stesso hash su un documento il cui file è sparito
            renamed = by_hash.get(file_hash)
            if (
                renamed is not None
                and renamed.external_id not in by_external_id
                and renamed.status == DocumentStatus.indexed.value
            ):
                async with session_factory() as db:
                    row = await db.get(Document, renamed.id)
                    if row is not None:
                        row.external_id = external_id
                        row.title = item.title
                        row.modified_at = item.modified_at
                        await db.commit()
                by_path.pop(renamed.external_id, None)
                by_path[external_id] = renamed
                report["renamed"].append(
                    {"from": renamed.external_id, "to": external_id}
                )
                log.info(
                    "rinomina rilevata (stesso hash), nessuna reindicizzazione",
                    document_id=renamed.id,
                )
                continue

            await pipeline.ingest_file(
                Path(external_id), source.id, external_id, session_factory,
                doc_meta={"title": item.title, "modified_at": item.modified_at},
            )
            report["ingested"].append(external_id)

        # Cancellazioni: documenti nel DB il cui file non esiste più
        index = pipeline._get_index()
        for external_id, document in by_path.items():
            if external_id in by_external_id:
                continue
            await index.delete_document(document.id)
            async with session_factory() as db:
                row = await db.get(Document, document.id)
                if row is not None:
                    # Knowledge graph: mention/relation ed entità orfane
                    await delete_document_graph(row.id, session=db)
                    await db.delete(row)  # cascade: elimina anche i chunk
                    await db.commit()
            report["deleted"].append(external_id)
            log.info("documento eliminato (file sparito)", document_id=document.id)

        # Stato della sincronizzazione sulla sorgente
        async with session_factory() as db:
            source_row = await db.get(Source, source.id)
            if source_row is not None:
                source_row.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
                source_row.state_json = {"file_count": len(items)}
                await db.commit()

        return report
