"""Test dell'hardening HTTP: security headers e anti brute-force sul login."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app import auth as auth_module
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
    """App di test con DB temporaneo, admin admin/admin e tetto login a 3."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("MOONSHOT_API_KEY", "")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LOGIN_WINDOW_SECONDS", "300")
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()
    auth_module._login_failures.clear()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    auth_module._login_failures.clear()
    db_module.reset_engine()
    pipeline.reset_state()
    get_settings.cache_clear()


async def _login(client: AsyncClient, username: str, password: str):
    return await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# --- security headers ------------------------------------------------------------


async def test_security_headers_presenti(client: AsyncClient) -> None:
    """Ogni risposta porta gli header di hardening (CSP inclusa)."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers["Permissions-Policy"]
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    # il viewer PDF usa worker/blob same-origin
    assert "worker-src 'self' blob:" in csp


# --- anti brute-force -------------------------------------------------------------


async def test_login_throttled_dopo_troppi_fallimenti(client: AsyncClient) -> None:
    """Dopo LOGIN_MAX_ATTEMPTS fallimenti nella finestra → 429 con Retry-After,
    anche con la password corretta."""
    for _ in range(3):
        resp = await _login(client, "admin", "sbagliata")
        assert resp.status_code == 401
    resp = await _login(client, "admin", "sbagliata")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    # anche le credenziali giuste sono bloccate finché la finestra non scade
    resp = await _login(client, "admin", "admin")
    assert resp.status_code == 429


async def test_login_successo_azzera_il_contatore(client: AsyncClient) -> None:
    """Un login riuscito azzera i fallimenti: non si cumulano nel tempo."""
    assert (await _login(client, "admin", "sbagliata")).status_code == 401
    assert (await _login(client, "admin", "sbagliata")).status_code == 401
    assert (await _login(client, "admin", "admin")).status_code == 200
    # contatore ripartito da zero: altri due fallimenti → ancora 401, non 429
    assert (await _login(client, "admin", "sbagliata")).status_code == 401
    assert (await _login(client, "admin", "sbagliata")).status_code == 401


async def test_throttle_per_coppia_ip_username(client: AsyncClient) -> None:
    """Il contatore è per coppia IP+username: un altro username non è bloccato."""
    for _ in range(3):
        assert (await _login(client, "admin", "sbagliata")).status_code == 401
    assert (await _login(client, "admin", "sbagliata")).status_code == 429
    # username diverso (inesistente): 401 normale, non 429
    assert (await _login(client, "nessuno", "qualunque")).status_code == 401


async def test_throttle_prima_di_lookup_e_verifica(
    client: AsyncClient, monkeypatch
) -> None:
    """Una chiave già bloccata è respinta PRIMA di qualunque lavoro: né
    verifica argon2 (costosa) né lookup utente sul DB."""
    for _ in range(3):
        assert (await _login(client, "admin", "sbagliata")).status_code == 401

    verify_calls: list[str] = []

    def spy(password: str, hashed: str) -> bool:
        verify_calls.append(password)
        return False

    monkeypatch.setattr(auth_module, "verify_password", spy)
    resp = await _login(client, "admin", "sbagliata")
    assert resp.status_code == 429
    assert verify_calls == []
