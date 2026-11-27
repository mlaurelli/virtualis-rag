"""Reranking con cross-encoder ``BAAI/bge-reranker-v2-m3`` (sentence-transformers).

Il modello (~1-2 GB) è scaricato da HuggingFace al primo uso reale e
caricato in un thread (operazione bloccante). Se il reranker è disabilitato
(``settings.reranker_enabled``) o il caricamento fallisce (import/download),
la ricerca NON fallisce: i risultati sono restituiti inalterati con un
warning nel log (graceful degradation).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from ..config import Settings, get_settings
from .hybrid import SearchResult

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

log = structlog.get_logger(__name__)

# Limite di caratteri per coppia (query, testo) inviata al cross-encoder
_RERANK_TEXT_MAX_CHARS = 2000


class Reranker:
    """Wrapper lazy/singleton attorno al cross-encoder di reranking."""

    _instance: Reranker | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model: CrossEncoder | None = None
        # None = mai tentato; False = non disponibile (non ritentare)
        self._available: bool | None = None

    @classmethod
    def get(cls) -> Reranker:
        """Restituisce il singleton (il modello è caricato al primo uso)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Azzera il singleton (usato dai test con settings diverse)."""
        cls._instance = None

    def _load_sync(self) -> CrossEncoder:
        """Carica il cross-encoder (bloccante: da chiamare in un thread)."""
        from sentence_transformers import CrossEncoder

        log.info(
            "caricamento reranker (download ~1-2 GB al primo uso)",
            model=self._settings.reranker_model,
        )
        model = CrossEncoder(self._settings.reranker_model)
        log.info("reranker pronto", model=self._settings.reranker_model)
        return model

    async def _load(self) -> CrossEncoder | None:
        """Restituisce il modello, o None se non disponibile (mai eccezioni)."""
        if self._model is not None:
            return self._model
        if self._available is False:
            return None
        if not self._settings.reranker_enabled:
            log.warning("reranker disabilitato da configurazione: salto il reranking")
            self._available = False
            return None
        try:
            self._model = await asyncio.to_thread(self._load_sync)
            self._available = True
            return self._model
        except Exception as exc:
            log.warning(
                "reranker non disponibile: risultati non riordinati",
                error=str(exc),
            )
            self._available = False
            return None

    async def rerank(
        self,
        query: str,
        results: list[SearchResult],
        top_n: int = 5,
    ) -> list[SearchResult]:
        """Riordina ``results`` per punteggio del cross-encoder.

        Aggiorna ``score`` con il punteggio del reranker e restituisce i
        primi ``top_n``. Se il modello non è disponibile restituisce i
        risultati inalterati (graceful degradation).
        """
        if not results:
            return results
        model = await self._load()
        if model is None:
            return results

        # Coppia (query, titolo + TESTO DEL CHUNK): il cross-encoder valuta
        # esattamente il passaggio citabile, non la finestra estesa che può
        # includere contenuto adiacente fuori tema e gonfiare lo score
        pairs: list[Any] = [
            [
                query,
                f"{r.title}\n{r.text or r.context_text or r.snippet}"[
                    :_RERANK_TEXT_MAX_CHARS
                ],
            ]
            for r in results
        ]
        try:
            # Wrapper named: model.predict è overloaded e mypy non lo lega a to_thread
            def _predict() -> Any:
                return model.predict(pairs)

            scores = await asyncio.to_thread(_predict)
        except Exception as exc:
            log.warning("reranking fallito: risultati inalterati", error=str(exc))
            return results

        for result, score in zip(results, scores, strict=True):
            result.score = float(score)
            # Provenienza esplicita dello score: il gate di pertinenza
            # dell'agente si applica solo a score del cross-encoder
            result.reranked = True
        ranked = sorted(results, key=lambda r: r.score, reverse=True)
        log.info(
            "reranking completato",
            candidati=len(results),
            restituiti=min(top_n, len(ranked)),
        )
        return ranked[:top_n]
