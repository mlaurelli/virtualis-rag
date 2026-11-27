"""Strumenti documentali dell'agente (Fase 4): ricerca, lettura, elenco.

Sono funzioni async pure, deliberatamente SENZA decoratori langchain né
registrazione automatica come "tool LLM". Con ``agent_mode="graph"`` è il
grafo a stati espliciti (:mod:`app.agent.graph`) a decidere QUANDO invocarli;
con ``agent_mode="react"`` (default) gli stessi strumenti sono esposti al
modello via function calling OpenAI dal research agent ReAct
(:mod:`app.agent.research`), che ne dichiara gli schemi e ne traccia ogni
chiamata come step. Gli stessi strumenti sono esposti via MCP in
:mod:`app.mcp_server`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..knowledge import query as kg_query
from ..models import Chunk, Document, Source
from ..retrieval import SearchFilters, SearchResult, retrieve

log = structlog.get_logger(__name__)

# Messaggio restituito dagli strumenti del grafo quando il KG è spento
_KG_DISABLED_DETAIL = (
    "Knowledge graph disabilitato: attivare con KG_ENABLED=true e popolare "
    "il grafo con scripts/kg_build.py"
)


def serialize_result(result: SearchResult) -> dict[str, Any]:
    """Serializza un :class:`SearchResult` in un dict JSON-friendly."""
    return {
        "chunk_id": result.chunk_id,
        "document_id": result.document_id,
        "title": result.title,
        "snippet": result.snippet,
        "context_text": result.context_text,
        "score": result.score,
        "page": result.page,
        "section_path": list(result.section_path),
        "source_name": result.source_name,
        "doc_type": result.doc_type,
        "bboxes": list(result.bboxes),
        "modified_at": result.modified_at.isoformat() if result.modified_at else None,
        # Provenienza dello score: True se dal cross-encoder di reranking
        "reranked": result.reranked,
    }


def _build_filters(filters: dict[str, Any] | None) -> SearchFilters | None:
    """Converte un dict di filtri grezzi in :class:`SearchFilters`.

    Chiavi riconosciute: ``source_id``, ``doc_type``, ``date_from``,
    ``date_to`` (date in ISO 8601), ``document_ids`` (lista di id: vincola la
    ricerca a quei documenti — usata dal ciclo agentico per i documenti
    collegati dal knowledge graph). Chiavi sconosciute sono ignorate.
    """
    if not filters:
        return None

    def _parse_dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            return datetime.fromisoformat(value.strip())
        return None

    raw_ids = filters.get("document_ids")
    document_ids = (
        [int(x) for x in raw_ids] if isinstance(raw_ids, list | tuple) else None
    )
    return SearchFilters(
        source_id=int(filters["source_id"]) if filters.get("source_id") is not None else None,
        doc_type=str(filters["doc_type"]) if filters.get("doc_type") else None,
        date_from=_parse_dt(filters.get("date_from")),
        date_to=_parse_dt(filters.get("date_to")),
        document_ids=document_ids,
    )


async def hybrid_search(
    query: str,
    filters: dict[str, Any] | None = None,
    limit: int = 8,
    *,
    session: AsyncSession | None,
) -> list[dict[str, Any]]:
    """Ricerca ibrida (semantica + lessicale) sui documenti indicizzati.

    Restituisce i passaggi (chunk) più rilevanti come dict serializzati,
    con metadati (titolo, fonte, pagina, data) e ``context_text``.

    Il reranking (cross-encoder locale) è parte del percorso standard:
    attivo quando ``settings.agent_rerank_enabled`` è True (default), con
    caricamento lazy del modello e graceful degradation se non disponibile.
    """
    settings = get_settings()
    results = await retrieve(
        query,
        filters=_build_filters(filters),
        limit=limit,
        rerank=settings.agent_rerank_enabled,
        rerank_top_n=limit,  # riordina l'intero pool senza perdere passaggi
        session=session,
    )
    log.info("tool hybrid_search", query=query[:80], risultati=len(results))
    return [serialize_result(r) for r in results]


async def read_document(
    document_id: int,
    *,
    session: AsyncSession,
) -> dict[str, Any]:
    """Legge un documento intero: metadati + testo dei chunk in ordine.

    Il contenuto è la concatenazione dei ``context_text`` (finestre estese)
    dei chunk in ordine ordinale. Solleva ``ValueError`` se il documento
    non esiste.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Documento {document_id} non trovato")
    source_name = (
        await session.scalar(select(Source.name).where(Source.id == document.source_id))
    ) or ""
    chunks = (
        (
            await session.execute(
                select(Chunk)
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.ord)
            )
        )
        .scalars()
        .all()
    )
    content = "\n\n".join((c.context_text or c.text) for c in chunks)
    log.info("tool read_document", document_id=document_id, chunk=len(chunks))
    return {
        "document_id": document.id,
        "title": document.title,
        "doc_type": document.doc_type,
        "author": document.author,
        "source_name": source_name,
        "status": document.status,
        "page_count": document.page_count,
        "modified_at": document.modified_at.isoformat() if document.modified_at else None,
        "chunk_count": len(chunks),
        "content": content,
    }


async def list_documents(
    *,
    session: AsyncSession,
    source_id: int | None = None,
    doc_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Elenca i documenti noti al sistema (metadati, non il contenuto).

    Filtri opzionali per ``source_id`` e ``doc_type``; ordinamento per data
    di modifica decrescente (i mai modificati in coda).
    """
    stmt = (
        select(Document, Source.name)
        .join(Source, Document.source_id == Source.id)
        .order_by(Document.modified_at.desc().nullslast(), Document.id.desc())
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    rows = (await session.execute(stmt)).all()
    log.info("tool list_documents", risultati=len(rows))
    return [
        {
            "document_id": doc.id,
            "title": doc.title,
            "doc_type": doc.doc_type,
            "author": doc.author,
            "source_id": doc.source_id,
            "source_name": source_name,
            "status": doc.status,
            "page_count": doc.page_count,
            "modified_at": doc.modified_at.isoformat() if doc.modified_at else None,
        }
        for doc, source_name in rows
    ]


# --- strumenti del knowledge graph (GraphRAG) ----------------------------------
# Disponibili solo con ``kg_enabled``: spenti rispondono con un messaggio
# esplicito invece di fallire, così l'agente/MCP restano funzionanti.


def _kg_session(session: AsyncSession | None) -> AsyncSession:
    """Sessione DB obbligatoria quando il KG è abilitato."""
    if session is None:
        raise ValueError("sessione DB richiesta per gli strumenti del knowledge graph")
    return session


async def cerca_entita(
    query: str,
    limit: int = 10,
    *,
    session: AsyncSession | None,
) -> dict[str, Any]:
    """Cerca entità del knowledge graph per nome (match testuale).

    Restituisce le entità (persone, organizzazioni, contratti, progetti,
    date, importi, prodotti) il cui nome contiene ``query``, con il numero di
    documenti che le menzionano. Se il knowledge graph è disabilitato,
    restituisce un messaggio esplicito invece di risultati.
    """
    if not get_settings().kg_enabled:
        return {"detail": _KG_DISABLED_DETAIL, "entities": []}
    entities = await kg_query.search_entities(
        query, session=_kg_session(session), limit=limit
    )
    log.info("tool cerca_entita", query=query[:80], risultati=len(entities))
    return {"entities": entities}


async def contesto_entita(
    entity_id: int,
    *,
    session: AsyncSession | None,
) -> dict[str, Any]:
    """Contesto di un'entità: documenti che la menzionano e relazioni.

    Le relazioni includono display delle controparti e documento di
    provenienza; utile per quesiti del tipo "che relazione c'è tra X e Y?".
    """
    if not get_settings().kg_enabled:
        return {"detail": _KG_DISABLED_DETAIL}
    context = await kg_query.entity_context(entity_id, session=_kg_session(session))
    if context is None:
        return {"detail": f"Entità {entity_id} non trovata"}
    log.info("tool contesto_entita", entity_id=entity_id)
    return context


async def documenti_per_entita(
    names: list[str],
    mode: str = "any",
    *,
    session: AsyncSession | None,
) -> dict[str, Any]:
    """Documenti che menzionano le entità date (recupero multi-documento).

    ``mode="all"`` richiede che il documento le menzioni TUTTE (quesiti che
    attraversano più documenti); ``mode="any"`` (default) ne basta una.
    """
    if not get_settings().kg_enabled:
        return {"detail": _KG_DISABLED_DETAIL, "documents": []}
    if mode not in ("all", "any"):
        mode = "any"
    documents = await kg_query.documents_for_entities(
        names, session=_kg_session(session), mode=mode
    )
    log.info("tool documenti_per_entita", entita=names, mode=mode, risultati=len(documents))
    return {"documents": documents, "mode": mode}
