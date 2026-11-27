"""Test dell'healthcheck approfondito ``GET /api/health/deep`` (solo admin).

App su DB/Qdrant/upload temporanei (stessa meccanica di test_api): il worker
di ingestion reale è avviato dal lifespan, nessuna chiamata LLM.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app.api import main as main_module
from app.config import get_settings
from app.ingestion import pipeline
from app.retrieval import service as retrieval_service
from app.retrieval.rerank import Reranker


@asynccontextmanager
async def _lifespan(app):
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test: DB/Qdrant/upload in tmp_path, LLM "configurato" (finto)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-health")
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()
    retrieval_service.reset_state()
    Reranker.reset()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    db_module.reset_engine()
    pipeline.reset_state()
    retrieval_service.reset_state()
    Reranker.reset()
    get_settings.cache_clear()


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_health_deep_richiede_autenticazione(client: AsyncClient) -> None:
    resp = await client.get("/api/health/deep")
    assert resp.status_code == 401


async def test_health_deep_richiede_admin(client: AsyncClient) -> None:
    admin_token = await _login(client, "admin", "admin")
    resp = await client.post(
        "/api/admin/users",
        json={"username": "utente", "password": "password123", "role": "user"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    user_token = await _login(client, "utente", "password123")

    resp = await client.get("/api/health/deep", headers=_auth(user_token))
    assert resp.status_code == 403


async def test_health_deep_tutto_ok(client: AsyncClient) -> None:
    """Con DB/Qdrant sani e LLM configurato: 200 con tutti i check ok."""
    token = await _login(client, "admin", "admin")
    resp = await client.get("/api/health/deep", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["database"]["ok"] is True
    assert checks["qdrant"]["ok"] is True
    assert checks["qdrant"]["mode"] == "embedded"
    assert checks["qdrant"]["collection_exists"] is True
    # LLM: solo verifica di configurazione, nessuna chiamata al provider
    assert checks["llm"] == {"ok": True, "configured": True, "note": checks["llm"]["note"]}
    # Embedder: config riportata; il check non carica mai i modelli da solo
    assert checks["embedder"]["ok"] is True
    assert checks["embedder"]["dense_model"]
    assert "loaded" in checks["embedder"]


async def test_health_deep_degradato_se_qdrant_irraggiungibile(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un check fallito → 503 con status 'degraded' e dettaglio dell'errore."""

    def _boom() -> Any:
        raise RuntimeError("qdrant irraggiungibile")

    monkeypatch.setattr(main_module, "_get_index", _boom)
    token = await _login(client, "admin", "admin")
    resp = await client.get("/api/health/deep", headers=_auth(token))
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["qdrant"]["ok"] is False
    assert "qdrant irraggiungibile" in body["checks"]["qdrant"]["error"]
    assert body["checks"]["database"]["ok"] is True
