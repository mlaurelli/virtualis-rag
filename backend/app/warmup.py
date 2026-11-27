"""Preriscaldamento dei modelli di retrieval all'avvio dell'applicazione.

Embedder e Reranker caricano i pesi (1-2 GB) in modo lazy al primo uso:
senza preriscaldamento la prima query utente pagherebbe secondi o minuti
di attesa. Il warmup gira in un task in background (non blocca il boot) ed
esegue un embedding e un predict di prova. Ogni fallimento è degradato a
warning: non deve mai abbattere l'applicazione.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from .config import Settings
from .ingestion.embeddings import Embedder
from .retrieval.rerank import Reranker

log = structlog.get_logger(__name__)

# Testo di prova usato per il preriscaldamento
_WARMUP_TEXT = "preriscaldamento"


async def warmup_models(settings: Settings) -> None:
    """Preriscalda Embedder (denso+sparso) e, se abilitato, il Reranker.

    Mai eccezioni verso il chiamante: ogni errore è loggato come warning.
    """
    started = time.perf_counter()
    log.info("preriscaldamento modelli avviato")

    try:
        embedder = Embedder.get()
        t0 = time.perf_counter()
        await asyncio.to_thread(embedder.embed_queries, [_WARMUP_TEXT])
        await asyncio.to_thread(embedder.embed_sparse, [_WARMUP_TEXT])
        log.info(
            "embedder preriscaldato",
            secondi=round(time.perf_counter() - t0, 2),
        )
    except Exception as exc:
        log.warning("preriscaldamento embedder fallito", error=str(exc))

    if settings.reranker_enabled:
        try:
            reranker = Reranker.get()
            t0 = time.perf_counter()
            # _load non solleva: restituisce None se il modello non è
            # disponibile (già loggato come warning dal Reranker)
            model = await reranker._load()
            if model is not None:

                def _predict() -> Any:
                    return model.predict([[_WARMUP_TEXT, _WARMUP_TEXT]])

                await asyncio.to_thread(_predict)
                log.info(
                    "reranker preriscaldato",
                    secondi=round(time.perf_counter() - t0, 2),
                )
        except Exception as exc:
            log.warning("preriscaldamento reranker fallito", error=str(exc))

    log.info(
        "preriscaldamento modelli completato",
        secondi=round(time.perf_counter() - started, 2),
    )
