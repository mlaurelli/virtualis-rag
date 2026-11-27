"""Test del flusso di autenticazione sull'app FastAPI di test."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app.auth import hash_password, require_admin
from app.config import get_settings
from app.ingestion import pipeline
from app.models import User, UserRole


@asynccontextmanager
async def _lifespan(app):
    """Esegue la lifespan dell'app (init_db + bootstrap_admin)."""
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test con DB SQLite temporaneo e segreti fissi."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    # La suite deve essere indipendente da una eventuale chiave reale in .env
    monkeypatch.setenv("MOONSHOT_API_KEY", "")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    get_settings.cache_clear()
    db_module.reset_engine()
    # Il lifespan avvia il worker di ingestion: la coda globale va azzerata
    # per non restare legata all'event loop del test precedente
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


async def _login(client: AsyncClient, username: str, password: str):
    return await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


async def test_bootstrap_admin_e_login_ok(client: AsyncClient) -> None:
    """L'admin iniziale viene creato e può fare login (JSON)."""
    resp = await _login(client, "admin", "admin")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == UserRole.admin.value


async def test_login_form_encoded(client: AsyncClient) -> None:
    """Il login accetta anche form-encoded (OAuth2-compatible)."""
    resp = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_ko_password_errata(client: AsyncClient) -> None:
    resp = await _login(client, "admin", "sbagliata")
    assert resp.status_code == 401
    assert resp.json()["detail"]


async def test_login_json_malformato_400(client: AsyncClient) -> None:
    """Corpo JSON non parseabile (o non oggetto) → 400 con dettaglio chiaro,
    mai 500."""
    resp = await client.post(
        "/api/auth/login",
        content=b'{"username": "admin", ',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"]

    # JSON valido ma non un oggetto: stesso trattamento
    resp = await client.post(
        "/api/auth/login",
        content=b'["admin", "admin"]',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


async def test_login_ko_utente_inesistente(client: AsyncClient) -> None:
    resp = await _login(client, "nessuno", "admin")
    assert resp.status_code == 401


async def test_me_con_token(client: AsyncClient) -> None:
    token = (await _login(client, "admin", "admin")).json()["access_token"]
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"


async def test_me_senza_token(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_require_admin_403_per_user_normale(client: AsyncClient) -> None:
    """Un utente con ruolo user riceve 403 dagli endpoint admin."""
    from app.db import get_session_factory

    async with get_session_factory()() as db:
        db.add(
            User(
                username="mario",
                password_hash=hash_password("segreta"),
                role=UserRole.user.value,
                is_active=True,
            )
        )
        await db.commit()

    token = (await _login(client, "mario", "segreta")).json()["access_token"]

    # App sonda minima con un endpoint protetto da require_admin
    probe = FastAPI()

    @probe.get("/solo-admin")
    async def solo_admin(user=Depends(require_admin)):  # type: ignore[no-untyped-def]  # noqa: B008 — idiom FastAPI
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=probe), base_url="http://test"
    ) as c:
        resp = await c.get("/solo-admin", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

        # Un admin invece passa
        admin_token = (await _login(client, "admin", "admin")).json()["access_token"]
        resp = await c.get(
            "/solo-admin", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert resp.status_code == 200


async def test_login_utente_inesistente_confronto_costante(
    client: AsyncClient, monkeypatch
) -> None:
    """Anti timing attack / enumerazione username: anche per uno username
    inesistente la password è verificata contro un hash argon2 fittizio,
    così i tempi di risposta non rivelano quali username esistono."""
    import app.auth as auth_module

    calls: list[tuple[str, str]] = []
    real_verify = auth_module.verify_password

    def spy(password: str, hashed: str) -> bool:
        calls.append((password, hashed))
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    resp = await _login(client, "utente-che-non-esiste", "qualunque")
    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0][0] == "qualunque"
    assert calls[0][1] == auth_module._DUMMY_PASSWORD_HASH


async def test_token_scaduto_401(client: AsyncClient) -> None:
    """Un token con exp nel passato è rifiutato (la scadenza è rispettata)."""
    from datetime import UTC, datetime, timedelta

    import jwt

    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": "1",
            "role": "admin",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert resp.status_code == 401


async def test_utente_disattivato_token_gia_emesso_bloccato(client: AsyncClient) -> None:
    """Comportamento scelto: la disattivazione revoca immediatamente l'accesso.
    Un token valido emesso prima della disattivazione non basta più:
    ``is_active`` è ricontrollato a ogni richiesta."""
    from app.db import get_session_factory

    async with get_session_factory()() as db:
        db.add(
            User(
                username="laura",
                password_hash=hash_password("segreta"),
                role=UserRole.user.value,
                is_active=True,
            )
        )
        await db.commit()

    token = (await _login(client, "laura", "segreta")).json()["access_token"]
    assert (
        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200

    # L'admin disattiva l'utente
    admin_token = (await _login(client, "admin", "admin")).json()["access_token"]
    users = (
        await client.get(
            "/api/admin/users", headers={"Authorization": f"Bearer {admin_token}"}
        )
    ).json()["users"]
    laura_id = next(u["id"] for u in users if u["username"] == "laura")
    resp = await client.patch(
        f"/api/admin/users/{laura_id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200

    # Il token già emesso non dà più accesso
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["llm_configured"] is False
