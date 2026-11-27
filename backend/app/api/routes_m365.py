"""Route per l'import da Microsoft 365 (Graph API).

- ``POST /api/m365/test``: verifica le credenziali senza avviare nulla;
- ``POST /api/m365/start``: avvia il job di import (o la sola scansione con
  ``dry_run``) in background — solo admin. Supporta lo scoping per cartella
  (``folders``, formato ``"upn:/percorso"``: lo scan OneDrive parte solo da
  quei percorsi) e il tetto di dimensione per file (``max_file_mb``,
  default 50 MB: i file oltre soglia vengono saltati);
- ``GET  /api/m365/status``: stato del job per il polling del frontend
  — solo admin (come tutta la pagina /m365);
- ``POST /api/m365/cancel``: annulla il job in corso — solo admin.

Tutte le route richiedono il ruolo admin; lo stato generico della coda di
ingestion per gli altri utenti resta esposto da ``/api/ingest/status``.

Le credenziali NON vengono mai persistite: viaggiano nel body della
richiesta di avvio e vivono solo per la durata del job.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, get_session_factory
from ..ingestion import pipeline
from ..m365_import import (
    GraphClient,
    M365AuthError,
    M365JobRunningError,
    begin_device_flow,
    get_job,
    parse_folder_spec,
    try_complete_device_flow,
)
from ..models import Document, User
from .deps import require_admin

router = APIRouter(prefix="/api/m365", tags=["m365"])

AuthMode = Literal["password", "client_credentials", "device_code"]


class M365AuthIn(BaseModel):
    """Credenziali per l'accesso a Graph (mai salvate)."""

    mode: AuthMode = "password"
    # ROPC
    username: str | None = None
    password: str | None = None
    tenant: str | None = None  # dominio o tenant id (default: organizations)
    # App-only
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=True)
        if self.mode == "device_code":
            return data  # il token arriva dal flusso device code condiviso
        if self.mode == "password" and not (self.username and self.password):
            raise HTTPException(
                status_code=400, detail="username e password obbligatori"
            )
        if self.mode == "client_credentials" and not (
            self.tenant_id and self.client_id and self.client_secret
        ):
            raise HTTPException(
                status_code=400,
                detail="tenant_id, client_id e client_secret obbligatori",
            )
        return data


class M365StartIn(BaseModel):
    auth: M365AuthIn
    include_onedrive: bool = True
    include_sharepoint: bool = True
    include_mail: bool = True
    user: str | None = Field(
        default=None,
        description="UPN dell'utente da importare (solo app-only; default: tutti)",
    )
    users: list[str] | None = Field(
        default=None,
        description="Lista esplicita di UPN da importare (app-only senza User.Read.All)",
    )
    folders: list[str] | None = Field(
        default=None,
        description=(
            "Limita lo scan OneDrive a queste cartelle (formato 'upn:/percorso', "
            "es. 'mario.rossi@contoso.it:/Presente/VIRTUALIS'): gli altri "
            "utenti/drive non vengono scansionati; mail e SharePoint restano "
            "governati dai rispettivi flag"
        ),
    )
    max_file_mb: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Tetto di dimensione per singolo file (MB): oltre viene saltato",
    )
    scan_mode: Literal["enumerate", "search", "delta"] = Field(
        default="enumerate",
        description=(
            "'enumerate' (default, completezza garantita) percorre tutto "
            "l'albero; 'search' chiede a Graph solo le estensioni supportate "
            "(veloce su drive enormi, dipende dall'indice di ricerca); "
            "'delta' enumera il sottoalbero via endpoint delta (consigliato "
            "con 'folders' su alberi enormi: niente throttling, completo)"
        ),
    )
    dry_run: bool = Field(
        default=False, description="Solo scansione/anteprima, nessun download"
    )

    @field_validator("folders")
    @classmethod
    def _valida_folders(cls, folders: list[str] | None) -> list[str] | None:
        """Formato 'upn:/percorso' verificato all'avvio (422 se malformato)."""
        for spec in folders or []:
            parse_folder_spec(spec)  # ValueError → 422 con messaggio esplicito
        return folders


@router.post("/test")
async def test_connection(
    body: M365AuthIn,
    admin: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Verifica le credenziali richiedendo un token (nessuna chiamata Graph).

    Con ``device_code`` attende (polling) che l'utente abbia approvato il
    codice: può richiedere fino a qualche minuto.
    """
    client = GraphClient(body.as_dict())
    try:
        await client._acquire_token()
    except M365AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        await client.close()
    return {"ok": True}


class DeviceBeginIn(BaseModel):
    tenant: str | None = Field(
        default=None,
        description="Dominio/tenant id (default: organizations)",
    )
    client_id: str | None = Field(
        default=None,
        description="Client id 'public client' (default: Azure PowerShell Microsoft)",
    )


@router.post("/device/begin")
async def device_begin(
    body: DeviceBeginIn,
    admin: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Genera un codice dispositivo da approvare su login.microsoft.com/device.

    L'approvazione (con MFA) rilascia un refresh token riutilizzabile dal
    job di import: è l'unica via con tenant che impongono l'MFA.
    """
    try:
        return await begin_device_flow(body.tenant, body.client_id)
    except M365AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/device/status")
async def device_status(
    admin: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Stato del flusso device code (codice, scadenza, approvazione avvenuta).

    Se l'utente ha appena approvato, questa chiamata cattura il refresh
    token (completamento attivo del flusso).
    """
    return await try_complete_device_flow()


@router.post("/start", status_code=202)
async def start_import(
    body: M365StartIn,
    admin: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Avvia il job di import in background (202).

    Con ``folders`` ("upn:/percorso") lo scan OneDrive parte solo da quei
    percorsi (gli altri utenti/drive non vengono toccati); ``max_file_mb``
    (default 50) fa saltare i file oltre soglia.
    """
    job = get_job()
    options = {
        "include_onedrive": body.include_onedrive,
        "include_sharepoint": body.include_sharepoint,
        "include_mail": body.include_mail,
        "user": body.user,
        "users": body.users,
        "folders": body.folders,
        "max_file_mb": body.max_file_mb,
        "scan_mode": body.scan_mode,
        "dry_run": body.dry_run,
    }
    try:
        job.start(body.auth.as_dict(), options, get_session_factory())
    except M365JobRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, "dry_run": body.dry_run}


@router.get("/status")
async def import_status(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Stato del job di import (polling dal frontend, solo admin come tutta
    la pagina /m365; lo stato generico della coda per gli altri utenti resta
    su ``/api/ingest/status``).

    Se non c'è un job attivo ma la pipeline sta indicizzando (es. dopo un
    ``/api/ingest/requeue`` post-riavvio), sintetizza uno stato "running"
    in fase ingest con i contatori live dal DB: la pagina M365 mostra così
    l'avanzamento reale invece di "idle".
    """
    snapshot = get_job().status()
    if snapshot["state"] == "idle":
        queue = pipeline.pipeline_status()
        busy = len(queue["pending"]) + (1 if queue["processing"] is not None else 0)
        if busy:
            rows = (
                await db.execute(
                    select(Document.status, func.count()).group_by(Document.status)
                )
            ).all()
            counts = {status_: n for status_, n in rows}
            indexed = counts.get("indexed", 0)
            failed = counts.get("failed", 0)
            pending = counts.get("pending", 0) + counts.get("processing", 0)
            total = indexed + failed + pending
            snapshot["state"] = "running"
            snapshot["phase"] = "ingest"
            snapshot["ingest"] = {
                "queued": total,
                "indexed": indexed,
                "failed": failed,
                "pending": pending,
            }
            snapshot["download"]["total_files"] = total
            snapshot["download"]["done_files"] = indexed + failed
            snapshot["download"]["percent"] = (
                round(100 * (indexed + failed) / total, 1) if total else 0
            )
    return snapshot


@router.post("/cancel", status_code=202)
async def cancel_import(
    admin: Annotated[User, Depends(require_admin)],
) -> dict[str, Any]:
    """Annulla il job in corso (best-effort: termina al prossimo checkpoint)."""
    await get_job().cancel()
    return {"cancelling": True}
