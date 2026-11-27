"""Route di amministrazione (tutte protette da ``require_admin``).

Gestione utenti, log di audit e sorgenti documentali. Non esiste una delete
degli utenti nel contratto; la tutela dell'ultimo admin è applicata alla
PATCH: non si può degradare o disattivare l'ultimo amministratore attivo.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import reindex
from ..auth import hash_password
from ..config import get_settings
from ..db import get_session_factory
from ..ingestion import pipeline
from ..knowledge.query import invalidate_entity_cache
from ..models import (
    AuditLog,
    ChatMessage,
    ChatSession,
    Chunk,
    Document,
    Entity,
    EntityMention,
    Feedback,
    Relation,
    Source,
    User,
    UserRole,
)
from .deps import get_db, require_admin

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

RoleLiteral = Literal["admin", "user"]
SourceTypeLiteral = Literal["local_fs", "upload", "gdrive", "m365"]

# Documenti eliminati (e committati) per batch nel reset del materiale
_RESET_BATCH = 200


# ---------------------------------------------------------------------------
# Utenti
# ---------------------------------------------------------------------------


def _user_out(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at,
    }


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    # Minimo 8 caratteri, allineato al wizard di primo avvio (/api/setup)
    password: str = Field(min_length=8, max_length=256)
    role: RoleLiteral = "user"


class UserPatchIn(BaseModel):
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: RoleLiteral | None = None
    is_active: bool | None = None


@router.get("/users")
async def list_users(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, Any]:
    """Elenco di tutti gli utenti."""
    users = (
        (await db.execute(select(User).order_by(User.id))).scalars().all()
    )
    return {"users": [_user_out(u) for u in users]}


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreateIn, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    """Crea un utente (409 se lo username è già in uso)."""
    existing = await db.scalar(select(User.id).where(User.username == body.username))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username già in uso")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UserPatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Aggiorna ruolo, stato attivo o password di un utente.

    L'ultimo amministratore attivo non può essere degradato né disattivato.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")

    demotes = (body.role is not None and body.role != UserRole.admin.value) or (
        body.is_active is False
    )
    if user.role == UserRole.admin.value and user.is_active and demotes:
        other_admins = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == UserRole.admin.value,
                User.is_active.is_(True),
                User.id != user.id,
            )
        )
        if not other_admins:
            raise HTTPException(
                status_code=400,
                detail="Impossibile degradare/disattivare l'ultimo amministratore attivo",
            )

    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/audit")
async def list_audit(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Log di audit delle risposte, più recenti prima (paginato).

    ``total`` è il conteggio complessivo della tabella: consente al frontend
    di paginare senza scaricare l'intero log.
    """
    total = await db.scalar(select(func.count()).select_from(AuditLog))
    entries = (
        (
            await db.execute(
                select(AuditLog)
                .order_by(AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "total": total or 0,
        "entries": [
            {
                "id": e.id,
                "user_id": e.user_id,
                "session_id": e.session_id,
                "question": e.question,
                "path": e.path,
                "steps": e.steps_json or [],
                "retrieved_chunk_ids": e.retrieved_json or [],
                "answer": e.answer,
                "warnings": e.warnings_json or [],
                "latency_ms": e.latency_ms,
                "created_at": e.created_at,
            }
            for e in entries
        ]
    }


# ---------------------------------------------------------------------------
# Sorgenti
# ---------------------------------------------------------------------------


def _source_out(source: Source) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type,
        "config": source.config_json or {},
        "enabled": source.enabled,
        "last_sync_at": source.last_sync_at,
    }


def _validate_config(source_type: str, config: dict[str, Any]) -> None:
    """Valida la configurazione: per local_fs il path deve esistere."""
    if source_type == "local_fs":
        path = (config or {}).get("path")
        if not path or not Path(path).expanduser().is_dir():
            raise HTTPException(
                status_code=400,
                detail="config.path deve essere una cartella esistente",
            )


class SourceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    type: SourceTypeLiteral
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SourcePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


@router.get("/sources")
async def list_sources(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, Any]:
    """Elenco delle sorgenti documentali."""
    sources = (
        (await db.execute(select(Source).order_by(Source.id))).scalars().all()
    )
    return {"sources": [_source_out(s) for s in sources]}


@router.post("/sources", status_code=201)
async def create_source(
    body: SourceCreateIn, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    """Crea una sorgente (per local_fs il path deve esistere)."""
    _validate_config(body.type, body.config)
    source = Source(
        name=body.name,
        type=body.type,
        config_json=body.config,
        enabled=body.enabled,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return _source_out(source)


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: int,
    body: SourcePatchIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Aggiorna nome, configurazione o stato abilitato di una sorgente."""
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Sorgente non trovata")
    new_config = body.config if body.config is not None else (source.config_json or {})
    _validate_config(source.type, new_config)
    if body.name is not None:
        source.name = body.name
    if body.config is not None:
        source.config_json = body.config
    if body.enabled is not None:
        source.enabled = body.enabled
    await db.commit()
    await db.refresh(source)
    return _source_out(source)


# ---------------------------------------------------------------------------
# Reset materiale documentale
# ---------------------------------------------------------------------------


class ResetMaterialIn(BaseModel):
    """Opzioni del reset: per procedere serve la conferma esplicita."""

    confirm: bool = False
    include_chat: bool = Field(
        default=False,
        description="Azzera anche sessioni/messaggi chat, feedback e audit log",
    )


@router.post("/material/reset")
async def reset_material(
    body: ResetMaterialIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Elimina TUTTO il materiale documentale (documenti, chunk, indice
    vettoriale, knowledge graph, file su disco, sorgenti).

    La sorgente "upload" di default viene ricreata automaticamente al primo
    utilizzo. Operazione irreversibile: richiede ``confirm: true``.
    Rifiuta con 409 se la pipeline sta ancora indicizzando.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Conferma richiesta: inviare confirm=true"
        )
    status = pipeline.pipeline_status()
    if status["pending"] or status["processing"] is not None:
        raise HTTPException(
            status_code=409,
            detail="Indicizzazione in corso: riprovare a coda vuota",
        )

    index = pipeline._get_index()
    document_ids = list((await db.execute(select(Document.id))).scalars().all())
    errors: list[str] = []
    # Eliminazione a BATCH: con migliaia di documenti una transazione
    # monolitica terrebbe il DB bloccato (e la richiesta HTTP appesa) per
    # minuti; commit ogni _RESET_BATCH documenti e delete Qdrant per batch
    for start in range(0, len(document_ids), _RESET_BATCH):
        batch = document_ids[start : start + _RESET_BATCH]
        try:
            await index.delete_documents(batch)
        except Exception as exc:  # l'indice non deve bloccare il reset
            errors.append(f"indice batch da doc {batch[0]}: {exc}")
        await db.execute(delete(Chunk).where(Chunk.document_id.in_(batch)))
        await db.execute(delete(Document).where(Document.id.in_(batch)))
        await db.commit()

    # KG rimosso in blocco (in ordine FK-safe): inutile la pulizia
    # per-documento visto che qui si azzera comunque tutto il grafo
    await db.execute(delete(EntityMention))
    await db.execute(delete(Relation))
    await db.execute(delete(Entity))
    await db.execute(delete(Source))
    invalidate_entity_cache()
    cleared: dict[str, int] = {"documents": len(document_ids)}
    if body.include_chat:
        for model, key in (
            (ChatMessage, "chat_messages"),
            (ChatSession, "chat_sessions"),
            (Feedback, "feedback"),
            (AuditLog, "audit_logs"),
        ):
            cleared[key] = (
                await db.scalar(select(func.count()).select_from(model)) or 0
            )
            await db.execute(delete(model))
    await db.commit()

    # File fisici negli upload (staging incluso)
    settings = get_settings()
    removed_files = 0
    if settings.upload_dir.is_dir():
        for path in settings.upload_dir.rglob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed_files += 1
    cleared["files"] = removed_files
    if errors:
        cleared["index_errors"] = len(errors)
    return {"reset": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# Reindicizzazione dell'indice vettoriale
# ---------------------------------------------------------------------------


class ReindexIn(BaseModel):
    """Avvio della reindicizzazione: serve la conferma esplicita."""

    confirm: bool = False
    dry_run: bool = Field(
        default=False,
        description="Conta solo documenti e chunk da rilavorare, senza reindicizzare",
    )
    restart: bool = Field(
        default=False,
        description="Ignora lo stato di ripresa salvato e riparte dal primo documento",
    )


@router.post("/reindex", status_code=202)
async def start_reindex(body: ReindexIn) -> dict[str, Any]:
    """Avvia in background la reindicizzazione vettoriale (202).

    Ricostruisce i punti Qdrant dai chunk in DB (sola lettura) ri-embeddando
    col formato corrente del testo indicizzato (titolo + testo). Un solo job
    alla volta (409 se già attivo); progresso su ``GET /reindex/status``.
    Il job cede il passo alla coda di ingestion ed è riprendibile dopo un
    riavvio (stato in ``data_dir/reindex_state.json``).
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail="Conferma richiesta: inviare confirm=true"
        )
    try:
        reindex.get_job().start(
            get_session_factory(), dry_run=body.dry_run, restart=body.restart
        )
    except reindex.ReindexRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, "dry_run": body.dry_run}


@router.get("/reindex/status")
async def reindex_status() -> dict[str, Any]:
    """Stato del job di reindicizzazione (polling da CLI/cron)."""
    return reindex.get_job().status()
