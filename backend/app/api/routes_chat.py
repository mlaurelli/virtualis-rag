"""Route chat: streaming SSE di ``POST /api/chat`` e gestione delle sessioni.

MECCANISMO SSE: un unico ``asyncio.Queue`` trasporta eventi tipizzati interni
(``("step", dict)``, ``("token", str)``, ``("result", AgentResult)``,
``("error", str)``). L'agente gira in un task con ``token_sink``/``step_sink``
che accodano su questo canale; il generatore della risposta traduce gli eventi
interni negli eventi del contratto (step/token/reset/citations/answer/done/error),
nel formato righe ``data: {json}\\n\\n`` atteso dal frontend. Se la coda resta
muta per più di 15s viene emesso un commento keep-alive (``: keep-alive``),
che il frontend ignora perché non inizia con ``data:``.

REFLEXION: allo step ``refine`` (rigenerazione della risposta dopo un verdict
"fragile" della critic) è emesso anche un evento ``reset``: il client azzera
il testo streamato della bozza e mostra i token della versione corretta.

Le sessioni di chat sono PRIVATE: visibili solo al proprietario, admin inclusi
(404, come da contratto "sessione inesistente o di altro utente").

CONTESTO CONVERSAZIONALE: su una sessione esistente, gli ultimi
``chat_history_messages`` messaggi (escluso quello appena inviato) sono passati
a ``run_agent`` come ``history``: l'agente li usa per riformulare il quesito in
forma autonoma (nodo ``classify``) e per la continuità del dialogo (nodo
``synthesize``), mai come fonte di fatti.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.graph import AgentResult, run_agent
from ..config import get_settings
from ..db import get_db, get_session_factory
from ..models import AuditLog, ChatMessage, ChatSession, MessageRole, User
from .deps import get_current_user

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Titolo della sessione: primi caratteri del primo messaggio
_TITLE_MAX_CHARS = 60
# Anteprima dell'ultimo messaggio nell'elenco sessioni
_PREVIEW_MAX_CHARS = 100
# Silenzio oltre il quale si emette un commento keep-alive
_HEARTBEAT_SECONDS = 15.0


def _sse(event: dict[str, Any]) -> str:
    """Serializza un evento nel formato ``data: {json}\\n\\n`` del contratto."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _get_owned_session(
    db: AsyncSession, session_id: int, user: User
) -> ChatSession:
    """Sessione dell'utente o 404 (le sessioni sono private, anche per admin)."""
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="Sessione non trovata")
    return session


async def _load_history(db: AsyncSession, session_id: int) -> list[dict[str, str]]:
    """Ultimi ``chat_history_messages`` messaggi della sessione, in ordine
    cronologico, nel formato ``{"role", "content"}`` atteso dall'agente
    (contesto conversazionale: risoluzione dei riferimenti e continuità)."""
    limit = get_settings().chat_history_messages
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def _persist_partial_answer(session_id: int, content: str) -> None:
    """Salva il messaggio assistant PARZIALE di uno stream interrotto.

    Nessuna riga di audit: l'agente non ha prodotto un esito completo
    (niente citazioni, path, latenza). Il testo resta nello storico della
    sessione con il suffisso "(risposta interrotta)".
    """
    async with get_session_factory()() as db:
        db.add(
            ChatMessage(
                session_id=session_id,
                role=MessageRole.assistant.value,
                content=content,
                citations_json=[],
            )
        )
        await db.commit()


async def _persist_result(
    session_id: int, user_id: int, question: str, result: AgentResult
) -> int:
    """Salva il messaggio assistant e la riga di audit; ritorna l'``audit_id``."""
    citations = [c.model_dump() for c in result.citations]
    async with get_session_factory()() as db:
        db.add(
            ChatMessage(
                session_id=session_id,
                role=MessageRole.assistant.value,
                content=result.answer,
                citations_json=citations,
            )
        )
        audit = AuditLog(
            user_id=user_id,
            session_id=session_id,
            question=question,
            path=result.path,
            steps_json=result.steps,
            retrieved_json=result.retrieved_chunk_ids,
            answer=result.answer,
            warnings_json=result.warnings,
            latency_ms=result.latency_ms,
        )
        db.add(audit)
        await db.commit()
        return audit.id


async def _event_stream(
    message: str,
    session_id: int,
    user_id: int,
    history: list[dict[str, str]],
) -> AsyncIterator[str]:
    """Generatore SSE: esegue l'agente e traduce gli eventi interni."""
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def _sink(kind: str) -> Callable[[Any], None]:
        def put(payload: Any) -> None:
            queue.put_nowait((kind, payload))

        return put

    async def _run_agent_task() -> None:
        try:
            async with get_session_factory()() as agent_db:
                result = await run_agent(
                    message,
                    session=agent_db,
                    session_id=session_id,
                    user_id=user_id,
                    history=history,
                    token_sink=_sink("token"),
                    step_sink=_sink("step"),
                )
            queue.put_nowait(("result", result))
        except Exception as exc:
            log.error("elaborazione chat fallita", error=str(exc))
            queue.put_nowait(("error", "Errore durante l'elaborazione del quesito"))

    task = asyncio.create_task(_run_agent_task())
    # Token accumulati: se il client si disconnette a metà stream la parte
    # già generata viene comunque persistita (vedi finally)
    parts: list[str] = []
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if kind == "step":
                if payload.get("step") == "refine":
                    # Reflexion: la risposta bozza (già streamata) sta per
                    # essere sostituita dalla versione corretta — il client
                    # azzera il testo e mostra i token della rigenerazione
                    parts.clear()
                    yield _sse({"type": "reset"})
                yield _sse(
                    {
                        "type": "step",
                        "step": payload.get("step", ""),
                        "detail": payload.get("detail", ""),
                    }
                )
            elif kind == "token":
                parts.append(payload)
                yield _sse({"type": "token", "text": payload})
            elif kind == "error":
                yield _sse({"type": "error", "message": payload})
                break
            else:  # "result": l'agente ha terminato
                result: AgentResult = payload
                yield _sse(
                    {
                        "type": "citations",
                        "citations": [c.model_dump() for c in result.citations],
                    }
                )
                yield _sse(
                    {
                        "type": "answer",
                        "text": result.answer,
                        "path": result.path,
                        "warnings": result.warnings,
                        "confidence": result.confidence,
                    }
                )
                try:
                    audit_id = await _persist_result(
                        session_id, user_id, message, result
                    )
                except Exception as exc:
                    log.error("persistenza esito chat fallita", error=str(exc))
                    yield _sse(
                        {
                            "type": "error",
                            "message": "Errore nel salvataggio della risposta",
                        }
                    )
                    break
                yield _sse(
                    {"type": "done", "session_id": session_id, "audit_id": audit_id}
                )
                break
    finally:
        if not task.done():
            # Client disconnesso a metà stream: cancella il task dell'agente e
            # attendilo, così le sue risorse (sessione DB, chiamata LLM) sono
            # rilasciate subito e non restano task pendenti nell'event loop
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # I token già generati non vanno persi: la risposta parziale è
            # persistita in un task SCHERMATO dalla cancellazione (il
            # generatore viene chiuso in un contesto già cancellato: senza
            # shield la scrittura morirebbe con lui)
            partial = "".join(parts).strip()
            if partial:
                persist = asyncio.create_task(
                    _persist_partial_answer(
                        session_id, partial + "\n\n_(risposta interrotta)_"
                    )
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(persist)


class ChatIn(BaseModel):
    """Richiesta di chat: messaggio ed eventuale sessione esistente."""

    message: str = Field(min_length=1, max_length=8000)
    session_id: int | None = None


@router.post("")
async def chat(
    body: ChatIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Invia un messaggio e risponde con uno stream SSE (vedi contratto)."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")

    if body.session_id is not None:
        session = await _get_owned_session(db, body.session_id, user)
        # Contesto conversazionale: caricato PRIMA di salvare il messaggio
        # corrente, che quindi non compare nella storia passata all'agente
        history = await _load_history(db, session.id)
    else:
        session = ChatSession(user_id=user.id, title=message[:_TITLE_MAX_CHARS])
        db.add(session)
        await db.flush()
        history = []
    db.add(
        ChatMessage(
            session_id=session.id, role=MessageRole.user.value, content=message
        )
    )
    await db.commit()
    session_id = session.id

    return StreamingResponse(
        _event_stream(message, session_id, user.id, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
async def list_sessions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Sessioni dell'utente, più recenti prima, con conteggio e anteprima.

    Conteggio e anteprima sono calcolati con subquery correlate: solo i
    primi ``_PREVIEW_MAX_CHARS`` caratteri dell'ultimo messaggio lasciano il
    DB (mai il contenuto integrale di tutti i messaggi dell'utente).
    """
    message_count = (
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.session_id == ChatSession.id)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    last_message = (
        select(func.substr(ChatMessage.content, 1, _PREVIEW_MAX_CHARS))
        .where(ChatMessage.session_id == ChatSession.id)
        .order_by(ChatMessage.id.desc())
        .limit(1)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(ChatSession, message_count, last_message)
            .where(ChatSession.user_id == user.id)
            .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
            .limit(limit)
        )
    ).all()
    return {
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "message_count": int(count or 0),
                "last_message": preview,
            }
            for s, count, preview in rows
        ]
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Dettaglio di una sessione con tutti i messaggi (citazioni incluse)."""
    session = await _get_owned_session(db, session_id, user)
    messages = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session.id)
                .order_by(ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                **(
                    {"citations": m.citations_json or []}
                    if m.role == MessageRole.assistant.value
                    else {}
                ),
                "created_at": m.created_at,
            }
            for m in messages
        ],
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Elimina la sessione e i suoi messaggi (solo il proprietario)."""
    session = await _get_owned_session(db, session_id, user)
    # Rimozione esplicita: il cascade ORM farebbe lazy loading (vietato in async)
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    await db.delete(session)
    await db.commit()
    return Response(status_code=204)
