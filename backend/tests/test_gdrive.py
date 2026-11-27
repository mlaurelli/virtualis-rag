"""Test del connettore Google Drive con supporto Shared Drives.

Nessuna chiamata alle API reali: il client Drive è un MagicMock iniettato in
``connector._service`` e i test asseriscono i kwargs delle chiamate (i flag
``supportsAllDrives``/``includeItemsFromAllDrives``/``corpora``/``driveId``).
Le librerie google (extra opzionale) NON servono: gli import sono lazy.
"""

import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.connectors.base import ConnectorNotConfiguredError, RemoteItem, Tombstone
from app.connectors.gdrive import GDriveConnector
from app.db import get_session_factory, init_db
from app.ingestion import pipeline
from app.models import Document, DocumentStatus, Source

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _connector(config: dict | None = None) -> GDriveConnector:
    cfg: dict = {"credentials_json": {"type": "service_account"}}
    cfg.update(config or {})
    return GDriveConnector(cfg)


def _file(file_id: str, name: str, mime: str = "application/pdf", **extra) -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": "2026-01-01T00:00:00Z",
        "size": "10",
        **extra,
    }


@pytest.fixture
def fake_media(monkeypatch):
    """Modulo googleapiclient.http finto: il downloader scrive byte fissi."""

    class _FakeDownloader:
        def __init__(self, fh, request):
            self._fh = fh

        def next_chunk(self):
            self._fh.write(b"contenuto scaricato")
            return None, True

    http_mod = types.ModuleType("googleapiclient.http")
    http_mod.MediaIoBaseDownload = _FakeDownloader
    pkg = types.ModuleType("googleapiclient")
    pkg.http = http_mod
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


async def test_enumerate_all_drives() -> None:
    """Senza folder_id: corpora=allDrives + flag Shared Drives; scarta cartelle
    e cestinati."""
    connector = _connector()
    service = MagicMock()
    connector._service = service
    files_list = service.files.return_value.list
    files_list.return_value.execute.return_value = {
        "files": [
            _file("f1", "Doc.pdf"),
            _file("d1", "Cartella", mime=_FOLDER_MIME),
            _file("f2", "Cestinato.pdf", trashed=True),
        ]
    }

    items = await connector.enumerate()

    assert [i.external_id for i in items] == ["f1"]
    assert items[0].title == "Doc.pdf"
    assert items[0].modified_at == datetime(2026, 1, 1)
    kwargs = files_list.call_args.kwargs
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True
    assert kwargs["corpora"] == "allDrives"
    assert "driveId" not in kwargs


async def test_enumerate_shared_drive_scoping() -> None:
    """Con shared_drive_id: corpora=drive + driveId, sempre con i due flag."""
    connector = _connector({"shared_drive_id": "SD1"})
    service = MagicMock()
    connector._service = service
    files_list = service.files.return_value.list
    files_list.return_value.execute.return_value = {"files": [_file("f1", "A.pdf")]}

    items = await connector.enumerate()

    assert len(items) == 1
    kwargs = files_list.call_args.kwargs
    assert kwargs["corpora"] == "drive"
    assert kwargs["driveId"] == "SD1"
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True


async def test_enumerate_folder_ricorsivo_con_percorso() -> None:
    """Con folder_id: ricorsione nelle sottocartelle e titolo col percorso."""
    connector = _connector({"folder_id": "root123"})
    service = MagicMock()
    connector._service = service
    files_list = service.files.return_value.list
    files_list.return_value.execute.side_effect = [
        # Contenuto di root123: un file e una sottocartella
        {"files": [_file("f1", "A.pdf"), _file("sub1", "Progetti", mime=_FOLDER_MIME)]},
        # Contenuto di sub1
        {"files": [_file("f2", "B.pdf")]},
    ]

    items = await connector.enumerate()

    assert [i.title for i in items] == ["A.pdf", "Progetti/B.pdf"]
    first, second = files_list.call_args_list
    assert first.kwargs["q"] == "'root123' in parents and trashed = false"
    assert second.kwargs["q"] == "'sub1' in parents and trashed = false"
    for call in (first, second):
        assert call.kwargs["supportsAllDrives"] is True
        assert call.kwargs["includeItemsFromAllDrives"] is True


# ---------------------------------------------------------------------------
# fetch (download/export)
# ---------------------------------------------------------------------------


async def test_fetch_export_google_doc(tmp_settings, fake_media) -> None:
    """Google Doc → export in DOCX (files.export non prevede supportsAllDrives)."""
    connector = _connector()
    service = MagicMock()
    connector._service = service
    item = RemoteItem(
        external_id="doc1",
        title="Verbale",
        meta={"mime_type": "application/vnd.google-apps.document"},
    )

    path = await connector.fetch(item)

    assert path.suffix == ".docx"
    assert path.read_bytes() == b"contenuto scaricato"
    kwargs = service.files.return_value.export_media.call_args.kwargs
    assert kwargs == {"fileId": "doc1", "mimeType": _DOCX_MIME}
    service.files.return_value.get_media.assert_not_called()


async def test_fetch_download_binario_supports_all_drives(
    tmp_settings, fake_media
) -> None:
    """File non Google → get_media con supportsAllDrives=True."""
    connector = _connector()
    service = MagicMock()
    connector._service = service
    item = RemoteItem(
        external_id="f1", title="Report.pdf", meta={"mime_type": "application/pdf"}
    )

    path = await connector.fetch(item)

    assert path.suffix == ".pdf"
    kwargs = service.files.return_value.get_media.call_args.kwargs
    assert kwargs == {"fileId": "f1", "supportsAllDrives": True}


# ---------------------------------------------------------------------------
# changes
# ---------------------------------------------------------------------------


async def test_changes_delta_con_tombstone() -> None:
    """Delta: flag Shared Drives sulla list, tombstone per removed/trashed."""
    connector = _connector()
    service = MagicMock()
    connector._service = service
    changes_list = service.changes.return_value.list
    changes_list.return_value.execute.return_value = {
        "changes": [
            {"fileId": "gone", "removed": True},
            {"fileId": "f1", "file": _file("f1", "Nuovo.pdf")},
        ],
        "newStartPageToken": "T2",
    }

    events, new_state = await connector.changes({"page_token": "T1"})

    assert new_state == {"page_token": "T2"}
    assert isinstance(events[0], Tombstone)
    assert events[0].external_id == "gone"
    assert isinstance(events[1], RemoteItem)
    kwargs = changes_list.call_args.kwargs
    assert kwargs["pageToken"] == "T1"
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True


async def test_changes_primo_giro_scan_completo() -> None:
    """Senza page_token: startPageToken (con driveId se scoping) + enumerate."""
    connector = _connector({"shared_drive_id": "SD1"})
    service = MagicMock()
    connector._service = service
    get_start = service.changes.return_value.getStartPageToken
    get_start.return_value.execute.return_value = {"startPageToken": "T1"}
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [_file("f1", "A.pdf")]
    }

    events, new_state = await connector.changes({})

    assert new_state == {"page_token": "T1"}
    assert [e.external_id for e in events] == ["f1"]
    assert get_start.call_args.kwargs == {
        "supportsAllDrives": True,
        "driveId": "SD1",
    }


# ---------------------------------------------------------------------------
# sync end-to-end (pipeline mockata, DB reale su tmp_path)
# ---------------------------------------------------------------------------


async def _setup_db(state_json: dict) -> tuple:
    """DB su tmp_path: sorgente gdrive + due documenti già indicizzati."""
    await init_db()
    sf = get_session_factory()
    async with sf() as db:
        source = Source(name="drive", type="gdrive", config_json={},
                        state_json=state_json)
        db.add(source)
        await db.commit()
        await db.refresh(source)
        docs = [
            Document(
                source_id=source.id, external_id="gdrive://gone", title="Vecchio",
                status=DocumentStatus.indexed.value, content_hash="h-gone",
            ),
            Document(
                source_id=source.id, external_id="gdrive://same", title="Stabile",
                status=DocumentStatus.indexed.value, content_hash="h-same",
                modified_at=datetime(2026, 1, 1),
            ),
        ]
        db.add_all(docs)
        await db.commit()
    return source, sf


async def test_sync_delta_end_to_end(tmp_settings, tmp_path, monkeypatch) -> None:
    """Delta: ingest del nuovo, skip dell'invariato, delete da tombstone,
    page_token e last_sync_at aggiornati sulla sorgente."""
    source, sf = await _setup_db({"page_token": "T1"})

    connector = _connector()
    service = MagicMock()
    connector._service = service
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": [
            {"fileId": "gone", "removed": True},
            {"fileId": "f1", "file": _file("f1", "Nuovo.pdf")},
            {"fileId": "same", "file": _file("same", "Stabile.pdf")},
        ],
        "newStartPageToken": "T2",
    }

    staged = tmp_path / "staged.pdf"
    staged.write_bytes(b"%PDF nuovo")

    async def fake_fetch(item):
        return staged

    connector.fetch = fake_fetch  # type: ignore[method-assign]

    ingest_calls = []

    async def fake_ingest(path, source_id, external_id, session_factory, *, doc_meta=None):
        ingest_calls.append((path, source_id, external_id, doc_meta))
        return 99

    fake_index = SimpleNamespace(delete_document=AsyncMock())
    monkeypatch.setattr(pipeline, "ingest_file", fake_ingest)
    monkeypatch.setattr(pipeline, "_get_index", lambda: fake_index)
    delete_graph = AsyncMock()
    monkeypatch.setattr("app.connectors.gdrive.delete_document_graph", delete_graph)

    report = await connector.sync(source, sf)

    # Nuovo file ingerito con external_id stabile e titolo nel doc_meta
    assert report["ingested"] == ["gdrive://f1"]
    assert ingest_calls[0][0] == staged
    assert ingest_calls[0][2] == "gdrive://f1"
    assert ingest_calls[0][3]["title"] == "Nuovo.pdf"
    # File con modifiedTime invariato: nessun download/reingest
    assert report["unchanged"] == 1
    # Tombstone: rimozione da indice, KG e DB
    assert report["deleted"] == ["gdrive://gone"]
    fake_index.delete_document.assert_awaited_once()
    delete_graph.assert_awaited_once()
    async with sf() as db:
        remaining = [
            d.external_id
            for d in (await db.execute(select(Document))).scalars()
        ]
        assert "gdrive://gone" not in remaining
        assert "gdrive://same" in remaining
        source_row = await db.get(Source, source.id)
        assert source_row.state_json == {"page_token": "T2"}
        assert source_row.last_sync_at is not None


async def test_sync_guardia_anti_svuotamento(tmp_settings, monkeypatch) -> None:
    """Scan completo vuoto + documenti nel DB = permessi revocati: la sync si
    ferma prima di toccare indice e DB (stessa guardia di local_fs)."""
    source, sf = await _setup_db({})  # nessun page_token → scan completo

    connector = _connector({"folder_id": "root123"})
    service = MagicMock()
    connector._service = service
    service.files.return_value.list.return_value.execute.return_value = {"files": []}

    index_mock = MagicMock()
    monkeypatch.setattr(pipeline, "_get_index", index_mock)

    with pytest.raises(ConnectorNotConfiguredError, match="annullata"):
        await connector.sync(source, sf)
    index_mock.assert_not_called()


async def test_sync_scan_completo_filtra_non_supportati(
    tmp_settings, tmp_path, monkeypatch
) -> None:
    """Scan completo: i formati non lavorabili sono contati come unsupported
    (e non scaricati), i Google Docs sono sempre supportati (export)."""
    source, sf = await _setup_db({})
    connector = _connector({"folder_id": "root123"})
    service = MagicMock()
    connector._service = service
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            _file("g1", "Verbale", mime="application/vnd.google-apps.document"),
            _file("z1", "Backup.zip", mime="application/zip"),
            _file("same", "Stabile.pdf"),
            _file("gone2", "Nuovo nome.pdf"),
        ]
    }

    fetched = []

    async def fake_fetch(item):
        fetched.append(item.external_id)
        path = tmp_path / f"{item.external_id}.bin"
        path.write_bytes(b"x")
        return path

    connector.fetch = fake_fetch  # type: ignore[method-assign]

    async def fake_ingest(path, source_id, external_id, session_factory, *, doc_meta=None):
        return 99

    fake_index = SimpleNamespace(delete_document=AsyncMock())
    monkeypatch.setattr(pipeline, "ingest_file", fake_ingest)
    monkeypatch.setattr(pipeline, "_get_index", lambda: fake_index)
    monkeypatch.setattr("app.connectors.gdrive.delete_document_graph", AsyncMock())

    report = await connector.sync(source, sf)

    assert report["unsupported"] == 1
    assert "z1" not in fetched
    # 'same' invariato (stesso modifiedTime) → non scaricato
    assert fetched == ["g1", "gone2"]
    # Diff su scan completo: gdrive://gone non è più su Drive → eliminato
    assert report["deleted"] == ["gdrive://gone"]


async def test_sync_riprova_documenti_failed(tmp_settings, tmp_path, monkeypatch) -> None:
    """Delta vuoto: i documenti failed della sorgente vengono riscaricati e
    riaccodati (metadati freschi da Drive), quelli indexed restano skip."""
    source, sf = await _setup_db({"page_token": "T1"})
    async with sf() as db:
        db.add(
            Document(
                source_id=source.id, external_id="gdrive://bad", title="Rotto.pdf",
                status=DocumentStatus.failed.value, content_hash="h-bad",
                error="boom",
            )
        )
        await db.commit()

    connector = _connector()
    service = MagicMock()
    connector._service = service
    # Delta senza cambiamenti
    service.changes.return_value.list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "T2",
    }
    # Metadati freschi per il retry del failed
    service.files.return_value.get.return_value.execute.return_value = _file(
        "bad", "Rotto.pdf"
    )

    staged = tmp_path / "staged.pdf"
    staged.write_bytes(b"%PDF ritentato")

    fetched = []

    async def fake_fetch(item):
        fetched.append(item.external_id)
        return staged

    connector.fetch = fake_fetch  # type: ignore[method-assign]

    ingest_calls = []

    async def fake_ingest(path, source_id, external_id, session_factory, *, doc_meta=None):
        ingest_calls.append((external_id, doc_meta))
        return 99

    fake_index = SimpleNamespace(delete_document=AsyncMock())
    monkeypatch.setattr(pipeline, "ingest_file", fake_ingest)
    monkeypatch.setattr(pipeline, "_get_index", lambda: fake_index)
    monkeypatch.setattr("app.connectors.gdrive.delete_document_graph", AsyncMock())

    report = await connector.sync(source, sf)

    assert report["retried_failed"] == 1
    assert report["ingested"] == ["gdrive://bad"]
    assert fetched == ["bad"]
    assert ingest_calls[0][0] == "gdrive://bad"
    assert ingest_calls[0][1]["title"] == "Rotto.pdf"
    # Metadati letti col fileId grezzo (external_id senza prefisso)
    get_kwargs = service.files.return_value.get.call_args.kwargs
    assert get_kwargs["fileId"] == "bad"
    assert get_kwargs["supportsAllDrives"] is True


def test_gdrive_senza_credenziali_resta_bloccato() -> None:
    """Il guard sul config è invariato: senza credentials_json niente connettore."""
    with pytest.raises(ConnectorNotConfiguredError, match="credentials_json"):
        GDriveConnector({})


def test_route_sync_dispatch_include_gdrive() -> None:
    """POST /api/ingest/sync sa dispatchare anche le sorgenti gdrive."""
    from app.api.routes_ingest import _SYNC_CONNECTORS

    assert _SYNC_CONNECTORS.get("gdrive") is GDriveConnector
    assert "local_fs" in _SYNC_CONNECTORS
