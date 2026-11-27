"""Retrieval (Fase 3): ricerca ibrida, reranking e servizio di alto livello."""

from .hybrid import HybridSearcher, SearchFilters, SearchResult, make_snippet
from .rerank import Reranker
from .service import retrieve

__all__ = [
    "HybridSearcher",
    "Reranker",
    "SearchFilters",
    "SearchResult",
    "make_snippet",
    "retrieve",
]
