"""Route del knowledge graph documentale (GraphRAG), tutte autenticate.

Il knowledge graph è opzionale (``kg_enabled``, default off): quando è
disabilitato ogni endpoint risponde 404 con un dettaglio che spiega come
attivarlo, invece di esporre dati vuoti.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..knowledge import query as kg_query
from ..models import User
from .deps import get_current_user, get_db

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])

_KG_DISABLED_DETAIL = (
    "Knowledge graph disabilitato: impostare KG_ENABLED=true e popolare il "
    "grafo con scripts/kg_build.py"
)


def _require_kg() -> None:
    """404 con dettaglio chiaro se il knowledge graph è disabilitato."""
    if not get_settings().kg_enabled:
        raise HTTPException(status_code=404, detail=_KG_DISABLED_DETAIL)


@router.get("/entities")
async def search_entities(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """Ricerca testuale di entità (case-insensitive) con conteggio documenti."""
    _require_kg()
    entities = await kg_query.search_entities(q, session=db, limit=limit)
    return {"entities": entities}


@router.get("/entities/{entity_id}")
async def entity_context(
    entity_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Contesto completo di un'entità: documenti e relazioni (controparti)."""
    _require_kg()
    context = await kg_query.entity_context(entity_id, session=db)
    if context is None:
        raise HTTPException(status_code=404, detail="Entità non trovata")
    return context


@router.get("/graph")
async def graph_overview(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=2000)] = 300,
) -> dict[str, Any]:
    """Vista grafica del grafo: top-``limit`` entità per numero di documenti.

    Restituisce solo gli archi interni al sottoinsieme; ``total_nodes`` e
    ``total_edges`` riportano i conteggi complessivi del grafo.
    """
    _require_kg()
    return await kg_query.full_graph(session=db, limit=limit)


@router.get("/documents")
async def documents_for_entities(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    entities: Annotated[str, Query()],
    mode: Annotated[str, Query(pattern="^(all|any)$")] = "any",
) -> dict[str, Any]:
    """Documenti che menzionano le entità (nomi separati da virgola).

    ``mode=all``: solo documenti che le menzionano TUTTE (quesiti
    multi-documento); ``mode=any``: almeno una.
    """
    _require_kg()
    names = [name.strip() for name in entities.split(",") if name.strip()]
    if not names:
        raise HTTPException(
            status_code=400, detail="Parametro 'entities' vuoto o assente"
        )
    documents = await kg_query.documents_for_entities(names, session=db, mode=mode)
    return {"documents": documents, "mode": mode}
