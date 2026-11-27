"""Connettore Google Drive con supporto ai Drive condivisi (Shared Drives).

Configurazione in ``Source.config_json``::

    {
        "credentials_json": "<contenuto del service account JSON>",
        "folder_id": "ID cartella da indicizzare (My Drive o Shared Drive)",  // opzionale
        "shared_drive_id": "ID di un singolo Drive condiviso",                // opzionale
        "page_token_state": "..."                                  // gestito dal sistema
    }

Dipendenze opzionali: ``pip install virtualis-backend[gdrive]``
(``google-api-python-client``, ``google-auth``); l'immagine di produzione le
include. Senza ``credentials_json`` il costruttore solleva
:class:`ConnectorNotConfiguredError`; se mancano le librerie l'errore emerge
alla prima chiamata alle API (import lazy in :meth:`_drive`).

Comportamento:

- scope ``drive.readonly``; il service account deve essere aggiunto come
  Visualizzatore alle cartelle o come membro dei Drive condivisi;
- tutte le chiamate passano ``supportsAllDrives=True`` /
  ``includeItemsFromAllDrives=True``: senza ``folder_id`` si usa
  ``corpora="allDrives"`` (tutto ciò che il service account vede, Drive
  condivisi inclusi); con ``shared_drive_id`` si usa ``corpora="drive"``;
- con ``folder_id`` l'enumerazione è ricorsiva nelle sottocartelle e il
  titolo conserva il percorso relativo ("Cartella/file"); nelle modalità
  senza ``folder_id`` il listing è piatto e il titolo è il solo nome file
  (la risoluzione del percorso richiederebbe una chiamata per cartella);
- i Google Docs/Sheets/Slides sono esportati rispettivamente come
  DOCX/XLSX/PPTX; gli altri file sono scaricati così come sono
  (nota: l'endpoint ``files.export`` non prevede ``supportsAllDrives``,
  l'export funziona comunque per i file nei Drive condivisi);
- :meth:`changes` usa la Changes API con ``pageToken`` persistito in
  ``Source.state_json``: elementi nuovi/modificati + tombstone per i file
  eliminati (``removed: true``). Con ``folder_id`` la Changes API non è
  filtrabile per cartella, quindi si ricade sullo scan completo con diff
  nel chiamante (:meth:`sync`).
"""

from __future__ import annotations

import asyncio
import json
import uuid
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
from .base import (
    Connector,
    ConnectorNotConfiguredError,
    RemoteItem,
    Tombstone,
)
from .local_fs import SUPPORTED_EXTENSIONS

log = structlog.get_logger(__name__)

# MIME Google Workspace -> (MIME di export, estensione)
_GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

_FOLDER_MIME = "application/vnd.google-apps.folder"

_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, modifiedTime, size, trashed)"
)
_CHANGES_FIELDS = (
    "nextPageToken, newStartPageToken, "
    "changes(fileId, removed, file(id, name, mimeType, modifiedTime, size, trashed))"
)


def _parse_dt(value: str | None) -> datetime | None:
    """Converte il timestamp RFC3339 di Drive in datetime naive UTC."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _external_id(file_id: str) -> str:
    """External id stabile nel DB per un file di Drive."""
    return f"gdrive://{file_id}"


class GDriveConnector(Connector):
    """Connettore Google Drive in sola lettura (vedi docstring del modulo)."""

    name = "gdrive"

    def __init__(self, config: dict[str, Any], settings: Settings | None = None) -> None:
        super().__init__(config, settings)
        self._service = None
        credentials_json = self.config.get("credentials_json")
        if not credentials_json:
            raise ConnectorNotConfiguredError(
                "Connettore gdrive: manca 'credentials_json' (service account) "
                "nella configurazione della sorgente"
            )
        self._credentials_info: dict[str, Any] = (
            json.loads(credentials_json)
            if isinstance(credentials_json, str)
            else credentials_json
        )
        self.folder_id: str | None = self.config.get("folder_id")
        self.shared_drive_id: str | None = self.config.get("shared_drive_id")

    def _drive(self) -> Any:
        """Client Drive v3 (creato lazy, thread-safe per uso in executor).

        Gli import di google sono qui (e non nel costruttore) perché le
        librerie sono un extra opzionale: così il connettore è istanziabile
        (e testabile) anche senza; l'errore emerge alla prima chiamata.
        """
        if self._service is None:
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise ConnectorNotConfiguredError(
                    "Connettore gdrive: installa le dipendenze opzionali con "
                    "`uv pip install -e .[gdrive]`"
                ) from exc
            credentials = service_account.Credentials.from_service_account_info(
                self._credentials_info,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            self._service = build("drive", "v3", credentials=credentials)
        return self._service

    @staticmethod
    def _to_item(file: dict[str, Any], path_prefix: str = "") -> RemoteItem | None:
        if file.get("trashed") or file.get("mimeType") == _FOLDER_MIME:
            return None
        name = file.get("name", file["id"])
        title = f"{path_prefix}/{name}" if path_prefix else name
        return RemoteItem(
            external_id=file["id"],
            title=title[:256],
            modified_at=_parse_dt(file.get("modifiedTime")),
            size=int(file.get("size", 0) or 0),
            meta={"mime_type": file.get("mimeType", "")},
        )

    def _list_folder_recursive(self, service: Any) -> list[RemoteItem]:
        """BFS dalla cartella radice; i titoli conservano il percorso relativo.

        La cache id→percorso è costruita durante la visita stessa: nessuna
        chiamata extra per risolvere i parent.
        """
        items: list[RemoteItem] = []
        queue: list[tuple[str, str]] = [(str(self.folder_id), "")]
        while queue:
            folder_id, prefix = queue.pop(0)
            page_token: str | None = None
            while True:
                response = (
                    service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=_LIST_FIELDS,
                        pageToken=page_token,
                        pageSize=1000,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                for file in response.get("files", []):
                    if file.get("mimeType") == _FOLDER_MIME:
                        name = file.get("name", file["id"])
                        sub_prefix = f"{prefix}/{name}" if prefix else name
                        queue.append((file["id"], sub_prefix))
                        continue
                    item = self._to_item(file, path_prefix=prefix)
                    if item is not None:
                        items.append(item)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        return items

    def _get_item(self, file_id: str) -> RemoteItem | None:
        """Metadati di un singolo file (usato per il retry dei failed)."""
        file = (
            self._drive()
            .files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, modifiedTime, size, trashed",
                supportsAllDrives=True,
            )
            .execute()
        )
        return self._to_item(file)

    async def enumerate(self) -> list[RemoteItem]:
        """Elenca i file: cartella (ricorsivo), singolo Shared Drive o tutto."""

        def _list() -> list[RemoteItem]:
            service = self._drive()
            if self.folder_id:
                return self._list_folder_recursive(service)

            kwargs: dict[str, Any] = {
                "q": "trashed = false",
                "fields": _LIST_FIELDS,
                "pageSize": 1000,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if self.shared_drive_id:
                kwargs["corpora"] = "drive"
                kwargs["driveId"] = self.shared_drive_id
            else:
                kwargs["corpora"] = "allDrives"

            items: list[RemoteItem] = []
            page_token: str | None = None
            while True:
                response = (
                    service.files().list(pageToken=page_token, **kwargs).execute()
                )
                for file in response.get("files", []):
                    item = self._to_item(file)
                    if item is not None:
                        items.append(item)
                page_token = response.get("nextPageToken")
                if not page_token:
                    return items

        return await asyncio.to_thread(_list)

    async def fetch(self, item: RemoteItem) -> Path:
        """Scarica (o esporta) il file in ``upload_dir/staging``."""
        from googleapiclient.http import MediaIoBaseDownload

        staging = self.settings.upload_dir / "staging"
        staging.mkdir(parents=True, exist_ok=True)

        mime_type = item.meta.get("mime_type", "")
        export = _GOOGLE_EXPORTS.get(mime_type)
        suffix = export[1] if export else Path(item.title).suffix or ".bin"
        dest = staging / f"{uuid.uuid4().hex}{suffix}"

        def _download() -> Path:
            service = self._drive()
            if export:
                # files.export non prevede supportsAllDrives (vedi docstring)
                request = service.files().export_media(
                    fileId=item.external_id, mimeType=export[0]
                )
            else:
                request = service.files().get_media(
                    fileId=item.external_id, supportsAllDrives=True
                )
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
            return dest

        log.info("download da Drive", external_id=item.external_id, title=item.title)
        return await asyncio.to_thread(_download)

    async def changes(
        self, state: dict[str, Any]
    ) -> tuple[list[RemoteItem | Tombstone], dict[str, Any]]:
        """Delta via Changes API; al primo giro scan completo + startPageToken.

        Con ``folder_id`` la Changes API non è filtrabile per cartella: si
        ricade sullo scan completo di default (diff nel chiamante).
        """
        if self.folder_id:
            return await super().changes(state)

        def _changes() -> tuple[list[RemoteItem | Tombstone], dict[str, Any]]:
            service = self._drive()
            drive_kwargs: dict[str, Any] = {"supportsAllDrives": True}
            if self.shared_drive_id:
                drive_kwargs["driveId"] = self.shared_drive_id
            page_token = state.get("page_token")
            if not page_token:
                # Prima sincronizzazione: token iniziale + enumerazione completa
                page_token = (
                    service.changes()
                    .getStartPageToken(**drive_kwargs)
                    .execute()["startPageToken"]
                )
                new_state = {"page_token": page_token}
                return [], new_state  # il chiamante farà enumerate()

            events: list[RemoteItem | Tombstone] = []
            while page_token:
                response = (
                    service.changes()
                    .list(
                        pageToken=page_token,
                        fields=_CHANGES_FIELDS,
                        pageSize=1000,
                        includeItemsFromAllDrives=True,
                        **drive_kwargs,
                    )
                    .execute()
                )
                for change in response.get("changes", []):
                    if change.get("removed") or change.get("file", {}).get("trashed"):
                        events.append(Tombstone(external_id=change["fileId"]))
                    else:
                        item = self._to_item(change["file"])
                        if item is not None:
                            events.append(item)
                page_token = response.get("nextPageToken")
                if not page_token:
                    new_token = response.get("newStartPageToken") or state.get(
                        "page_token"
                    )
            return events, {"page_token": new_token}

        events, new_state = await asyncio.to_thread(_changes)
        if not events and "page_token" in new_state and "page_token" not in state:
            # Primo giro: scan completo con il token appena salvato
            items = await self.enumerate()
            return list(items), new_state
        return events, new_state

    @staticmethod
    def _is_supported(item: RemoteItem) -> bool:
        """Vero se la pipeline sa lavorare il file (export Google o estensione)."""
        if item.meta.get("mime_type", "") in _GOOGLE_EXPORTS:
            return True
        return Path(item.title).suffix.lower() in SUPPORTED_EXTENSIONS

    async def sync(
        self,
        source: Source,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> dict[str, Any]:
        """Sincronizza la sorgente: download/ingest dei nuovi/modificati, cancellazioni.

        - senza ``folder_id``: delta via Changes API (tombstone inclusi), con
          scan completo al primo giro; il ``page_token`` è persistito in
          ``Source.state_json``;
        - con ``folder_id``: scan completo a ogni giro, cancellazioni per
          diff con i Document esistenti;
        - il dedup sui contenuti è quello della pipeline (``content_hash``):
          un file già ``indexed`` e identico non viene reindicizzato; qui si
          evita anche il download se ``modifiedTime`` non è cambiato;
        - i documenti ``failed`` della sorgente vengono SEMPRE ritentati: in
          modalità delta la Changes API non li restituisce (nessun cambiamento
          remoto) e resterebbero failed per sempre anche se il fallimento era
          transitorio (rete, memoria, throttling). Se il problema è permanente
          torneranno failed senza loop: il retry avviene solo quando una sync
          viene esplicitamente richiesta.

        Ritorna un report con conteggi ed elenchi per ispezione.
        """
        state = dict(source.state_json or {})
        # Scan completo = si può fare il diff per le cancellazioni; con la
        # Changes API un delta vuoto è invece del tutto normale.
        full_scan = self.folder_id is not None or not state.get("page_token")
        events, new_state = await self.changes(state)

        items = [e for e in events if isinstance(e, RemoteItem)]
        tombstones = [e for e in events if isinstance(e, Tombstone)]
        supported = [item for item in items if self._is_supported(item)]

        async with session_factory() as db:
            result = await db.execute(
                select(Document).where(Document.source_id == source.id)
            )
            existing = list(result.scalars())

        # Guardia anti-svuotamento (come local_fs): se lo scan completo non
        # trova NULLA ma il DB ha documenti, con ogni probabilità sono state
        # revocate le condivisioni o le credenziali: meglio fermarsi che
        # cancellare l'intero indice della sorgente.
        if full_scan and not supported and existing:
            raise ConnectorNotConfiguredError(
                "Connettore gdrive: la scansione non ha trovato alcun file ma "
                f"la sorgente ha {len(existing)} documenti indicizzati; "
                "sincronizzazione annullata per sicurezza (condivisioni o "
                "credenziali revocate?)"
            )

        by_ext = {doc.external_id: doc for doc in existing}
        report: dict[str, Any] = {
            "scanned": len(items),
            "ingested": [],
            "unchanged": 0,
            "unsupported": len(items) - len(supported),
            "deleted": [],
            "errors": [],
        }

        # Ingest di nuovi/modificati
        for item in supported:
            external_id = _external_id(item.external_id)
            document = by_ext.get(external_id)
            if (
                document is not None
                and document.status == DocumentStatus.indexed.value
                and item.modified_at is not None
                and document.modified_at == item.modified_at
            ):
                report["unchanged"] += 1
                continue
            try:
                path = await self.fetch(item)
            except Exception as exc:
                report["errors"].append(
                    {"external_id": external_id, "error": str(exc)}
                )
                continue
            await pipeline.ingest_file(
                path, source.id, external_id, session_factory,
                doc_meta={"title": item.title, "modified_at": item.modified_at},
            )
            report["ingested"].append(external_id)

        # Retry dei documenti ``failed``: in modalità delta la Changes API non
        # li restituisce (nessun cambiamento remoto), quindi senza questo
        # passaggio resterebbero failed per sempre. I metadati vengono letti
        # freschi da Drive così l'export dei Google Docs nativi funziona.
        covered = {_external_id(item.external_id) for item in supported}
        retried = 0
        for document in existing:
            if (
                document.status != DocumentStatus.failed.value
                or document.external_id in covered
            ):
                continue
            file_id = document.external_id.removeprefix("gdrive://")
            try:
                retry_item: RemoteItem | None = await asyncio.to_thread(
                    self._get_item, file_id
                )
            except Exception as exc:
                report["errors"].append(
                    {"external_id": document.external_id, "error": str(exc)}
                )
                continue
            if retry_item is None or not self._is_supported(retry_item):
                continue
            try:
                path = await self.fetch(retry_item)
            except Exception as exc:
                report["errors"].append(
                    {"external_id": document.external_id, "error": str(exc)}
                )
                continue
            await pipeline.ingest_file(
                path, source.id, document.external_id, session_factory,
                doc_meta={
                    "title": retry_item.title,
                    "modified_at": retry_item.modified_at,
                },
            )
            report["ingested"].append(document.external_id)
            retried += 1
        report["retried_failed"] = retried

        # Cancellazioni: tombstone della Changes API + (solo su scan completo)
        # documenti del DB non più presenti nella scansione
        to_delete: dict[str, Document] = {}
        for tomb in tombstones:
            external_id = _external_id(tomb.external_id)
            if external_id in by_ext:
                to_delete[external_id] = by_ext[external_id]
        if full_scan:
            scanned_ids = {_external_id(item.external_id) for item in supported}
            for external_id, document in by_ext.items():
                if external_id not in scanned_ids:
                    to_delete.setdefault(external_id, document)

        index = pipeline._get_index()
        for external_id, document in to_delete.items():
            await index.delete_document(document.id)
            async with session_factory() as db:
                row = await db.get(Document, document.id)
                if row is not None:
                    # Knowledge graph: mention/relation ed entità orfane
                    await delete_document_graph(row.id, session=db)
                    await db.delete(row)  # cascade: elimina anche i chunk
                    await db.commit()
            report["deleted"].append(external_id)
            log.info(
                "documento eliminato (sparito da Drive)", document_id=document.id
            )

        # Stato della sincronizzazione sulla sorgente (page_token incluso)
        async with session_factory() as db:
            source_row = await db.get(Source, source.id)
            if source_row is not None:
                source_row.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
                source_row.state_json = dict(new_state)
                await db.commit()

        return report
