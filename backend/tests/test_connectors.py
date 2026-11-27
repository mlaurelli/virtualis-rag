"""Test dei connettori: guard di configurazione e contratto base.

Nessuna chiamata alle API reali: gdrive/m365 sono verificati solo per
import e comportamento in assenza di credenziali.
"""

import pytest

from app.connectors.base import (
    Connector,
    ConnectorNotConfiguredError,
    RemoteItem,
    Tombstone,
)
from app.connectors.gdrive import GDriveConnector
from app.connectors.local_fs import LocalFsConnector
from app.connectors.m365 import M365Connector


def test_gdrive_senza_credenziali() -> None:
    """Senza credentials_json il connettore Drive rifiuta la configurazione."""
    with pytest.raises(ConnectorNotConfiguredError, match="credentials_json"):
        GDriveConnector({})


def test_m365_senza_credenziali() -> None:
    """Senza tenant/client/secret/drive il connettore M365 elenca cosa manca."""
    with pytest.raises(ConnectorNotConfiguredError, match="tenant_id"):
        M365Connector({})
    with pytest.raises(ConnectorNotConfiguredError, match="drive_id"):
        M365Connector(
            {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
        )


def test_local_fs_senza_path() -> None:
    """Senza 'path' il connettore locale rifiuta la configurazione."""
    with pytest.raises(ConnectorNotConfiguredError, match="path"):
        LocalFsConnector({})


def test_contratto_dati() -> None:
    """RemoteItem/Tombstone sono i tipi del contratto dei connettori."""
    item = RemoteItem(external_id="id-1", title="Doc", size=10)
    assert item.meta == {}
    assert item.modified_at is None
    tomb = Tombstone(external_id="id-2")
    assert tomb.external_id == "id-2"


async def test_changes_default_scan_completo(tmp_settings) -> None:
    """L'implementazione di default di changes() fa uno scan completo."""

    class DummyConnector(Connector):
        name = "dummy"

        async def enumerate(self):
            return [RemoteItem(external_id="a", title="A")]

        async def fetch(self, item):  # pragma: no cover - non usato qui
            raise NotImplementedError

    connector = DummyConnector({})
    events, state = await connector.changes({"old": True})
    assert len(events) == 1
    assert isinstance(events[0], RemoteItem)
    assert state == {"old": True}


async def test_local_fs_enumerate(tmp_settings, tmp_path) -> None:
    """Il connettore locale enumera solo i formati supportati, ricorsivamente."""
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.md").write_text("# A\n\ntesto\n", encoding="utf-8")
    (folder / "sub" / "b.txt").write_text("testo\n", encoding="utf-8")
    (folder / "c.exe").write_bytes(b"MZ")  # formato non supportato
    (folder / ".hidden.md").write_text("# H\n", encoding="utf-8")

    connector = LocalFsConnector({"path": str(folder)})
    items = await connector.enumerate()
    names = sorted(item.title for item in items)
    # Il titolo conserva il percorso relativo delle sottocartelle
    assert names == [".hidden", "a", "sub/b"]
    for item in items:
        assert item.size > 0
        assert item.modified_at is not None
    # fetch restituisce il path originale (nessuna copia)
    path = await connector.fetch(items[0])
    assert path.exists()


async def test_local_fs_sync_guardia_cartella_vuota(tmp_settings, tmp_path) -> None:
    """Scansione vuota + documenti nel DB = mount assente: la sync si ferma.

    Protegge dal caso reale di una share SMB smontata: senza guardia la sync
    interpreterebbe il vuoto come 'tutti i file cancellati' e svuoterebbe la
    sorgente dall'indice.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import pytest

    from app.connectors.base import ConnectorNotConfiguredError

    folder = tmp_path / "mount-vuoto"
    folder.mkdir()
    connector = LocalFsConnector({"path": str(folder)})

    # Sessione finta che restituisce un documento esistente per la sorgente
    doc = SimpleNamespace(id=1, external_id="/vecchio/file.pdf", content_hash="x")
    result = MagicMock()
    result.scalars.return_value = [doc]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    source = SimpleNamespace(id=7)
    # La guardia scatta prima di qualunque accesso all'indice vettoriale
    with pytest.raises(ConnectorNotConfiguredError, match="vuota"):
        await connector.sync(source, factory)  # type: ignore[arg-type]
