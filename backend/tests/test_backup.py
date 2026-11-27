"""Test dello script di backup/ripristino (tutto su directory temporanee)."""

import io
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from scripts import backup  # noqa: E402


def _settings(data_dir: Path, **kwargs) -> Settings:
    return Settings(
        data_dir=data_dir, jwt_secret="x" * 40, _env_file=None, **kwargs
    )  # type: ignore[call-arg]


def _make_db(db_path: Path, docs: int, chunks: int, users: int) -> None:
    """SQLite minimo con lo schema delle tabelle conteggiate dal manifest."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.executemany(
        "INSERT INTO documents (title) VALUES (?)", [(f"doc{i}",) for i in range(docs)]
    )
    conn.executemany(
        "INSERT INTO chunks (text) VALUES (?)", [(f"chunk{i}",) for i in range(chunks)]
    )
    conn.executemany(
        "INSERT INTO users (username) VALUES (?)", [(f"user{i}",) for i in range(users)]
    )
    conn.commit()
    conn.close()


def _populated_data_dir(tmp_path: Path) -> Path:
    """data_dir finta ma realistica: DB, uploads, qdrant embedded, .jwt_secret."""
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    (data_dir / "qdrant").mkdir()
    _make_db(data_dir / "virtualis.db", docs=3, chunks=7, users=2)
    (data_dir / "uploads" / "contratto.pdf").write_bytes(b"%PDF-finto")
    (data_dir / "qdrant" / "meta.json").write_text("{}", encoding="utf-8")
    (data_dir / ".jwt_secret").write_text("segreto-di-prova\n", encoding="utf-8")
    return data_dir


def _titles(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM documents ORDER BY id")]
    finally:
        conn.close()


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    """backup → restore su tmp: il DB ripristinato ha gli stessi record."""
    data_dir = _populated_data_dir(tmp_path)
    settings = _settings(data_dir)
    out_dir = tmp_path / "backups"

    archive = backup.create_backup(settings=settings, output_dir=out_dir)
    assert archive.is_file()

    manifest = backup.read_manifest(archive)
    assert manifest["counts"] == {"documents": 3, "chunks": 7, "users": 2}
    assert manifest["qdrant"] == {"mode": "embedded", "included": True}
    assert manifest["app_version"]
    assert manifest["created_at"]

    # Ripristino su una data_dir diversa (settings separate)
    restored_dir = tmp_path / "ripristino"
    backup.restore_backup(archive, _settings(restored_dir), assume_yes=True)

    assert _titles(restored_dir / "virtualis.db") == _titles(
        data_dir / "virtualis.db"
    )
    assert (restored_dir / "uploads" / "contratto.pdf").read_bytes() == b"%PDF-finto"
    assert (restored_dir / ".jwt_secret").read_text(encoding="utf-8") == (
        "segreto-di-prova\n"
    )
    assert (restored_dir / "qdrant" / "meta.json").is_file()


def test_backup_qdrant_server_esclude_indice_e_lo_dichiara(tmp_path: Path) -> None:
    """Con QDRANT_URL valorizzato: niente qdrant/ nell'archivio, manifest esplicito."""
    data_dir = _populated_data_dir(tmp_path)
    settings = _settings(data_dir, qdrant_url="http://qdrant.lan:6333")

    archive = backup.create_backup(settings=settings, output_dir=tmp_path / "backups")
    manifest = backup.read_manifest(archive)
    assert manifest["qdrant"]["mode"] == "server"
    assert manifest["qdrant"]["included"] is False
    assert manifest["qdrant"]["url"] == "http://qdrant.lan:6333"

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert not any(n.startswith("qdrant") for n in names)
    assert "virtualis.db" in names and "manifest.json" in names


def test_restore_senza_conferma_non_tocca_nulla(tmp_path: Path) -> None:
    """Risposta negativa alla conferma: RestoreAbortedError, dati intatti."""
    data_dir = _populated_data_dir(tmp_path)
    archive = backup.create_backup(
        settings=_settings(data_dir), output_dir=tmp_path / "backups"
    )
    target = tmp_path / "ripristino"
    with pytest.raises(backup.RestoreAbortedError):
        backup.restore_backup(
            archive, _settings(target), assume_yes=False, confirm=lambda _: "n"
        )
    assert not target.exists()


def test_restore_sposta_la_data_dir_esistente(tmp_path: Path) -> None:
    """Il ripristino non cancella: la data_dir corrente è salvata altrove."""
    data_dir = _populated_data_dir(tmp_path)
    archive = backup.create_backup(
        settings=_settings(data_dir), output_dir=tmp_path / "backups"
    )
    # Simula una data_dir "viva" diversa dal backup
    (data_dir / "uploads" / "contratto.pdf").write_bytes(b"versione-nuova")
    backup.restore_backup(archive, _settings(data_dir), assume_yes=True)

    salvati = list(tmp_path.glob("data.pre-restore-*"))
    assert len(salvati) == 1
    assert (salvati[0] / "uploads" / "contratto.pdf").read_bytes() == b"versione-nuova"
    assert (data_dir / "uploads" / "contratto.pdf").read_bytes() == b"%PDF-finto"


def test_restore_archivio_senza_manifest(tmp_path: Path) -> None:
    """Un tar.gz qualunque non è un backup Virtualis: errore chiaro."""
    finto = tmp_path / "finto.tar.gz"
    with tarfile.open(finto, "w:gz") as tar:
        info = tarfile.TarInfo("file.txt")
        payload = b"ciao"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="manifest"):
        backup.restore_backup(
            finto, _settings(tmp_path / "ripristino"), assume_yes=True
        )


def test_list_backups(tmp_path: Path) -> None:
    """list mostra gli archivi con i conteggi del manifest."""
    data_dir = _populated_data_dir(tmp_path)
    out_dir = tmp_path / "backups"
    backup.create_backup(settings=_settings(data_dir), output_dir=out_dir)

    backups = backup.list_backups(output_dir=out_dir)
    assert len(backups) == 1
    assert backups[0]["manifest"]["counts"]["documents"] == 3
    assert backups[0]["size_bytes"] > 0

    # directory inesistente: lista vuota, nessun errore
    assert backup.list_backups(output_dir=tmp_path / "assente") == []
