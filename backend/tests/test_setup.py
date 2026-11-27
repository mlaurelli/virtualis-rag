"""Test del wizard di primo avvio (/api/setup/*) e del bootstrap condizionato."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app.config import get_settings
from app.ingestion import pipeline


@asynccontextmanager
async def _lifespan(app):
    """Esegue la lifespan dell'app (init_db + bootstrap_admin)."""
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test con DB temporaneo e SENZA bootstrap automatico dell'admin.

    Nessuna delle variabili ADMIN_AUTO_CREATE / ADMIN_USERNAME / ADMIN_PASSWORD
    è impostata: bootstrap_admin non deve creare alcun utente.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("MOONSHOT_API_KEY", "")
    monkeypatch.setenv("WARMUP_ON_START", "false")
    monkeypatch.delenv("ADMIN_AUTO_CREATE", raising=False)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    db_module.reset_engine()
    pipeline.reset_state()
    get_settings.cache_clear()


async def test_status_needs_setup_su_db_vuoto(client: AsyncClient) -> None:
    """Senza utenti e senza bootstrap: needs_setup è true e nessun admin esiste."""
    resp = await client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json() == {"needs_setup": True}

    # Conferma che il bootstrap non ha creato l'admin di default
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 401


async def test_setup_password_corta_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/setup", json={"username": "admin", "password": "corta"}
    )
    assert resp.status_code == 422


async def test_setup_crea_admin_e_token_valido(client: AsyncClient) -> None:
    """POST /api/setup crea il primo admin e restituisce un token come il login."""
    resp = await client.post(
        "/api/setup", json={"username": "mario", "password": "password-lunga-1"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["username"] == "mario"
    assert body["user"]["role"] == "admin"

    # Il token è valido
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "mario"

    # E il login classico funziona con le credenziali appena create
    resp = await client.post(
        "/api/auth/login", json={"username": "mario", "password": "password-lunga-1"}
    )
    assert resp.status_code == 200


async def test_secondo_setup_409_e_status_false(client: AsyncClient) -> None:
    """Dopo il primo setup: needs_setup è false e POST /api/setup risponde 409."""
    resp = await client.post(
        "/api/setup", json={"username": "admin", "password": "password-lunga-1"}
    )
    assert resp.status_code == 201

    resp = await client.get("/api/setup/status")
    assert resp.json() == {"needs_setup": False}

    resp = await client.post(
        "/api/setup", json={"username": "altro", "password": "password-lunga-2"}
    )
    assert resp.status_code == 409


async def test_bootstrap_attivo_con_admin_auto_create(
    tmp_path, monkeypatch
) -> None:
    """Con ADMIN_AUTO_CREATE=true il bootstrap crea l'admin di default (legacy)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("MOONSHOT_API_KEY", "")
    monkeypatch.setenv("WARMUP_ON_START", "false")
    monkeypatch.setenv("ADMIN_AUTO_CREATE", "true")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        resp = await c.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert resp.status_code == 200
        resp = await c.get("/api/setup/status")
        assert resp.json() == {"needs_setup": False}

    db_module.reset_engine()
    pipeline.reset_state()
    get_settings.cache_clear()
