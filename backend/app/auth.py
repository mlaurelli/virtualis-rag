"""Autenticazione: hash password (argon2), JWT, dipendenze FastAPI, router auth."""

import os
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .models import User, UserRole

log = structlog.get_logger(__name__)

ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()  # argon2

# Hash argon2 fittizio verificato al login quando lo username non esiste:
# i tempi di risposta restano uniformi e non rivelano quali username sono
# validi (anti timing attack / enumerazione utenti)
_DUMMY_PASSWORD_HASH = password_hash.hash("virtualis-dummy-password")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Hash password
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Calcola l'hash argon2 di una password."""
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verifica una password contro il suo hash."""
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(user: User) -> str:
    """Emette un JWT con sub=user id e claim role."""
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decodifica e valida un JWT; solleva HTTPException 401 se invalido."""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token non valido o scaduto",
        ) from exc


# ---------------------------------------------------------------------------
# Dipendenze
# ---------------------------------------------------------------------------

async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Risolvi l'utente corrente dal bearer token JWT.

    ``is_active`` è ricontrollato a ogni richiesta: la disattivazione di un
    utente revoca immediatamente l'accesso anche ai token già emessi e non
    ancora scaduti (comportamento scelto: revoca immediata, nessuna finestra
    di tolleranza fino alla scadenza del JWT).
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticazione richiesta",
        )
    payload = decode_access_token(token)
    user_id = int(payload["sub"])
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utente non trovato o disattivato",
        )
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Richiede che l'utente corrente abbia ruolo admin."""
    if user.role != UserRole.admin.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilegi di amministratore richiesti",
        )
    return user


# ---------------------------------------------------------------------------
# Bootstrap admin iniziale
# ---------------------------------------------------------------------------

def _truthy(value: str | None) -> bool:
    """Interpreta una variabile d'ambiente come booleano."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


async def bootstrap_admin(db: AsyncSession) -> None:
    """Crea l'utente admin iniziale se non esiste alcun utente.

    Il bootstrap automatico è attivo solo se richiesto esplicitamente:
    - ``ADMIN_AUTO_CREATE=true`` (credenziali da ADMIN_USERNAME/ADMIN_PASSWORD
      o default admin/admin, con avviso nel log), oppure
    - entrambe ``ADMIN_USERNAME`` e ``ADMIN_PASSWORD`` impostate.
    Altrimenti nessun utente viene creato: il primo amministratore si crea
    dal wizard di primo avvio (``POST /api/setup``).
    """
    count = await db.scalar(select(func.count(User.id)))
    if count and count > 0:
        return
    auto_create = _truthy(os.environ.get("ADMIN_AUTO_CREATE"))
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not auto_create and not (username and password):
        log.info(
            "nessun utente presente e bootstrap automatico disattivato: il primo "
            "amministratore va creato dal wizard di primo avvio (GET /api/setup/status) "
            "oppure impostando ADMIN_AUTO_CREATE=true o ADMIN_USERNAME/ADMIN_PASSWORD."
        )
        return
    username = username or "admin"
    password = password or "admin"
    if username == "admin" and password == "admin":
        log.warning(
            "Creato admin iniziale con credenziali di default admin/admin: "
            "cambiarle subito o impostare ADMIN_USERNAME/ADMIN_PASSWORD."
        )
    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role=UserRole.admin.value,
            is_active=True,
        )
    )
    await db.commit()
    log.info("bootstrap_admin completato", username=username)


# ---------------------------------------------------------------------------
# Anti brute-force sul login
# ---------------------------------------------------------------------------

# Fallimenti recenti per chiave "ip|username", in una finestra scorrevole.
# Contatore in memoria del processo: adeguato al dispiegamento a nodo singolo
# del prototipo (nessuno stato condiviso tra worker richiesto)
_login_failures: dict[str, deque[float]] = {}


def _throttle_key(request: Request, username: str) -> str:
    """Chiave del contatore: coppia IP client + username (normalizzato)."""
    ip = request.client.host if request.client else "unknown"
    return f"{ip}|{username.strip().lower()}"


def _purge_expired(failures: deque[float], window: float, now: float) -> None:
    """Scarta i fallimenti usciti dalla finestra scorrevole."""
    while failures and now - failures[0] > window:
        failures.popleft()


def check_login_throttle(key: str) -> None:
    """Solleva 429 (con Retry-After) se i fallimenti nella finestra hanno
    raggiunto ``login_max_attempts`` per questa chiave."""
    settings = get_settings()
    failures = _login_failures.get(key)
    if not failures:
        return
    now = time.monotonic()
    window = float(settings.login_window_seconds)
    _purge_expired(failures, window, now)
    if len(failures) >= settings.login_max_attempts:
        retry_after = max(1, int(window - (now - failures[0])) + 1)
        log.warning("login limitato per troppi fallimenti", chiave=key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Troppi tentativi di accesso falliti: riprova più tardi",
            headers={"Retry-After": str(retry_after)},
        )


def record_login_failure(key: str) -> None:
    """Registra un fallimento e fa igiene delle chiavi interamente scadute."""
    now = time.monotonic()
    window = float(get_settings().login_window_seconds)
    _login_failures.setdefault(key, deque()).append(now)
    for other_key, other in list(_login_failures.items()):
        if other_key == key:
            continue
        _purge_expired(other, window, now)
        if not other:
            del _login_failures[other_key]


# ---------------------------------------------------------------------------
# Schemi e router
# ---------------------------------------------------------------------------

class UserOut(BaseModel):
    id: int
    username: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


@router.post("/login", response_model=TokenOut)
async def login(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    """Login con JSON {"username","password"} o form-encoded (OAuth2 compatibile)."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:  # JSON malformato: 400 chiaro, non 500
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Corpo della richiesta non è JSON valido",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Corpo della richiesta non è JSON valido",
            )
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
    else:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))

    # Throttle PRIMA di qualunque lavoro (lookup utente, verifica argon2):
    # una chiave già bloccata non deve costare né una query né un hash
    throttle = _throttle_key(request, username)
    check_login_throttle(throttle)
    user = await db.scalar(select(User).where(User.username == username))
    if user is None:
        # Verifica comunque eseguita su un hash fittizio: stesso costo
        # computazionale del caso "utente esistente" (niente oracolo temporale)
        verify_password(password, _DUMMY_PASSWORD_HASH)
        record_login_failure(throttle)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide",
        )
    if not verify_password(password, user.password_hash):
        record_login_failure(throttle)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Utente disattivato",
        )
    _login_failures.pop(throttle, None)  # successo: contatore azzerato
    return TokenOut(
        access_token=create_access_token(user),
        user=UserOut(id=user.id, username=user.username, role=user.role),
    )


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    """Restituisce il profilo dell'utente autenticato."""
    return UserOut(id=user.id, username=user.username, role=user.role)
