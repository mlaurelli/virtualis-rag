"""Route ricerca: ``POST /api/search`` (ricerca ibrida + reranking)."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from ..retrieval.hybrid import SearchFilters
from ..retrieval.service import retrieve
from .deps import get_current_user, get_db

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchFiltersIn(BaseModel):
    """Filtri opzionali della ricerca (tutti i campi facoltativi)."""

    source_id: int | None = None
    doc_type: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class SearchIn(BaseModel):
    """Richiesta di ricerca: interrogazione, filtri e limite (max 50)."""

    query: str = Field(min_length=1, max_length=2000)
    filters: SearchFiltersIn | None = None
    limit: int = Field(default=10, ge=1, le=50)


@router.post("")
async def search(
    body: SearchIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Ricerca ibrida con reranking attivo, filtrata per metadati."""
    filters = None
    if body.filters is not None:
        filters = SearchFilters(
            source_id=body.filters.source_id,
            doc_type=body.filters.doc_type,
            date_from=body.filters.date_from,
            date_to=body.filters.date_to,
        )
    results = await retrieve(
        body.query,
        filters=filters,
        limit=body.limit,
        rerank=True,
        rerank_top_n=body.limit,
        session=db,
    )
    return {
        "results": [
            {
                "chunk_id": r.chunk_id,
                "document_id": r.document_id,
                "title": r.title,
                "snippet": r.snippet,
                "score": r.score,
                "page": r.page,
                "section_path": r.section_path,
                "source_name": r.source_name,
            }
            for r in results
        ]
    }
