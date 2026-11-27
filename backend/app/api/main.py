"""Applicazione FastAPI del backend Virtualis."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from ..auth import bootstrap_admin, require_admin
from ..auth import router as auth_router
from ..config import Settings, get_settings
from ..db import get_db, get_session_factory, init_db
from ..ingestion import convert_worker
from ..ingestion.embeddings import Embedder
from ..ingestion.pipeline import (
    _get_index,
    resume_interrupted,
    start_worker,
    stop_worker,
)
from ..llm import LLMNotConfiguredError
from ..models import User
from ..warmup import warmup_models
from .routes_admin import router as admin_router
from .routes_chat import router as chat_router
from .routes_docs import router as docs_router
from .routes_feedback import admin_router as feedback_admin_router
from .routes_feedback import router as feedback_router
from .routes_ingest import router as ingest_router
from .routes_kg import router as kg_router
from .routes_m365 import router as m365_router
from .routes_search import router as search_router
from .routes_setup import router as setup_router

log = structlog.get_logger(__name__)


async def _run_warmup(settings: Settings) -> None:
    """Esegue il preriscaldamento senza mai sollevare: un suo fallimento
    non deve abbattere l'applicazione (né lo shutdown, che attende il task)."""
    try:
        await warmup_models(settings)
    except Exception as exc:
        log.warning("preriscaldamento modelli fallito", error=str(exc))


async def _resume_ingest_safely() -> None:
    """Ripresa ingest all'avvio senza mai sollevare (stessa policy del warmup)."""
    try:
        await resume_interrupted(get_session_factory())
    except Exception as exc:
        log.warning("ripresa ingest all'avvio fallita", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Avvio: tabelle, admin iniziale, worker di ingestion e preriscaldamento
    dei modelli (in background); stop allo shutdown."""
    settings = get_settings()
    await init_db()
    async with get_session_factory()() as db:
        await bootstrap_admin(db)
    await start_worker(get_session_factory())
    # Ripresa automatica dopo riavvii/crash: riaccoda in background i
    # documenti rimasti non indicizzati (coda in memoria = vuota a ogni avvio)
    resume_task: asyncio.Task[None] = asyncio.create_task(_resume_ingest_safely())
    # Preriscaldamento in background: non blocca l'avvio; i fallimenti sono
    # degradati a warning (vedi _run_warmup), mai eccezioni
    warmup_task: asyncio.Task[None] | None = None
    if settings.warmup_on_start:
        warmup_task = asyncio.create_task(_run_warmup(settings))
    log.info("Applicazione avviata")
    yield
    await stop_worker()
    # Arresto del sottoprocesso di conversione (in thread: la join sul figlio
    # può attendere qualche secondo e non deve bloccare l'event loop)
    await asyncio.to_thread(convert_worker.shutdown)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task
    if warmup_task is not None:
        warmup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup_task
    log.info("Applicazione arrestata")


class SPAStaticFiles(StaticFiles):
    """Statici del build frontend con fallback SPA su ``index.html``."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app() -> FastAPI:
    """Factory dell'app FastAPI."""
    settings = get_settings()
    app = FastAPI(title="Virtualis Backend", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Header di sicurezza su ogni risposta (hardening del piano HTTP).

        La CSP è volutamente moderata: il build React usa script esterni e
        stili inline; il viewer PDF usa iframe/blob worker same-origin.
        """
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self' data:; "
            "worker-src 'self' blob:; frame-src 'self'; object-src 'self'",
        )
        return response

    @app.exception_handler(LLMNotConfiguredError)
    async def llm_not_configured(
        request: Request, exc: LLMNotConfiguredError
    ) -> JSONResponse:
        """LLM non configurato → 503 con dettaglio pulito."""
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    app.include_router(auth_router)
    app.include_router(setup_router)
    app.include_router(chat_router)
    app.include_router(search_router)
    app.include_router(docs_router)
    app.include_router(ingest_router)
    app.include_router(admin_router)
    app.include_router(kg_router)
    app.include_router(m365_router)
    app.include_router(feedback_router)
    app.include_router(feedback_admin_router)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        """Stato del servizio e configurazione LLM."""
        return {"status": "ok", "llm_configured": settings.llm_configured}

    @app.get("/api/health/deep")
    async def health_deep(
        user: Annotated[User, Depends(require_admin)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> JSONResponse:
        """Healthcheck approfondito per il monitoraggio (solo admin).

        Scelte di design:
        - autenticazione admin richiesta: la risposta espone dettagli
          infrastrutturali (modelli, modalità Qdrant);
        - il check LLM verifica SOLO la configurazione (nessuna chiamata al
          provider: nessun costo né latenza);
        - il check embedder NON carica i modelli (1-2 GB, caricamento lazy
          al primo uso): riporta configurazione e stato di caricamento.
        Risposta 200 se tutto ok, 503 con lo stesso payload se degradato.
        """
        checks: dict[str, Any] = {}

        try:
            await db.execute(text("SELECT 1"))
            checks["database"] = {"ok": True}
        except Exception as exc:
            checks["database"] = {"ok": False, "error": str(exc)}

        try:
            index = _get_index()
            exists = index.collection_exists()
            checks["qdrant"] = {
                "ok": exists,
                "mode": "server" if settings.qdrant_url else "embedded",
                "collection": index.collection,
                "collection_exists": exists,
            }
        except Exception as exc:
            checks["qdrant"] = {"ok": False, "error": str(exc)}

        checks["llm"] = {
            "ok": settings.llm_configured,
            "configured": settings.llm_configured,
            "note": "solo verifica di configurazione: nessuna chiamata al provider",
        }

        checks["embedder"] = {
            "ok": bool(settings.dense_model and settings.sparse_model),
            "dense_model": settings.dense_model,
            "sparse_model": settings.sparse_model,
            "loaded": Embedder.is_loaded(),
            "note": "modelli a caricamento lazy: 'loaded' indica se sono già in memoria",
        }

        healthy = all(bool(c.get("ok")) for c in checks.values())
        payload: dict[str, Any] = {
            "status": "ok" if healthy else "degraded",
            "checks": checks,
        }
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    # In produzione serve il build del frontend (in dev ci pensa il proxy Vite)
    dist = settings.frontend_dist_path
    if dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()
