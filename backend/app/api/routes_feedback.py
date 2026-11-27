"""Route per il feedback degli utenti sulle risposte dell'assistente.

``POST /api/feedback`` raccoglie pollice su/giù (e commento opzionale) su un
messaggio assistant; ``GET /api/admin/feedback`` e ``.../stats`` espongono
agli admin l'andamento dei giudizi. I feedback negativi alimentano la
batteria supplementare di valutazione (``scripts/evaluate.py --from-feedback``).

Come le sessioni di chat, i messaggi sono PRIVATI: un messaggio altrui (o
inesistente) risponde 404, senza rivelarne l'esistenza.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatMessage, ChatSession, Feedback, MessageRole, User
from .deps import get_current_user, get_db, require_admin

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
admin_router = APIRouter(
    prefix="/api/admin/feedback",
    tags=["admin", "feedback"],
    dependencies=[Depends(require_admin)],
)

# Anteprima della risposta conservata nel feedback (denormalizzata)
_ANSWER_EXCERPT_CHARS = 500
# Ultimi feedback negativi inclusi nelle statistiche admin
_RECENT_NEGATIVE = 5


class FeedbackIn(BaseModel):
    """Richiesta di feedback: giudizio su un messaggio assistant."""

    message_id: int
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2000)


def _feedback_out(fb: Feedback, username: str | None = None) -> dict[str, Any]:
    """Serializzazione condivisa di un feedback (vista utente e admin)."""
    out: dict[str, Any] = {
        "id": fb.id,
        "message_id": fb.message_id,
        "rating": fb.rating,
        "comment": fb.comment,
        "question": fb.question,
        "answer_excerpt": fb.answer_excerpt,
        "created_at": fb.created_at,
        "updated_at": fb.updated_at,
    }
    if username is not None:
        out["username"] = username
    return out


@router.post("")
async def post_feedback(
    body: FeedbackIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Invia (o sovrascrive) il feedback su una risposta dell'assistente."""
    message = await db.get(ChatMessage, body.message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Messaggio non trovato")
    session = await db.get(ChatSession, message.session_id)
    if session is None or session.user_id != user.id:
        # messaggio di un altro utente: 404, come per le sessioni private
        raise HTTPException(status_code=404, detail="Messaggio non trovato")
    if message.role != MessageRole.assistant.value:
        raise HTTPException(
            status_code=400,
            detail="Il feedback è ammesso solo sulle risposte dell'assistente",
        )

    # Quesito denormalizzato: ultimo messaggio utente precedente nella sessione
    question = (
        await db.execute(
            select(ChatMessage.content)
            .where(
                ChatMessage.session_id == session.id,
                ChatMessage.role == MessageRole.user.value,
                ChatMessage.id < message.id,
            )
            .order_by(ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or ""

    comment = (body.comment or "").strip() or None
    excerpt = message.content[:_ANSWER_EXCERPT_CHARS]

    existing = (
        await db.execute(select(Feedback).where(Feedback.message_id == message.id))
    ).scalar_one_or_none()
    if existing is not None:
        # upsert: cambiare idea sovrascrive il giudizio precedente
        existing.rating = body.rating
        existing.comment = comment
        existing.question = question
        existing.answer_excerpt = excerpt
        await db.commit()
        await db.refresh(existing)
        return _feedback_out(existing)

    fb = Feedback(
        message_id=message.id,
        user_id=user.id,
        rating=body.rating,
        comment=comment,
        question=question,
        answer_excerpt=excerpt,
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return _feedback_out(fb)


@admin_router.get("")
async def list_feedback(
    db: Annotated[AsyncSession, Depends(get_db)],
    rating: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Feedback degli utenti, più recenti prima, con filtro per giudizio."""
    if rating is not None and rating not in (-1, 1):
        raise HTTPException(status_code=422, detail="rating deve essere -1 o 1")
    stmt = (
        select(Feedback, User.username)
        .join(User, Feedback.user_id == User.id)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .limit(limit)
    )
    if rating is not None:
        stmt = stmt.where(Feedback.rating == rating)
    rows = (await db.execute(stmt)).all()
    return {"entries": [_feedback_out(fb, username) for fb, username in rows]}


@admin_router.get("/stats")
async def feedback_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Statistiche aggregate: totale, % positivi, ultimi feedback negativi."""
    ratings = (await db.execute(select(Feedback.rating))).scalars().all()
    total = len(ratings)
    positive = sum(1 for r in ratings if r > 0)
    recent_negative_rows = (
        await db.execute(
            select(Feedback, User.username)
            .join(User, Feedback.user_id == User.id)
            .where(Feedback.rating < 0)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
            .limit(_RECENT_NEGATIVE)
        )
    ).all()
    return {
        "total": total,
        "positive": positive,
        "negative": total - positive,
        "positive_pct": round(positive / total * 100, 1) if total else None,
        "recent_negative": [
            _feedback_out(fb, username) for fb, username in recent_negative_rows
        ],
    }
