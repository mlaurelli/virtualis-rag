"""Test di init_db: colonne additive su database creati con versioni precedenti."""

from sqlalchemy import text

from app.db import get_engine, init_db


async def test_init_db_aggiunge_bboxes_json_su_db_preesistente(tmp_settings) -> None:
    """Un DB con la tabella chunks "vecchia" (senza bboxes_json) riceve la
    colonna con un ALTER additivo: nessuna migrazione di dati forzata."""
    engine = get_engine()
    async with engine.begin() as conn:
        # Schema minimo stile versione precedente (senza bboxes_json)
        await conn.execute(
            text(
                "CREATE TABLE chunks ("
                "id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, "
                "ord INTEGER NOT NULL, text TEXT NOT NULL)"
            )
        )

    await init_db()

    async with engine.begin() as conn:
        columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(chunks)"))).all()
        }
    assert "bboxes_json" in columns

    # inserimento con il modello corrente: il default tiene la riga valida
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO chunks (document_id, ord, text) VALUES (1, 0, 't')"
            )
        )
        value = (
            await conn.execute(text("SELECT bboxes_json FROM chunks"))
        ).scalar_one()
    assert value == "[]"


async def test_pragma_wal_e_busy_timeout_su_ogni_connessione(tmp_settings) -> None:
    """Ogni connessione SQLite ha WAL, busy_timeout e synchronous=NORMAL
    (event listener ``connect`` sull'engine)."""
    await init_db()
    engine = get_engine()
    async with engine.connect() as conn:
        journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
        synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
    assert str(journal).lower() == "wal"
    assert int(busy) == 5000
    assert int(synchronous) == 1  # NORMAL


async def test_init_db_crea_indice_updated_at_su_db_preesistente(tmp_settings) -> None:
    """Sui DB creati prima dell'indice su documents.updated_at, init_db lo
    aggiunge (CREATE INDEX IF NOT EXISTS); sui nuovi lo crea create_all."""
    engine = get_engine()
    async with engine.begin() as conn:
        # Tabella documents "vecchia": schema minimo senza l'indice
        await conn.execute(
            text(
                "CREATE TABLE documents ("
                "id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, "
                "external_id VARCHAR(1024) NOT NULL, updated_at DATETIME)"
            )
        )

    await init_db()

    async with engine.begin() as conn:
        indexes = {
            row[1]
            for row in (
                await conn.execute(text("PRAGMA index_list(documents)"))
            ).all()
        }
    assert "ix_documents_updated_at" in indexes
