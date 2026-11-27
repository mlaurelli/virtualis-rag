"""Test delle route API della Fase 5: chat SSE, sessioni, ricerca, documenti,
ingestion e admin.

L'app gira su DB SQLite e Qdrant embedded temporanei (fixture ``client``);
il worker di ingestion è quello reale avviato dal lifespan. Il KimiClient è
sempre mockato (fake identico a test_agent) e la hybrid_search dell'agente è
sostituita da un fake; la ricerca ``/api/search`` usa invece l'indice reale
popolato dalla pipeline, con reranker disabilitato via env.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client.http import models as qm
from sqlalchemy import select

from app import db as db_module
from app.agent import graph as agent_graph
from app.agent import tools as agent_tools
from app.config import get_settings
from app.db import get_session_factory
from app.ingestion import pipeline
from app.models import AuditLog, ChatMessage, ChatSession, Source
from app.retrieval import service as retrieval_service
from app.retrieval.rerank import Reranker

# --- fixture dell'app ---------------------------------------------------------


@asynccontextmanager
async def _lifespan(app):
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test: DB/Qdrant/upload in tmp_path, segreti fissi, reranker off."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RERANKER_ENABLED", "false")
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


async def _login(client: AsyncClient, username: str = "admin", password: str = "admin") -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(
    client: AsyncClient, admin_token: str, username: str, password: str, role: str = "user"
) -> dict[str, Any]:
    resp = await client.post(
        "/api/admin/users",
        json={"username": username, "password": password, "role": role},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- fake agente (come test_agent) ---------------------------------------------

_CLASSIFY, _VERIFY = "classificatore", "verificatore"


class _FakeKimi:
    """KimiClient finto: JSON prefissati per tipo di prompt + token di sintesi."""

    def __init__(self, responses: dict[str, list[str]], stream_tokens: list[str]) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_tokens = stream_tokens

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]
        raise AssertionError(f"prompt non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        for token in self._stream_tokens:
            yield token


def _hit(chunk_id: int, document_id: int, score: float) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": f"Documento {document_id}",
        "snippet": f"snippet {chunk_id}",
        "context_text": f"contenuto esteso del passaggio {chunk_id}",
        "score": score,
        "page": 3,
        "section_path": ["Capitolo 1"],
        "source_name": "Archivio locale",
        "doc_type": "pdf",
        "modified_at": "2024-03-01T12:00:00",
    }


def _install_fake_agent(monkeypatch, hits: list[dict[str, Any]]) -> None:
    """Monta KimiClient e hybrid_search finti (percorso fast con citazione [1])."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "quesito puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Le spese di trasferta ", "sono rimborsate [1]."],
    )

    async def fake_search(query, filters=None, limit=8, *, session=None):
        return [dict(h) for h in hits]

    monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake)
    monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Decodifica gli eventi ``data: {json}`` di uno stream SSE di test."""
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def _run_chat(client: AsyncClient, token: str, message: str, session_id: int | None = None):
    body: dict[str, Any] = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await client.post("/api/chat", json=body, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return _parse_sse(resp.text)


# --- chat SSE -----------------------------------------------------------------


async def test_chat_sse_end_to_end(client: AsyncClient, monkeypatch) -> None:
    """Stream SSE completo: step/token/citations/answer/done in ordine, con
    sessione, messaggi e audit persistiti."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    token = await _login(client)
    message = "Come funzionano i rimborsi delle spese di trasferta nel regolamento aziendale?"

    events = await _run_chat(client, token, message)
    types = [e["type"] for e in events]

    # ordine degli eventi del contratto
    assert types[0] == "step"
    assert [s["step"] for s in events if s["type"] == "step"] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    i_token = types.index("token")
    i_citations = types.index("citations")
    i_answer = types.index("answer")
    i_done = types.index("done")
    assert i_token < i_citations < i_answer < i_done
    assert types[-1] == "done"

    # token ricomposti = risposta finale
    full_text = "".join(e["text"] for e in events if e["type"] == "token")
    assert full_text == "Le spese di trasferta sono rimborsate [1]."
    answer = events[i_answer]
    assert answer["text"] == full_text
    assert answer["path"] == "fast"
    assert answer["warnings"] == []

    # citazioni nel formato del contratto
    citations = events[i_citations]["citations"]
    assert len(citations) == 1
    cit = citations[0]
    assert cit["n"] == 1
    assert cit["document_id"] == 7
    assert cit["title"] == "Documento 7"
    assert cit["page"] == 3
    assert cit["section_path"] == ["Capitolo 1"]
    assert cit["source_name"] == "Archivio locale"
    assert cit["snippet"]

    done = events[i_done]
    session_id, audit_id = done["session_id"], done["audit_id"]

    # persistenza: sessione con titolo, messaggi user+assistant, audit
    async with get_session_factory()() as db:
        session = await db.get(ChatSession, session_id)
        assert session.title == message[:60]
        messages = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == message
        assert messages[1].content == full_text
        assert messages[1].citations_json[0]["document_id"] == 7

        audit = await db.get(AuditLog, audit_id)
        assert audit.question == message
        assert audit.path == "fast"
        assert [s["step"] for s in audit.steps_json] == [
            "classify",
            "retrieve",
            "synthesize",
            "verify",
        ]
        assert audit.retrieved_json == [10]
        assert audit.answer == full_text
        assert audit.warnings_json == []
        assert audit.latency_ms >= 0
        assert audit.user_id == session.user_id
        assert audit.session_id == session_id


async def test_chat_continua_sessione_esistente(client: AsyncClient, monkeypatch) -> None:
    """Con session_id il messaggio si accoda alla stessa sessione."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    token = await _login(client)
    first = await _run_chat(client, token, "Prima domanda")
    session_id = first[-1]["session_id"]

    second = await _run_chat(client, token, "Seconda domanda", session_id=session_id)
    assert second[-1]["type"] == "done"
    assert second[-1]["session_id"] == session_id

    resp = await client.get(f"/api/chat/sessions/{session_id}", headers=_auth(token))
    assert [m["role"] for m in resp.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


async def test_chat_passa_la_storia_della_sessione_all_agente(
    client: AsyncClient, monkeypatch
) -> None:
    """Su sessione esistente, run_agent riceve gli ultimi messaggi come
    ``history`` (escluso quello appena inviato); la riformulazione prodotta da
    classify è registrata nell'audit (step classify)."""
    from app.api import routes_chat

    standalone = "quali sono le condizioni di garanzia del contratto 2023 nel 2024?"
    fake = _FakeKimi(
        responses={
            _CLASSIFY: [
                '{"path": "fast", "reason": "puntuale", '
                f'"standalone_question": "{standalone}"}}'
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Risposta con citazione [1]."],
    )

    async def fake_search(query, filters=None, limit=8, *, session=None):
        return [_hit(10, 7, 0.9)]

    monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake)
    monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)

    # spy su run_agent: annota la history ricevuta e delega a quello reale
    real_run_agent = routes_chat.run_agent
    seen: list[list[dict[str, str]] | None] = []

    async def spy(message, **kwargs):
        seen.append(kwargs.get("history"))
        return await real_run_agent(message, **kwargs)

    monkeypatch.setattr(routes_chat, "run_agent", spy)

    token = await _login(client)
    first = await _run_chat(client, token, "Come funziona la garanzia del contratto 2023?")
    session_id = first[-1]["session_id"]
    # primo messaggio di una nuova sessione: nessuna storia
    assert seen == [[]]

    second = await _run_chat(client, token, "e nel 2024?", session_id=session_id)
    assert second[-1]["type"] == "done"

    # secondo messaggio: storia con il primo scambio, senza "e nel 2024?"
    history = seen[-1]
    assert history is not None
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "Come funziona la garanzia del contratto 2023?"
    assert history[1]["content"] == "Risposta con citazione [1]."
    assert all("2024?" not in m["content"] for m in history)

    # audit: la riformulazione è tracciata nel dettaglio dello step classify
    audit_id = second[-1]["audit_id"]
    async with get_session_factory()() as db:
        audit = await db.get(AuditLog, audit_id)
    assert audit.question == "e nel 2024?"
    classify_step = audit.steps_json[0]
    assert classify_step["step"] == "classify"
    assert f"quesito riformulato: «{standalone}»" in classify_step["detail"]


async def test_chat_storia_limitata_a_chat_history_messages(
    client: AsyncClient, monkeypatch
) -> None:
    """La storia passata all'agente è limitata agli ultimi
    ``chat_history_messages`` messaggi (i più recenti, in ordine cronologico)."""
    from app.api import routes_chat

    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "2")
    get_settings.cache_clear()

    real_run_agent = routes_chat.run_agent
    seen: list[list[dict[str, str]] | None] = []

    async def spy(message, **kwargs):
        seen.append(kwargs.get("history"))
        return await real_run_agent(message, **kwargs)

    monkeypatch.setattr(routes_chat, "run_agent", spy)

    token = await _login(client)
    first = await _run_chat(client, token, "Prima domanda")
    session_id = first[-1]["session_id"]
    await _run_chat(client, token, "Seconda domanda", session_id=session_id)
    await _run_chat(client, token, "Terza domanda", session_id=session_id)

    # con limite 2 restano solo le ultime due battute (2° scambio)
    history = seen[-1]
    assert history is not None
    assert [m["content"] for m in history] == [
        "Seconda domanda",
        "Le spese di trasferta sono rimborsate [1].",
    ]


async def test_chat_sessione_altrui_404(client: AsyncClient, monkeypatch) -> None:
    """POST con session_id di un altro utente → 404."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    admin_token = await _login(client)
    events = await _run_chat(client, admin_token, "Domanda dell'admin")
    session_id = events[-1]["session_id"]

    await _create_user(client, admin_token, "mario", "segretissima")
    mario_token = await _login(client, "mario", "segretissima")
    resp = await client.post(
        "/api/chat",
        json={"message": "intrusione", "session_id": session_id},
        headers=_auth(mario_token),
    )
    assert resp.status_code == 404


async def test_chat_stream_errore_agente_prima_del_primo_evento(
    client: AsyncClient, monkeypatch
) -> None:
    """Se run_agent solleva prima di emettere qualunque evento, lo stream
    non si blocca: il client riceve un evento ``error`` e lo stream chiude."""
    from app.api import routes_chat

    async def boom(*args, **kwargs):
        raise RuntimeError("LLM irraggiungibile")

    monkeypatch.setattr(routes_chat, "run_agent", boom)
    token = await _login(client)
    events = await _run_chat(client, token, "Domanda che farà fallire l'agente")
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["message"]


async def test_chat_stream_disconnessione_client_cancella_task_agente(
    client: AsyncClient, monkeypatch
) -> None:
    """Se il client si disconnette a metà stream (chiusura del generatore),
    il task dell'agente è cancellato e atteso: niente task pendenti che
    continuano a consumare risorse dopo la disconnessione. I token già
    generati sono comunque persistiti come risposta parziale."""
    from app.api import routes_chat

    cancelled = asyncio.Event()

    async def fake_run_agent(message, *, session, token_sink=None, step_sink=None, **kwargs):
        token_sink("token-parziale")
        try:
            await asyncio.sleep(3600)  # agente ancora al lavoro
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(routes_chat, "run_agent", fake_run_agent)

    # Sessione reale nel DB: la risposta parziale vi sarà agganciata
    async with get_session_factory()() as db:
        session = ChatSession(user_id=1, title="interrotta")
        db.add(session)
        await db.commit()
        await db.refresh(session)
        session_id = session.id

    agen = routes_chat._event_stream(
        "domanda lunga", session_id=session_id, user_id=1, history=[]
    )
    first = await asyncio.wait_for(agen.__anext__(), timeout=5)
    assert json.loads(first.removeprefix("data:"))["type"] == "token"

    # Disconnessione: alla chiusura del generatore il task è cancellato E
    # atteso (senza await, la cancellazione non sarebbe ancora avvenuta qui)
    await agen.aclose()
    assert cancelled.is_set()

    # La risposta parziale è salvata con il suffisso di interruzione
    async with get_session_factory()() as db:
        messages = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
    assert [m.role for m in messages] == ["assistant"]
    assert messages[0].content == "token-parziale\n\n_(risposta interrotta)_"
    assert messages[0].citations_json == []


async def test_chat_stream_disconnessione_senza_token_non_salva_nulla(
    client: AsyncClient, monkeypatch
) -> None:
    """Disconnessione prima di qualunque token: nessun messaggio parziale."""
    from app.api import routes_chat

    async def fake_run_agent(message, *, session, step_sink=None, **kwargs):
        if step_sink is not None:
            step_sink({"step": "classify", "detail": ""})
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes_chat, "run_agent", fake_run_agent)
    async with get_session_factory()() as db:
        session = ChatSession(user_id=1, title="vuota")
        db.add(session)
        await db.commit()
        await db.refresh(session)
        session_id = session.id

    agen = routes_chat._event_stream(
        "domanda", session_id=session_id, user_id=1, history=[]
    )
    first = await asyncio.wait_for(agen.__anext__(), timeout=5)
    assert json.loads(first.removeprefix("data:"))["type"] == "step"
    await agen.aclose()

    async with get_session_factory()() as db:
        count = await db.scalar(
            select(ChatMessage.id).where(ChatMessage.session_id == session_id)
        )
    assert count is None


# --- sessioni ------------------------------------------------------------------


async def test_sessions_lista_dettaglio_delete_e_isolamento(
    client: AsyncClient, monkeypatch
) -> None:
    """CRUD sessioni e isolamento tra utenti (sessioni private, admin incluso)."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    admin_token = await _login(client)
    events = await _run_chat(client, admin_token, "Domanda di prova per le sessioni")
    session_id = events[-1]["session_id"]

    # lista: una sessione con conteggio e anteprima
    resp = await client.get("/api/chat/sessions", headers=_auth(admin_token))
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["title"].startswith("Domanda di prova")
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["last_message"] == "Le spese di trasferta sono rimborsate [1]."

    # dettaglio: messaggi con citazioni sul messaggio assistant
    resp = await client.get(f"/api/chat/sessions/{session_id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == session_id
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert "citations" not in detail["messages"][0]
    assert detail["messages"][1]["citations"][0]["document_id"] == 7

    # isolamento: altro utente → lista vuota, 404 su dettaglio e delete
    await _create_user(client, admin_token, "mario", "segretissima")
    mario_token = await _login(client, "mario", "segretissima")
    assert (await client.get("/api/chat/sessions", headers=_auth(mario_token))).json() == {
        "sessions": []
    }
    assert (
        await client.get(f"/api/chat/sessions/{session_id}", headers=_auth(mario_token))
    ).status_code == 404
    assert (
        await client.delete(f"/api/chat/sessions/{session_id}", headers=_auth(mario_token))
    ).status_code == 404

    # delete del proprietario → 204, poi 404
    assert (
        await client.delete(f"/api/chat/sessions/{session_id}", headers=_auth(admin_token))
    ).status_code == 204
    assert (
        await client.get(f"/api/chat/sessions/{session_id}", headers=_auth(admin_token))
    ).status_code == 404
    async with get_session_factory()() as db:
        remaining = await db.scalar(
            select(ChatMessage.id).where(ChatMessage.session_id == session_id).limit(1)
        )
        assert remaining is None


async def test_sessions_limit_elenco(client: AsyncClient, monkeypatch) -> None:
    """L'elenco sessioni rispetta ``limit`` (più recenti prima) e valida il tetto."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    token = await _login(client)
    await _run_chat(client, token, "Prima sessione")
    second = await _run_chat(client, token, "Seconda sessione")

    resp = await client.get("/api/chat/sessions?limit=1", headers=_auth(token))
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == second[-1]["session_id"]
    assert sessions[0]["message_count"] == 2
    assert (
        await client.get("/api/chat/sessions?limit=501", headers=_auth(token))
    ).status_code == 422


# --- fixture documenti indicizzati ----------------------------------------------


@pytest.fixture
async def indexed_docs(client: AsyncClient, tmp_path) -> dict[str, Any]:
    """Sorgente local_fs con due documenti indicizzati dalla pipeline reale."""
    async with get_session_factory()() as db:
        source = Source(
            name="Archivio locale",
            type="local_fs",
            config_json={"path": str(tmp_path)},
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        source_id = source.id

    md = tmp_path / "trasferte.md"
    md.write_text(
        "# Regolamento trasferte\n\n"
        "Le spese di trasferta sono rimborsate previa presentazione delle ricevute originali.\n\n"
        "## Diaria\n\nLa diaria giornaliera spetta per missioni oltre i 100 km.\n",
        encoding="utf-8",
    )
    txt = tmp_path / "note_ferie.txt"
    txt.write_text(
        "Le ferie vanno richieste con almeno due settimane di preavviso al responsabile.",
        encoding="utf-8",
    )
    sf = get_session_factory()
    doc_md = await pipeline.ingest_file(md, source_id, str(md), sf)
    doc_txt = await pipeline.ingest_file(txt, source_id, str(txt), sf)
    await pipeline.wait_idle()
    return {
        "source_id": source_id,
        "md": {"id": doc_md, "path": md},
        "txt": {"id": doc_txt, "path": txt},
    }


def _qdrant_count(document_id: int) -> int:
    index = pipeline._get_index()
    return index._client.count(
        collection_name=index.collection,
        count_filter=qm.Filter(
            must=[
                qm.FieldCondition(
                    key="document_id", match=qm.MatchValue(value=document_id)
                )
            ]
        ),
    ).count


# --- ricerca ---------------------------------------------------------------------


async def test_search_risultati_contratto_e_filtro_source(
    client: AsyncClient, indexed_docs
) -> None:
    """Risultati con i campi del contratto; filtro source_id inclusivo/esclusivo."""
    token = await _login(client)
    resp = await client.post(
        "/api/search",
        json={"query": "rimborso spese di trasferta", "limit": 10},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results
    top = results[0]
    for key in (
        "chunk_id",
        "document_id",
        "title",
        "snippet",
        "score",
        "page",
        "section_path",
        "source_name",
    ):
        assert key in top
    assert top["source_name"] == "Archivio locale"
    assert top["document_id"] == indexed_docs["md"]["id"]

    # filtro per la sorgente corretta: stessi risultati; sorgente inesistente: vuoto
    resp = await client.post(
        "/api/search",
        json={
            "query": "rimborso spese di trasferta",
            "filters": {"source_id": indexed_docs["source_id"]},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["results"]
    resp = await client.post(
        "/api/search",
        json={"query": "rimborso spese di trasferta", "filters": {"source_id": 99999}},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


async def test_search_validazione_input(client: AsyncClient) -> None:
    """limit oltre il massimo (50) e query vuota → 422; serve autenticazione."""
    token = await _login(client)
    resp = await client.post(
        "/api/search", json={"query": "x", "limit": 51}, headers=_auth(token)
    )
    assert resp.status_code == 422
    resp = await client.post("/api/search", json={"query": ""}, headers=_auth(token))
    assert resp.status_code == 422
    assert (await client.post("/api/search", json={"query": "x"})).status_code == 401


# --- documenti -------------------------------------------------------------------


async def test_documents_lista_e_filtri(client: AsyncClient, indexed_docs) -> None:
    """Elenco documenti con campi del contratto e filtri status/source_id."""
    token = await _login(client)
    resp = await client.get("/api/documents", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    docs = body["documents"]
    assert len(docs) == 2
    for key in (
        "id",
        "source_id",
        "external_id",
        "title",
        "doc_type",
        "author",
        "language",
        "status",
        "error",
        "page_count",
        "size_bytes",
        "modified_at",
        "created_at",
        "updated_at",
    ):
        assert key in docs[0]
    assert all(d["status"] == "indexed" for d in docs)

    resp = await client.get("/api/documents?status=failed", headers=_auth(token))
    assert resp.json()["documents"] == []
    resp = await client.get(
        f"/api/documents?source_id={indexed_docs['source_id']}", headers=_auth(token)
    )
    assert len(resp.json()["documents"]) == 2
    resp = await client.get("/api/documents?source_id=99999", headers=_auth(token))
    assert resp.json()["documents"] == []
    # paginazione
    resp = await client.get("/api/documents?limit=1&offset=1", headers=_auth(token))
    assert len(resp.json()["documents"]) == 1
    assert resp.json()["total"] == 2


async def test_documents_dettaglio(client: AsyncClient, indexed_docs) -> None:
    """Dettaglio con chunk_count; 404 su documento inesistente."""
    token = await _login(client)
    doc_id = indexed_docs["md"]["id"]
    resp = await client.get(f"/api/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == doc_id
    assert detail["title"] == "trasferte"
    assert detail["doc_type"] == "md"
    assert detail["chunk_count"] >= 1

    assert (await client.get("/api/documents/99999", headers=_auth(token))).status_code == 404


async def test_documents_file_download(client: AsyncClient, indexed_docs) -> None:
    """File originale servito inline con Content-Type da estensione, anche a
    utenti non admin; 404 se il file è sparito dal disco."""
    admin_token = await _login(client)
    await _create_user(client, admin_token, "mario", "segretissima")
    mario_token = await _login(client, "mario", "segretissima")

    doc_id = indexed_docs["txt"]["id"]
    resp = await client.get(f"/api/documents/{doc_id}/file", headers=_auth(mario_token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "inline" in resp.headers["content-disposition"]
    assert resp.content == indexed_docs["txt"]["path"].read_bytes()

    # file sparito dal disco → 404
    indexed_docs["txt"]["path"].unlink()
    resp = await client.get(f"/api/documents/{doc_id}/file", headers=_auth(mario_token))
    assert resp.status_code == 404
    assert (
        await client.get("/api/documents/99999/file", headers=_auth(mario_token))
    ).status_code == 404


async def test_documents_file_confinato_alle_radici_della_sorgente(
    client: AsyncClient, tmp_path
) -> None:
    """Difesa in profondità: external_id fuori da upload_dir / radice local_fs
    → 403, anche se il file esiste su disco (costruito via DB)."""
    from app.models import Document

    token = await _login(client)

    # File "sensibile" fuori da qualunque radice configurata
    outside = tmp_path / "segreto.md"
    outside.write_text("contenuto riservato", encoding="utf-8")

    # Sorgente local_fs radicata in una sottocartella legittima
    nas = tmp_path / "nas"
    nas.mkdir()
    inside = nas / "legittimo.md"
    inside.write_text("documento legittimo", encoding="utf-8")

    async with get_session_factory()() as db:
        source = Source(
            name="NAS", type="local_fs", config_json={"path": str(nas)}
        )
        db.add(source)
        await db.commit()
        await db.refresh(source)
        for external_id, title in ((str(outside), "evaso"), (str(inside), "dentro")):
            db.add(
                Document(
                    source_id=source.id,
                    external_id=external_id,
                    title=title,
                    status="indexed",
                )
            )
        await db.commit()
        ids = {
            d.title: d.id
            for d in (
                await db.execute(select(Document).where(Document.source_id == source.id))
            ).scalars()
        }

    # Dentro la radice → 200; fuori (path traversal via external_id) → 403
    resp = await client.get(f"/api/documents/{ids['dentro']}/file", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.text == "documento legittimo"
    resp = await client.get(f"/api/documents/{ids['evaso']}/file", headers=_auth(token))
    assert resp.status_code == 403
    assert "contenuto riservato" not in resp.text

    # Anche per la sorgente upload: external_id fuori da upload_dir → 403
    async with get_session_factory()() as db:
        upload_source = Source(name="upload", type="upload", config_json={})
        db.add(upload_source)
        await db.commit()
        await db.refresh(upload_source)
        doc = Document(
            source_id=upload_source.id,
            external_id=str(outside),
            title="evaso-upload",
            status="indexed",
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        upload_doc_id = doc.id
    resp = await client.get(
        f"/api/documents/{upload_doc_id}/file", headers=_auth(token)
    )
    assert resp.status_code == 403


async def test_documents_delete_rimuove_chunk_e_punti(
    client: AsyncClient, indexed_docs
) -> None:
    """DELETE: documento, chunk e punti Qdrant rimossi; 404 su inesistente."""
    token = await _login(client)
    doc_id = indexed_docs["md"]["id"]
    assert _qdrant_count(doc_id) > 0

    resp = await client.delete(f"/api/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 204
    assert _qdrant_count(doc_id) == 0
    assert (
        await client.get(f"/api/documents/{doc_id}", headers=_auth(token))
    ).status_code == 404
    assert (
        await client.delete("/api/documents/99999", headers=_auth(token))
    ).status_code == 404


# --- ingestion -------------------------------------------------------------------


async def _wait_document_status(
    client: AsyncClient, token: str, doc_id: int, wanted: str, timeout: float = 60.0
) -> dict[str, Any]:
    """Attende (polling) che il documento raggiunga lo status atteso."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        resp = await client.get(f"/api/documents/{doc_id}", headers=_auth(token))
        assert resp.status_code == 200
        doc = resp.json()
        if doc["status"] == wanted:
            return doc
        assert asyncio.get_running_loop().time() < deadline, (
            f"status fermo a {doc['status']}: {doc['error']}"
        )
        await asyncio.sleep(0.2)


async def test_upload_estensione_non_supportata_e_file_mancante(client: AsyncClient) -> None:
    """Estensione non supportata o file assente → 400."""
    token = await _login(client)
    resp = await client.post(
        "/api/ingest/upload",
        files={"file": ("virus.exe", b"MZ...", "application/octet-stream")},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert (await client.post("/api/ingest/upload", headers=_auth(token))).status_code == 400


async def test_upload_ok_pending_e_status_per_non_admin(client: AsyncClient, tmp_path) -> None:
    """Upload valido → 202 pending → indicizzato; status accessibile a utenti
    non admin e popolato con la sorgente upload; delete (solo admin) rimuove
    il file, per un non-admin risponde 403."""
    admin_token = await _login(client)
    await _create_user(client, admin_token, "mario", "segretissima")
    mario_token = await _login(client, "mario", "segretissima")

    resp = await client.post(
        "/api/ingest/upload",
        files={
            "file": (
                "manuale.md",
                b"# Manuale\n\nContenuto del manuale operativo.\n",
                "text/markdown",
            )
        },
        headers=_auth(mario_token),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    doc_id = body["document_id"]

    # status visibile anche a utenti non admin (nota frontend n. 1)
    resp = await client.get("/api/ingest/status", headers=_auth(mario_token))
    assert resp.status_code == 200
    status_body = resp.json()
    assert set(status_body) == {"queue", "per_source", "recent"}
    assert set(status_body["queue"]) == {"pending", "processing"}
    upload_sources = [s for s in status_body["per_source"] if s["name"] == "upload"]
    assert upload_sources and upload_sources[0]["source_id"]

    # l'indicizzazione avviene in background
    doc = await _wait_document_status(client, mario_token, doc_id, "indexed")
    assert doc["title"] == "manuale"

    # la cancellazione è riservata agli admin: 403 per l'utente normale
    assert (
        await client.delete(f"/api/documents/{doc_id}", headers=_auth(mario_token))
    ).status_code == 403

    # upload dir contiene il file; la delete (admin) del documento lo rimuove
    upload_dir = get_settings().upload_dir
    assert len(list(upload_dir.iterdir())) == 1
    assert (
        await client.delete(f"/api/documents/{doc_id}", headers=_auth(admin_token))
    ).status_code == 204
    assert list(upload_dir.iterdir()) == []


async def test_upload_filename_ostile_sanitizzato(client: AsyncClient) -> None:
    """Filename con path traversal, backslash o null byte: il nome su disco è
    sempre un UUID dentro upload_dir (il nome client non è mai usato)."""
    token = await _login(client)
    for hostile in ("../../etc/passwd.md", "..\\..\\segreto.md", "evil\x00.md"):
        resp = await client.post(
            "/api/ingest/upload",
            files={"file": (hostile, b"# T\n\ncontenuto del documento.\n", "text/markdown")},
            headers=_auth(token),
        )
        assert resp.status_code == 202, hostile

    upload_dir = get_settings().upload_dir
    names = [p.name for p in upload_dir.iterdir()]
    assert len(names) == 3
    for name in names:
        assert all(ch.isalnum() or ch == "." for ch in name)
        assert (upload_dir / name).resolve().parent == upload_dir.resolve()


async def test_upload_concorrenti_stesso_contenuto(client: AsyncClient) -> None:
    """Due upload concorrenti dello stesso file producono due documenti
    distinti (nomi UUID diversi): nessuna collisione né errore."""
    token = await _login(client)

    async def do_upload():
        return await client.post(
            "/api/ingest/upload",
            files={"file": ("doppio.md", b"# Doppio\n\nstesso contenuto.\n", "text/markdown")},
            headers=_auth(token),
        )

    r1, r2 = await asyncio.gather(do_upload(), do_upload())
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["document_id"] != r2.json()["document_id"]
    assert len(list(get_settings().upload_dir.iterdir())) == 2


async def test_upload_oltre_dimensione_massima_413(client: AsyncClient, monkeypatch) -> None:
    """File oltre upload_max_mb → 413, rifiutato PRIMA di leggere il corpo in
    memoria (la dimensione è nota dallo spool multipart, la read è evitata)."""
    import starlette.datastructures

    monkeypatch.setattr(get_settings(), "upload_max_mb", 0)
    token = await _login(client)

    async def no_read(self, size: int = -1):
        raise AssertionError("il corpo del file non andava letto")

    # FastAPI passa l'istanza starlette grezza creata dal parser multipart
    monkeypatch.setattr(starlette.datastructures.UploadFile, "read", no_read)
    resp = await client.post(
        "/api/ingest/upload",
        files={"file": ("grande.md", b"x" * 1024, "text/markdown")},
        headers=_auth(token),
    )
    assert resp.status_code == 413


async def test_sync_admin_only_e_flusso(client: AsyncClient, tmp_path) -> None:
    """Sync manuale: 403 per non-admin, 404 sorgente assente, 400 tipo non
    local_fs, 202 + indicizzazione per sorgente local_fs valida."""
    admin_token = await _login(client)
    await _create_user(client, admin_token, "mario", "segretissima")
    mario_token = await _login(client, "mario", "segretissima")

    # admin-only
    assert (
        await client.post("/api/ingest/sync", json={}, headers=_auth(mario_token))
    ).status_code == 403

    # sorgente inesistente → 404
    assert (
        await client.post("/api/ingest/sync", json={"source_id": 999}, headers=_auth(admin_token))
    ).status_code == 404

    # sorgente non local_fs → 400
    resp = await client.post(
        "/api/admin/sources",
        json={"name": "drive", "type": "upload", "config": {}},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201
    upload_source_id = resp.json()["id"]
    assert (
        await client.post(
            "/api/ingest/sync", json={"source_id": upload_source_id}, headers=_auth(admin_token)
        )
    ).status_code == 400

    # sorgente local_fs valida → 202 e file indicizzato dalla sync
    folder = tmp_path / "nas"
    folder.mkdir()
    (folder / "procedura.md").write_text(
        "# Procedura resi\n\nIl reso va autorizzato entro 30 giorni dalla consegna.\n",
        encoding="utf-8",
    )
    resp = await client.post(
        "/api/admin/sources",
        json={"name": "NAS", "type": "local_fs", "config": {"path": str(folder)}},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201
    source_id = resp.json()["id"]

    resp = await client.post(
        "/api/ingest/sync", json={"source_id": source_id}, headers=_auth(admin_token)
    )
    assert resp.status_code == 202
    assert resp.json() == {"started": True, "sources": [source_id]}

    # polling: il file della sorgente arriva a indexed
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        resp = await client.get(
            f"/api/documents?source_id={source_id}", headers=_auth(admin_token)
        )
        docs = resp.json()["documents"]
        if docs and docs[0]["status"] == "indexed":
            break
        assert asyncio.get_running_loop().time() < deadline, f"sync non completata: {docs}"
        await asyncio.sleep(0.2)
    assert docs[0]["title"] == "procedura"

    # last_sync_at valorizzato sullo status
    resp = await client.get("/api/ingest/status", headers=_auth(admin_token))
    nas = next(s for s in resp.json()["per_source"] if s["source_id"] == source_id)
    assert nas["last_sync_at"] is not None
    assert nas["indexed"] == 1


# --- admin -----------------------------------------------------------------------


async def test_admin_users_crud_e_vincoli(client: AsyncClient) -> None:
    """CRUD utenti: 403 per non-admin, 409 duplicato, patch ruolo/password,
    tutela dell'ultimo admin attivo."""
    admin_token = await _login(client)

    # elenco: solo admin
    resp = await client.get("/api/admin/users", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert [u["username"] for u in resp.json()["users"]] == ["admin"]

    # creazione + duplicato
    user = await _create_user(client, admin_token, "mario", "segretissima")
    assert user["role"] == "user" and user["is_active"] is True
    assert "password" not in user
    resp = await client.post(
        "/api/admin/users",
        json={"username": "mario", "password": "altra-password"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 409

    # un non-admin non accede alle route admin
    mario_token = await _login(client, "mario", "segretissima")
    assert (await client.get("/api/admin/users", headers=_auth(mario_token))).status_code == 403
    assert (
        await client.get("/api/admin/audit", headers=_auth(mario_token))
    ).status_code == 403
    assert (
        await client.get("/api/admin/sources", headers=_auth(mario_token))
    ).status_code == 403

    # patch: reset password e promozione ad admin
    resp = await client.patch(
        f"/api/admin/users/{user['id']}",
        json={"password": "nuovapassword", "role": "admin"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    assert await _login(client, "mario", "nuovapassword")

    # patch 404
    assert (
        await client.patch(
            "/api/admin/users/99999", json={"role": "admin"}, headers=_auth(admin_token)
        )
    ).status_code == 404

    # l'ultimo admin non può essere degradato né disattivato
    admin_id = (await client.get("/api/auth/me", headers=_auth(admin_token))).json()["id"]
    await client.patch(
        f"/api/admin/users/{user['id']}", json={"is_active": False}, headers=_auth(admin_token)
    )
    resp = await client.patch(
        f"/api/admin/users/{admin_id}", json={"role": "user"}, headers=_auth(admin_token)
    )
    assert resp.status_code == 400
    resp = await client.patch(
        f"/api/admin/users/{admin_id}", json={"is_active": False}, headers=_auth(admin_token)
    )
    assert resp.status_code == 400


async def test_admin_audit_lista(client: AsyncClient, monkeypatch) -> None:
    """Il log di audit espone le risposte con steps parseati, più recenti prima."""
    _install_fake_agent(monkeypatch, [_hit(10, 7, 0.9)])
    token = await _login(client)
    await _run_chat(client, token, "Domanda tracciata in audit")

    resp = await client.get("/api/admin/audit", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    entries = resp.json()["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["question"] == "Domanda tracciata in audit"
    assert entry["path"] == "fast"
    assert [s["step"] for s in entry["steps"]] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    assert entry["retrieved_chunk_ids"] == [10]
    assert entry["answer"] == "Le spese di trasferta sono rimborsate [1]."
    assert entry["warnings"] == []
    assert entry["latency_ms"] >= 0
    assert entry["session_id"] and entry["user_id"]

    # limit rispettato; total riporta comunque il conteggio complessivo
    await _run_chat(client, token, "Seconda domanda in audit")
    resp = await client.get("/api/admin/audit?limit=1", headers=_auth(token))
    assert resp.json()["total"] == 2
    assert len(resp.json()["entries"]) == 1
    assert resp.json()["entries"][0]["question"] == "Seconda domanda in audit"
    assert (
        await client.get("/api/admin/audit?limit=1001", headers=_auth(token))
    ).status_code == 422

    # offset: salta i più recenti (paginazione)
    resp = await client.get("/api/admin/audit?limit=1&offset=1", headers=_auth(token))
    assert resp.json()["total"] == 2
    assert resp.json()["entries"][0]["question"] == "Domanda tracciata in audit"
    assert (
        await client.get("/api/admin/audit?offset=-1", headers=_auth(token))
    ).status_code == 422


async def test_admin_sources_create_validate_patch(client: AsyncClient, tmp_path) -> None:
    """Sorgenti: validazione path local_fs, creazione, patch e 404."""
    token = await _login(client)

    # path inesistente → 400
    resp = await client.post(
        "/api/admin/sources",
        json={"name": "Fantasma", "type": "local_fs", "config": {"path": str(tmp_path / "nope")}},
        headers=_auth(token),
    )
    assert resp.status_code == 400

    # creazione valida
    folder = tmp_path / "archivio"
    folder.mkdir()
    resp = await client.post(
        "/api/admin/sources",
        json={
            "name": "Archivio NAS",
            "type": "local_fs",
            "config": {"path": str(folder), "glob": "**/*.md"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201
    source = resp.json()
    assert source["enabled"] is True
    assert source["config"]["glob"] == "**/*.md"

    # elenco
    resp = await client.get("/api/admin/sources", headers=_auth(token))
    assert any(s["name"] == "Archivio NAS" for s in resp.json()["sources"])

    # patch: disabilita + rinomina; config non valida → 400
    resp = await client.patch(
        f"/api/admin/sources/{source['id']}",
        json={"enabled": False, "name": "Archivio NAS (off)"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["name"] == "Archivio NAS (off)"
    resp = await client.patch(
        f"/api/admin/sources/{source['id']}",
        json={"config": {"path": str(tmp_path / "nope")}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert (
        await client.patch(
            "/api/admin/sources/99999", json={"enabled": False}, headers=_auth(token)
        )
    ).status_code == 404
