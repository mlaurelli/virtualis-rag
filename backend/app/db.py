"""Motore e session factory SQLAlchemy async (SQLite via aiosqlite)."""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings
from .models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """PRAGMA per ogni nuova connessione SQLite (event ``connect``).

    - ``journal_mode=WAL``: letture e scritture concorrenti senza blocchi
      reciproci (essenziale con worker di ingestion + API sulla stessa base);
    - ``busy_timeout=5000``: attesa fino a 5s su lock invece di fallire
      subito con "database is locked";
    - ``synchronous=NORMAL``: fsync ridotti, sicuro in combinazione con WAL.

    NOTA: ``foreign_keys`` resta volutamente OFF (default SQLite): il modello
    dati conserva per design riferimenti "penzolanti" (audit log e feedback
    sopravvivono alla cancellazione di sessioni/messaggi, vedi docstring di
    ``Feedback``); attivare l'enforcement romperebbe quei percorsi.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine() -> AsyncEngine:
    """Restituisce (creandolo al primo uso) l'engine async globale."""
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(settings.database_url, echo=False)
        event.listen(_engine.sync_engine, "connect", _set_sqlite_pragmas)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Restituisce la factory di sessioni async."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependency FastAPI: fornisce una sessione DB per richiesta."""
    async with get_session_factory()() as session:
        yield session


async def init_db() -> None:
    """Crea tutte le tabelle se non esistono.

    ``create_all`` non altera le tabelle esistenti: le colonne additive
    introdotte dopo il primo avvio sono applicate con ALTER TABLE
    (aggiunta pura con default, nessuna migrazione di dati).
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_additive_columns)


def _apply_additive_columns(conn: Any) -> None:
    """ALTER TABLE additivi per i DB creati prima dell'introduzione delle colonne."""
    columns = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(chunks)")
    }
    if "bboxes_json" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE chunks ADD COLUMN bboxes_json TEXT NOT NULL DEFAULT '[]'"
        )
    # Indice su documents.updated_at (ordinamento dell'elenco documenti):
    # create_all non aggiunge indici a tabelle esistenti, quindi sui DB
    # creati prima dell'introduzione va creato qui (idempotente)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_documents_updated_at "
        "ON documents (updated_at)"
    )


def reset_engine() -> None:
    """Azzera engine e factory (usato dai test per puntare a DB temporanei)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
