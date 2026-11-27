"""Server MCP ``virtualis-docs``: espone gli strumenti documentali dell'agente.

Avviabile in stdio con::

    python -m app.mcp_server

L'import del modulo non ha side-effect pesanti: l'engine DB e i modelli di
embedding/reranking sono creati solo alla prima chiamata di uno strumento
(tutti lazy, come nel resto del backend).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .agent.tools import (
    cerca_entita,
    contesto_entita,
    documenti_per_entita,
    hybrid_search,
    list_documents,
    read_document,
)
from .db import get_session_factory

mcp = FastMCP(
    "virtualis-docs",
    instructions=(
        "Strumenti di accesso all'archivio documentale Virtualis: ricerca "
        "ibrida sui passaggi indicizzati, lettura integrale dei documenti ed "
        "elenco dei documenti noti."
    ),
)


@mcp.tool
async def ricerca_documenti(
    query: str,
    source_id: int | None = None,
    doc_type: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Cerca nei documenti aziendali con ricerca ibrida (semantica + lessicale).

    Restituisce i passaggi più rilevanti con titolo, fonte, data, pagina e
    testo esteso. Filtri opzionali per sorgente (``source_id``) e tipo di
    documento (``doc_type``, es. "pdf").
    """
    filters: dict[str, Any] = {}
    if source_id is not None:
        filters["source_id"] = source_id
    if doc_type:
        filters["doc_type"] = doc_type
    async with get_session_factory()() as session:
        return await hybrid_search(query, filters=filters or None, limit=limit, session=session)


@mcp.tool
async def leggi_documento(document_id: int) -> dict[str, Any]:
    """Legge il contenuto integrale di un documento dato il suo id.

    Restituisce metadati (titolo, fonte, tipo, data) e il testo completo,
    ottenuto concatenando i passaggi in ordine. Errore se l'id non esiste.
    """
    async with get_session_factory()() as session:
        return await read_document(document_id, session=session)


@mcp.tool
async def elenca_documenti(
    source_id: int | None = None,
    doc_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Elenca i documenti dell'archivio (metadati, non il contenuto).

    Ordinati per data di modifica decrescente; filtri opzionali per sorgente
    e tipo di documento.
    """
    async with get_session_factory()() as session:
        return await list_documents(
            session=session, source_id=source_id, doc_type=doc_type, limit=limit
        )


@mcp.tool
async def kg_cerca_entita(query: str, limit: int = 10) -> dict[str, Any]:
    """Cerca entità del knowledge graph (persone, contratti, progetti, date...).

    Match testuale sul nome; ogni entità riporta il numero di documenti che
    la menzionano. Richiede il knowledge graph abilitato (``KG_ENABLED``):
    se disabilitato, restituisce un messaggio esplicito nel campo ``detail``.
    """
    async with get_session_factory()() as session:
        return await cerca_entita(query, limit=limit, session=session)


@mcp.tool
async def kg_contesto_entita(entity_id: int) -> dict[str, Any]:
    """Contesto di un'entità del knowledge graph dato il suo id.

    Restituisce i documenti che la menzionano e le relazioni di cui è
    soggetto od oggetto (con controparti e documento di provenienza).
    Richiede il knowledge graph abilitato (``KG_ENABLED``).
    """
    async with get_session_factory()() as session:
        return await contesto_entita(entity_id, session=session)


@mcp.tool
async def kg_documenti_per_entita(
    names: list[str], mode: str = "any"
) -> dict[str, Any]:
    """Trova i documenti che menzionano le entità date (recupero multi-documento).

    ``mode="all"``: solo i documenti che le menzionano TUTTE (quesiti che
    attraversano più documenti); ``mode="any"``: almeno una. Richiede il
    knowledge graph abilitato (``KG_ENABLED``).
    """
    async with get_session_factory()() as session:
        return await documenti_per_entita(names, mode=mode, session=session)


if __name__ == "__main__":
    mcp.run()  # trasporto stdio (default)