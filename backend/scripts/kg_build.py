#!/usr/bin/env python3
"""(Ri)costruzione del knowledge graph documentale (GraphRAG).

Esegue l'estrazione di entità e relazioni per tutti i documenti ``indexed``
(o per un solo documento con ``--document-id N``). Serve perché il knowledge
graph è disabilitato di default (``kg_enabled=false``): il cliente può
attivarlo e costruire il grafo sul corpus già indicizzato SENZA reindicizzare.

Costo: UNA chiamata LLM per documento (testo troncato a
``KG_MAX_CHARS_PER_DOC``, default 12000 caratteri).

Prerequisiti:

- ``MOONSHOT_API_KEY`` configurata (l'estrazione chiama il LLM reale);
- corpus già indicizzato nel ``DATA_DIR`` configurato.

Uso (dalla cartella ``backend/``)::

    uv run python scripts/kg_build.py
    uv run python scripts/kg_build.py --document-id 7

Codici di uscita: 0 = tutte le estrazioni riuscite; 1 = almeno una
estrazione fallita (riportata a console); 2 = errore di configurazione
(chiave API assente).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Permette l'import di `app` anche senza installazione del package
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_session_factory, init_db  # noqa: E402
from app.knowledge.extractor import extract_for_document  # noqa: E402
from app.llm import KimiClient  # noqa: E402
from app.models import Document, DocumentStatus  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Argomenti CLI dello script di (ri)costruzione del grafo."""
    parser = argparse.ArgumentParser(
        description="(Ri)costruisce il knowledge graph dai documenti indicizzati.",
    )
    parser.add_argument(
        "--document-id",
        type=int,
        default=None,
        help="estrai solo per questo documento (default: tutti quelli indexed)",
    )
    return parser.parse_args()


async def run(document_id: int | None) -> int:
    """Esegue l'estrazione; ritorna il numero di documenti falliti."""
    await init_db()
    session_factory = get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(Document)
            .where(Document.status == DocumentStatus.indexed.value)
            .order_by(Document.id)
        )
        if document_id is not None:
            stmt = stmt.where(Document.id == document_id)
        documents = list((await session.execute(stmt)).scalars().all())

    if not documents:
        if document_id is not None:
            print(f"Nessun documento indexed con id {document_id}.")
        else:
            print("Nessun documento indexed: indicizzare prima il corpus.")
        return 0

    settings = get_settings()
    llm = KimiClient(settings)
    print(
        f"Estrazione knowledge graph: {len(documents)} documenti "
        f"(max {settings.kg_max_chars_per_doc} caratteri/documento)"
    )

    failures = 0
    for i, document in enumerate(documents, start=1):
        async with session_factory() as session:
            report = await extract_for_document(document.id, session=session, llm=llm)
        if report["error"]:
            failures += 1
            print(
                f"[{i}/{len(documents)}] ERRORE documento {document.id} "
                f"({document.title}): {report['error']}"
            )
        else:
            print(
                f"[{i}/{len(documents)}] documento {document.id} "
                f"({document.title}): {report['n_entities']} entità, "
                f"{report['n_relations']} relazioni"
            )
    return failures


def main() -> int:
    """Entry point: controlli di configurazione e avvio dell'estrazione."""
    args = parse_args()
    settings = get_settings()
    if not settings.llm_configured:
        print(
            "MOONSHOT_API_KEY non configurata: impossibile estrarre entità e "
            "relazioni. Impostare la chiave nell'ambiente o in .env e riprovare.",
            file=sys.stderr,
        )
        return 2
    failures = asyncio.run(run(args.document_id))
    if failures:
        print(f"Completato con {failures} documenti falliti.", file=sys.stderr)
        return 1
    print("Knowledge graph aggiornato con successo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
