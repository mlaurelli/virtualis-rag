#!/usr/bin/env python3
"""Backup e ripristino dei dati locali di Virtualis.

Il backup è un archivio ``.tar.gz`` del contenuto di ``data_dir``:

- ``virtualis.db`` (SQLite, copiato in modo consistente con l'API di backup
  di sqlite3, sicura anche a backend in esecuzione);
- ``uploads/`` (file caricati via API);
- ``qdrant/`` (indice vettoriale embedded);
- ``.jwt_secret`` e qualunque altro file presente.

In cima all'archivio c'è ``manifest.json`` con data, versione dell'app,
conteggi (documenti/chunk/utenti) e modalità Qdrant. Se Qdrant è in modalità
server (``QDRANT_URL`` valorizzato) i vettori NON sono inclusi: il manifest lo
dichiara e lo snapshot va fatto lato server (messaggio stampato a console).

Uso (dalla cartella ``backend/``)::

    uv run python scripts/backup.py backup [--output-dir PATH]
    uv run python scripts/backup.py restore backups/virtualis-backup-....tar.gz [--yes]
    uv run python scripts/backup.py list [--output-dir PATH]

La directory di default degli archivi è ``backups/`` accanto a ``data_dir``.
Codici di uscita: 0 = ok; 1 = errore; 2 = operazione annullata dall'utente.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Permette l'import di `app` anche senza installazione del package
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings, get_settings  # noqa: E402

MANIFEST_NAME = "manifest.json"
# File del DB live: nell'archivio va la copia consistente, non questi
_DB_FILES = {"virtualis.db", "virtualis.db-wal", "virtualis.db-shm", "virtualis.db-journal"}


class RestoreAbortedError(RuntimeError):
    """Ripristino annullato dall'utente alla richiesta di conferma."""


def _app_version() -> str:
    """Versione del package backend (fallback se non installato)."""
    try:
        from importlib.metadata import version

        return version("virtualis-backend")
    except Exception:
        return "0.1.0"


def _default_backups_dir(settings: Settings) -> Path:
    """Directory degli archivi: ``backups/`` accanto a ``data_dir``."""
    return settings.data_dir.parent / "backups"


def _snapshot_sqlite(db_path: Path, dest: Path) -> None:
    """Copia consistente del DB SQLite (API di backup di sqlite3)."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _table_counts(db_path: Path) -> dict[str, int]:
    """Conteggi delle tabelle principali (0 se il DB o la tabella manca)."""
    counts: dict[str, int] = {"documents": 0, "chunks": 0, "users": 0}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return counts
    try:
        for table in counts:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                counts[table] = 0
    finally:
        conn.close()
    return counts


def build_manifest(settings: Settings, db_snapshot: Path) -> dict[str, Any]:
    """Manifest del backup: data, versione, conteggi, modalità Qdrant."""
    server_mode = bool(settings.qdrant_url)
    qdrant: dict[str, Any] = {"mode": "server" if server_mode else "embedded"}
    if server_mode:
        qdrant.update(
            {
                "url": settings.qdrant_url,
                "included": False,
                "note": (
                    "Qdrant in modalità server: i vettori NON sono inclusi nel "
                    "backup. Usare gli snapshot nativi di Qdrant sul server."
                ),
            }
        )
    else:
        qdrant["included"] = True
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "app_version": _app_version(),
        "counts": _table_counts(db_snapshot),
        "qdrant": qdrant,
    }


def create_backup(
    settings: Settings | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Crea l'archivio di backup e restituisce il suo path."""
    settings = settings or get_settings()
    data_dir = settings.data_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data_dir inesistente: {data_dir}")
    out_dir = (output_dir or _default_backups_dir(settings)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_path = out_dir / f"virtualis-backup-{stamp}.tar.gz"

    server_mode = bool(settings.qdrant_url)
    snapshot = out_dir / f".snapshot-{stamp}.db"
    db_live = data_dir / "virtualis.db"
    try:
        if db_live.is_file():
            _snapshot_sqlite(db_live, snapshot)
        manifest = build_manifest(settings, snapshot if snapshot.is_file() else db_live)

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(
                _manifest_bytes_path(out_dir, stamp, manifest),
                arcname=MANIFEST_NAME,
            )
            if snapshot.is_file():
                tar.add(snapshot, arcname="virtualis.db")
            for path in sorted(data_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(data_dir)
                if rel.name in _DB_FILES:
                    continue  # sostituito dalla copia consistente
                if server_mode and rel.parts[0] == "qdrant":
                    continue  # Qdrant server: niente indice embedded
                tar.add(path, arcname=str(rel))
    finally:
        snapshot.unlink(missing_ok=True)
        (out_dir / f".manifest-{stamp}.json").unlink(missing_ok=True)

    if server_mode:
        print(
            "NOTA: Qdrant è in modalità server "
            f"({settings.qdrant_url}): il backup include solo SQLite e i file "
            "caricati. Per i vettori usare gli snapshot nativi di Qdrant."
        )
    print(f"Backup creato: {archive_path}")
    return archive_path


def _manifest_bytes_path(out_dir: Path, stamp: str, manifest: dict[str, Any]) -> Path:
    """Scrive il manifest su un file temporaneo da archiviare."""
    path = out_dir / f".manifest-{stamp}.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def read_manifest(archive_path: Path) -> dict[str, Any]:
    """Legge il manifest da un archivio di backup (errore se assente)."""
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember(MANIFEST_NAME)
        except KeyError:
            raise ValueError(
                f"{archive_path} non è un backup Virtualis: manifest.json assente"
            ) from None
        extracted = tar.extractfile(member)
        assert extracted is not None
        return json.loads(extracted.read().decode("utf-8"))


def restore_backup(
    archive_path: Path,
    settings: Settings | None = None,
    *,
    assume_yes: bool = False,
    confirm: Any = input,
) -> dict[str, Any]:
    """Ripristina ``data_dir`` da un archivio; restituisce il manifest.

    Senza ``assume_yes`` chiede conferma mostrando il manifest. Se ``data_dir``
    esiste e non è vuota è rinominata in ``<data_dir>.pre-restore-<timestamp>``
    (niente cancellazioni: il ripristino è sempre reversibile a mano).
    """
    settings = settings or get_settings()
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"archivio inesistente: {archive_path}")
    manifest = read_manifest(archive_path)
    counts = manifest.get("counts", {})
    print(
        "Backup del {created} (app {version}): {docs} documenti, "
        "{chunks} chunk, {users} utenti; Qdrant: {qmode}".format(
            created=manifest.get("created_at", "?"),
            version=manifest.get("app_version", "?"),
            docs=counts.get("documents", "?"),
            chunks=counts.get("chunks", "?"),
            users=counts.get("users", "?"),
            qmode=manifest.get("qdrant", {}).get("mode", "?"),
        )
    )
    data_dir = settings.data_dir.resolve()
    if not assume_yes:
        answer = confirm(f"Ripristinare {data_dir} da questo backup? [s/N] ")
        if answer.strip().lower() not in ("s", "si", "sì", "y", "yes"):
            raise RestoreAbortedError("ripristino annullato dall'utente")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if data_dir.is_dir() and any(data_dir.iterdir()):
        salvaged = data_dir.with_name(f"{data_dir.name}.pre-restore-{stamp}")
        data_dir.rename(salvaged)
        print(f"data_dir esistente spostata in: {salvaged}")
    data_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != MANIFEST_NAME]
        tar.extractall(data_dir, members=members, filter="data")
    if manifest.get("qdrant", {}).get("mode") == "server":
        print(
            "NOTA: il backup non include i vettori Qdrant (modalità server): "
            "ripristinare lo snapshot sul server Qdrant separatamente."
        )
    print(f"Ripristino completato in: {data_dir}")
    return manifest


def list_backups(
    settings: Settings | None = None, output_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Elenca gli archivi di backup con il loro manifest."""
    settings = settings or get_settings()
    out_dir = (output_dir or _default_backups_dir(settings)).resolve()
    backups: list[dict[str, Any]] = []
    if not out_dir.is_dir():
        return backups
    for archive in sorted(out_dir.glob("virtualis-backup-*.tar.gz")):
        try:
            manifest = read_manifest(archive)
        except (ValueError, tarfile.TarError, json.JSONDecodeError) as exc:
            manifest = {"error": str(exc)}
        backups.append(
            {
                "path": str(archive),
                "size_bytes": archive.stat().st_size,
                "manifest": manifest,
            }
        )
    return backups


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argomenti CLI dei sotto-comandi backup/restore/list."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_output_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--output-dir",
            type=Path,
            default=None,
            help="directory degli archivi (default: backups/ accanto a data_dir)",
        )

    p_backup = sub.add_parser("backup", help="crea un archivio di backup")
    add_output_dir(p_backup)

    p_restore = sub.add_parser("restore", help="ripristina data_dir da un archivio")
    p_restore.add_argument("archive", type=Path, help="archivio .tar.gz da ripristinare")
    p_restore.add_argument(
        "--yes", action="store_true", help="non chiedere conferma (uso da script)"
    )

    p_list = sub.add_parser("list", help="elenca gli archivi di backup")
    add_output_dir(p_list)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point CLI."""
    args = parse_args(argv)
    try:
        if args.command == "backup":
            create_backup(output_dir=args.output_dir)
        elif args.command == "restore":
            restore_backup(args.archive, assume_yes=args.yes)
        elif args.command == "list":
            backups = list_backups(output_dir=args.output_dir)
            if not backups:
                print("Nessun backup trovato.")
            for item in backups:
                manifest = item["manifest"]
                if "error" in manifest:
                    print(f"{item['path']}  (manifest illeggibile: {manifest['error']})")
                    continue
                counts = manifest.get("counts", {})
                print(
                    f"{item['path']}  {item['size_bytes']} bytes  "
                    f"del {manifest.get('created_at', '?')}  "
                    f"documenti={counts.get('documents', '?')} "
                    f"chunk={counts.get('chunks', '?')} "
                    f"utenti={counts.get('users', '?')} "
                    f"qdrant={manifest.get('qdrant', {}).get('mode', '?')}"
                )
    except RestoreAbortedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, tarfile.TarError, OSError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
