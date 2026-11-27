"""Test del preriscaldamento dei modelli all'avvio (warmup_on_start)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app.config import get_settings
from app.ingestion import pipeline


@asynccontextmanager
async def _lifespan(app):
    """Esegue la lifespan dell'app."""
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@asynccontextmanager
async def _app_con_warmup(tmp_path, monkeypatch, warmup_on_start: str):
    """App di test con admin da bootstrap esplicito e warmup configurabile."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("MOONSHOT_API_KEY", "")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("WARMUP_ON_START", warmup_on_start)
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()

    from app.api.main import create_app

    app = create_app()
    try:
        yield app
    finally:
        db_module.reset_engine()
        pipeline.reset_state()
        get_settings.cache_clear()


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """Client HTTP di un'app con warmup attivo ma mockato a no-op."""
    import app.api.main as main_module

    monkeypatch.setattr(main_module, "warmup_models", AsyncMock())
    async with _app_con_warmup(tmp_path, monkeypatch, "true") as app:
        from app.api.main import lifespan

        async with (
            lifespan(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            yield c


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Attende che la condizione sia vera cedendo il controllo all'event loop."""
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(timeout / 100)
    raise AssertionError("condizione non raggiunta entro il timeout")


async def test_warmup_invocato_all_avvio(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con warmup_on_start=true il lifespan lancia warmup_models in background."""
    import app.api.main as main_module

    mock = AsyncMock()
    monkeypatch.setattr(main_module, "warmup_models", mock)

    async with _app_con_warmup(tmp_path, monkeypatch, "true") as app, _lifespan(app):
        await _wait_for(lambda: mock.await_count == 1)


async def test_warmup_disattivato_non_invocato(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con warmup_on_start=false warmup_models non viene mai chiamato."""
    import app.api.main as main_module

    mock = AsyncMock()
    monkeypatch.setattr(main_module, "warmup_models", mock)

    async with _app_con_warmup(tmp_path, monkeypatch, "false") as app, _lifespan(app):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert mock.await_count == 0


async def test_warmup_fallimento_non_abbatte_app(client: AsyncClient) -> None:
    """Il warmup che fallisce non abbatte l'app (vedi anche il test sotto)."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_warmup_che_solleva_non_abbatte_app(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un warmup che solleva un'eccezione non abbatte né avvio né shutdown."""
    import app.api.main as main_module

    async def _boom(settings) -> None:
        raise RuntimeError("modello corrotto")

    monkeypatch.setattr(main_module, "warmup_models", _boom)

    async with (
        _app_con_warmup(tmp_path, monkeypatch, "true") as app, _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        await asyncio.sleep(0)  # lascia girare il task fallito
        await asyncio.sleep(0)
        resp = await c.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


async def test_warmup_models_non_solleva_mai(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup_models degrada a warning anche se embedder e reranker falliscono."""
    import app.warmup as warmup_module

    class _EmbedderCheFallisce:
        @classmethod
        def get(cls):
            raise RuntimeError("download fallito")

    class _RerankerCheFallisce:
        @classmethod
        def get(cls):
            raise RuntimeError("reranker non disponibile")

    monkeypatch.setattr(warmup_module, "Embedder", _EmbedderCheFallisce)
    monkeypatch.setattr(warmup_module, "Reranker", _RerankerCheFallisce)

    await warmup_module.warmup_models(get_settings())  # non deve sollevare
