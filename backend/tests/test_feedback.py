"""Test del ciclo di feedback sulle risposte (modello, API, integrazione eval).

L'app gira su DB SQLite temporaneo (fixture ``client``); il test dello script
``evaluate.py --from-feedback`` monkeypatcha ``app.agent.run_agent``: nessuna
chiamata LLM reale.
"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import db as db_module
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.ingestion import pipeline
from app.models import ChatMessage, ChatSession, Feedback, MessageRole, User

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import evaluate  # noqa: E402

# --- fixture dell'app ---------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Any) -> AsyncIterator[None]:
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test: DB/upload in tmp_path, admin iniziale admin/admin."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
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


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(
    client: AsyncClient, admin_token: str, username: str, password: str
) -> dict[str, Any]:
    resp = await client.post(
        "/api/admin/users",
        json={"username": username, "password": password, "role": "user"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_chat(user_id: int) -> tuple[int, int]:
    """Sessione con un messaggio utente e uno assistant; ritorna i loro id."""
    async with get_session_factory()() as db:
        session = ChatSession(user_id=user_id, title="Sessione di prova")
        db.add(session)
        await db.flush()
        user_msg = ChatMessage(
            session_id=session.id,
            role=MessageRole.user.value,
            content="Come funziona il rimborso spese?",
        )
        asst_msg = ChatMessage(
            session_id=session.id,
            role=MessageRole.assistant.value,
            content="Il rimborso spese segue la procedura [1]. " * 30,  # > 500 char
        )
        db.add_all([user_msg, asst_msg])
        await db.commit()
        return user_msg.id, asst_msg.id


# --- modello e upsert ----------------------------------------------------------


async def test_post_feedback_ok(client: AsyncClient) -> None:
    """Feedback su una propria risposta assistant: 200, dati denormalizzati."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    token = await _login(client, "mario", "password-mario")
    _, asst_id = await _seed_chat(mario["id"])

    resp = await client.post(
        "/api/feedback",
        json={"message_id": asst_id, "rating": 1},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rating"] == 1
    assert body["comment"] is None
    assert body["question"] == "Come funziona il rimborso spese?"
    assert len(body["answer_excerpt"]) == 500  # excerpt troncato
    assert body["created_at"] and body["updated_at"]


async def test_upsert_sovrascrive(client: AsyncClient) -> None:
    """Un secondo feedback sullo stesso messaggio sovrascrive il precedente."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    token = await _login(client, "mario", "password-mario")
    _, asst_id = await _seed_chat(mario["id"])

    first = await client.post(
        "/api/feedback",
        json={"message_id": asst_id, "rating": 1},
        headers=_auth(token),
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/feedback",
        json={"message_id": asst_id, "rating": -1, "comment": "Risposta sbagliata"},
        headers=_auth(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["rating"] == -1
    assert second.json()["comment"] == "Risposta sbagliata"

    async with get_session_factory()() as db:
        from sqlalchemy import select

        rows = (await db.execute(select(Feedback))).scalars().all()
    assert len(rows) == 1  # un solo feedback per messaggio


# --- vincoli di accesso --------------------------------------------------------


async def test_feedback_messaggio_inesistente(client: AsyncClient) -> None:
    """message_id sconosciuto → 404."""
    token = await _login(client, "admin", "admin")
    resp = await client.post(
        "/api/feedback",
        json={"message_id": 99999, "rating": 1},
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_feedback_messaggio_altrui(client: AsyncClient) -> None:
    """Il messaggio di un altro utente → 404 (le chat sono private)."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    luigi = await _create_user(client, admin_token, "luigi", "password-luigi")
    _, asst_id = await _seed_chat(mario["id"])

    token_luigi = await _login(client, "luigi", "password-luigi")
    resp = await client.post(
        "/api/feedback",
        json={"message_id": asst_id, "rating": 1},
        headers=_auth(token_luigi),
    )
    assert resp.status_code == 404
    assert luigi["id"] != mario["id"]


async def test_feedback_su_messaggio_utente(client: AsyncClient) -> None:
    """Il feedback è ammesso solo sui messaggi assistant → 400."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    token = await _login(client, "mario", "password-mario")
    user_msg_id, _ = await _seed_chat(mario["id"])

    resp = await client.post(
        "/api/feedback",
        json={"message_id": user_msg_id, "rating": 1},
        headers=_auth(token),
    )
    assert resp.status_code == 400


async def test_feedback_rating_non_valido(client: AsyncClient) -> None:
    """rating diverso da -1/+1 → 422 di validazione."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    token = await _login(client, "mario", "password-mario")
    _, asst_id = await _seed_chat(mario["id"])

    resp = await client.post(
        "/api/feedback",
        json={"message_id": asst_id, "rating": 0},
        headers=_auth(token),
    )
    assert resp.status_code == 422


# --- viste admin ---------------------------------------------------------------


async def test_admin_list_e_stats(client: AsyncClient) -> None:
    """Lista con filtro per giudizio e statistiche aggregate."""
    admin_token = await _login(client, "admin", "admin")
    mario = await _create_user(client, admin_token, "mario", "password-mario")
    token_mario = await _login(client, "mario", "password-mario")

    _, asst_mario = await _seed_chat(mario["id"])
    resp = await client.post(
        "/api/feedback",
        json={"message_id": asst_mario, "rating": -1, "comment": "Fonti mancanti"},
        headers=_auth(token_mario),
    )
    assert resp.status_code == 200, resp.text
    _, asst_admin = await _seed_chat(1)  # admin bootstrap ha id 1
    resp = await client.post(
        "/api/feedback",
        json={"message_id": asst_admin, "rating": 1},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text

    lista = await client.get("/api/admin/feedback", headers=_auth(admin_token))
    assert lista.status_code == 200, lista.text
    entries = lista.json()["entries"]
    assert len(entries) == 2
    assert {e["username"] for e in entries} == {"mario", "admin"}

    negativi = await client.get(
        "/api/admin/feedback?rating=-1", headers=_auth(admin_token)
    )
    assert [e["rating"] for e in negativi.json()["entries"]] == [-1]

    stats = await client.get("/api/admin/feedback/stats", headers=_auth(admin_token))
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["total"] == 2
    assert body["positive"] == 1
    assert body["negative"] == 1
    assert body["positive_pct"] == 50.0
    assert len(body["recent_negative"]) == 1
    assert body["recent_negative"][0]["comment"] == "Fonti mancanti"
    assert body["recent_negative"][0]["question"] == "Come funziona il rimborso spese?"


async def test_admin_feedback_richiede_admin(client: AsyncClient) -> None:
    """Gli endpoint admin del feedback rispondono 403 agli utenti normali."""
    admin_token = await _login(client, "admin", "admin")
    await _create_user(client, admin_token, "mario", "password-mario")
    token = await _login(client, "mario", "password-mario")
    for path in ("/api/admin/feedback", "/api/admin/feedback/stats"):
        resp = await client.get(path, headers=_auth(token))
        assert resp.status_code == 403


# --- integrazione con la batteria di valutazione -------------------------------


def test_evaluate_from_feedback(
    tmp_settings, tmp_path, monkeypatch, capsys
) -> None:
    """--from-feedback riesegue i quesiti dei feedback negativi (LLM mockato)."""
    from app.agent.graph import AgentResult

    monkeypatch.setenv("MOONSHOT_API_KEY", "chiave-finta")
    get_settings.cache_clear()

    async def _fake_run_agent(message: str, **kwargs: Any) -> AgentResult:
        return AgentResult(
            answer=f"Risposta simulata a: {message}",
            path="fast",
            retrieved_chunk_ids=[1],
            latency_ms=10,
        )

    monkeypatch.setattr("app.agent.run_agent", _fake_run_agent)

    async def _seed() -> None:
        """DB popolato: un feedback negativo (solo i negativi contano)."""
        await init_db()
        async with get_session_factory()() as db:
            user = User(username="mario", password_hash="x", role="user")
            db.add(user)
            await db.flush()
            session = ChatSession(user_id=user.id, title="s")
            db.add(session)
            await db.flush()
            msg = ChatMessage(
                session_id=session.id,
                role=MessageRole.assistant.value,
                content="risposta",
            )
            db.add(msg)
            await db.flush()
            db.add(
                Feedback(
                    message_id=msg.id,
                    user_id=user.id,
                    rating=-1,
                    question="Quesito segnalato dagli utenti?",
                    answer_excerpt="risposta",
                )
            )
            await db.commit()

    # il test è sincrono perché evaluate.main() usa asyncio.run al suo interno
    asyncio.run(_seed())

    questions_file = tmp_path / "questions.json"
    questions_file.write_text(
        json.dumps([{"question": "Quesito di riferimento?"}]), encoding="utf-8"
    )
    output_file = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate.py",
            "--questions",
            str(questions_file),
            "--output",
            str(output_file),
            "--from-feedback",
        ],
    )

    rc = evaluate.main()
    assert rc == 0
    report = json.loads(output_file.read_text(encoding="utf-8"))
    assert [r["question"] for r in report["results"]] == ["Quesito di riferimento?"]
    feedback_questions = [r["question"] for r in report["feedback_results"]]
    assert feedback_questions == ["Quesito segnalato dagli utenti?"]
    out = capsys.readouterr().out
    assert "Quesiti da feedback negativi" in out
