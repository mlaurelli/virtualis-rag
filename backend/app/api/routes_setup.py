"""Wizard di primo avvio: creazione del primo amministratore.

Endpoint pubblici, attivi solo finché non esiste alcun utente: dopo la
creazione del primo admin (via wizard, bootstrap da env o pannello admin)
``POST /api/setup`` risponde 409 e ``GET /api/setup/status`` riporta
``needs_setup: false``.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import TokenOut, UserOut, create_access_token, hash_password
from ..db import get_db
from ..models import User, UserRole

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


class SetupStatusOut(BaseModel):
    needs_setup: bool


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)


async def _has_users(db: AsyncSession) -> bool:
    count = await db.scalar(select(func.count(User.id)))
    return bool(count and count > 0)


@router.get("/status", response_model=SetupStatusOut)
async def setup_status(db: Annotated[AsyncSession, Depends(get_db)]) -> SetupStatusOut:
    """Indica se il sistema richiede la configurazione iniziale (nessun utente)."""
    return SetupStatusOut(needs_setup=not await _has_users(db))


@router.post("", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def setup(
    body: SetupIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    """Crea il primo amministratore e restituisce un token, come il login.

    Accettato SOLO se non esiste alcun utente: in caso contrario 409.
    La password deve avere almeno 8 caratteri (422 altrimenti).
    """
    if await _has_users(db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Configurazione iniziale già completata",
        )
    user = User(
        username=body.username.strip(),
        password_hash=hash_password(body.password),
        role=UserRole.admin.value,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    log.info("primo amministratore creato dal wizard di primo avvio", username=user.username)
    return TokenOut(
        access_token=create_access_token(user),
        user=UserOut(id=user.id, username=user.username, role=user.role),
    )
