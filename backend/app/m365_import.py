"""Import completo da Microsoft 365 (Graph API) con tracking del progresso.

Il job scarica "tutto" dal tenant/utenza configurata:

- **OneDrive**: tutti i file del drive dell'utente (o di tutti gli utenti,
  in modalità app-only);
- **SharePoint**: tutti i siti del tenant e tutti i file dei loro drive;
- **Posta**: tutti i messaggi di tutte le cartelle (salvati come ``.md``
  con intestazioni + corpo) e i loro allegati come file separati.

Controlli sul volume scaricato (opzioni del job):

- ``folders``: limita lo scan OneDrive a cartelle specifiche (formato
  ``"upn:/percorso"``): ogni percorso è risolto via
  ``/users/{upn}/drive/root:/percorso:`` e la BFS parte dai suoi children;
  gli altri utenti/drive NON vengono scansionati, mentre mail e SharePoint
  restano governati dai flag ``include_mail``/``include_sharepoint``;
- ``max_file_mb``: tetto di dimensione per singolo file (default 50 MB):
  i file oltre soglia vengono saltati e conteggiati in ``skipped_files``.

Ogni file scaricato entra nella normale pipeline di ingestion
(:func:`pipeline.ingest_file`): conversione, chunking, embedding, Qdrant e
knowledge graph restano quelli standard del sistema, con dedup via
``content_hash``.

Autenticazione (senza dipendenze extra: token endpoint via ``httpx``):

- ``client_credentials``: app registration con permessi applicativi
  (``Files.Read.All``, ``Sites.Read.All``, ``Mail.Read``) — la via consigliata
  per un service account;
- ``password`` (ROPC): username/password dell'utenza — funziona solo se il
  tenant non impone MFA/Conditional Access all'utenza.

Lo stato del job è esposto via :func:`job_status` per il polling dal
frontend: fase corrente, conteggi per categoria, byte scaricati, velocità
media, ETA stimato, eventi recenti ed errori per file.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import get_settings
from .ingestion import pipeline
from .models import Document, Source, SourceType

log = structlog.get_logger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_LOGIN = "https://login.microsoftonline.com"
# Client pubblico Microsoft (Azure PowerShell): usato di default per il flusso
# ROPC, che richiede un client_id registrato come "public client".
_DEFAULT_PUBLIC_CLIENT = "1b730954-1685-4b74-9bfd-dac224a7b894"

M365_SOURCE_NAME = "Microsoft 365"

# Estensioni importabili: allineate a quelle del connettore local_fs
_SUPPORTED = {
    ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".csv",
    ".txt", ".md", ".html", ".htm",
}

_EVENTS_MAX = 200

# Worker concorrenti per l'enumerazione dei drive (scan parallelo)
_SCAN_WORKERS = 8

# Tetto assoluto per singola richiesta Graph (secondi). Graph a volte tiene
# una risposta appesa mandando byte a singhiozzo ("trickle-hang", visto il
# 4/09/26 sul delta di /Presente: pagina mai completata per ore): i byte che
# gocciolano resettano il read timeout di httpx, che quindi non scatta mai.
# Il cap lato asyncio abortisce la richiesta e riprova su connessione nuova.
_REQUEST_HARD_CAP_S = 180.0

# Cartelle da non enumerare affatto: artefatti di sviluppo/build che
# contengono centinaia di migliaia di file non documentali (il collo di
# bottiglia dello scan è l'enumerazione, non il download). Coprono i
# principali ecosistemi: JS/TS, Python, .NET, Java, Android, iOS, PHP/Go.
_EXCLUDED_DIR_NAMES = {
    # VCS
    ".git",
    ".svn",
    ".hg",
    # JavaScript / frontend
    "node_modules",
    "bower_components",
    ".next",
    ".nuxt",
    ".turbo",
    ".angular",
    "dist",
    "coverage",
    # Python
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # .NET / Java
    "bin",
    "obj",
    "target",
    # Android / Gradle
    "build",
    "intermediates",
    ".gradle",
    "gradle",
    # iOS
    "Pods",
    "DerivedData",
    ".dart_tool",
    # PHP / Go / generici
    "vendor",
    ".cache",
}

# Spazio libero minimo sul disco upload: sotto questa soglia il download si
# interrompe con errore esplicito invece di riempire il disco del server
_MIN_FREE_BYTES = 5 * (1 << 30)  # 5 GiB

# Tetto di default per la dimensione del singolo file (sovrascrivibile con
# l'opzione ``max_file_mb`` del job): oltre soglia il file viene saltato
_DEFAULT_MAX_FILE_MB = 50

# I salti per file troppo grande vengono loggati in modo aggregato (un evento
# ogni N salti): un evento per file intaserebbe il feed su cartelle grandi
_OVERSIZE_LOG_EVERY = 25


def parse_folder_spec(spec: str) -> tuple[str, str]:
    """Valida e scompone una cartella ``"upn:/percorso"`` in ``(upn, percorso)``.

    Il percorso è normalizzato senza slash iniziali/finali. Solleva
    :class:`ValueError` con messaggio esplicito se il formato è malformato
    (separatore ``:/`` assente, upn o percorso vuoti).
    """
    upn, sep, path = spec.partition(":/")
    upn = upn.strip()
    path = path.strip().strip("/")
    if not sep or not upn or not path:
        raise ValueError(
            f"Cartella non valida: '{spec}' — atteso il formato "
            "'upn:/percorso' (es. 'mario.rossi@contoso.it:/Presente/VIRTUALIS')"
        )
    return upn, path


class M365AuthError(Exception):
    """Autenticazione/autorizzazione verso Entra ID o Graph fallita."""


class M365JobRunningError(Exception):
    """Un job di import è già in esecuzione."""


# ---------------------------------------------------------------------------
# Client Graph
# ---------------------------------------------------------------------------


class GraphClient:
    """Client Microsoft Graph minimale: token, GET paginata, download."""

    def __init__(
        self, auth: dict[str, Any], cancel_event: asyncio.Event | None = None
    ) -> None:
        self._auth = auth
        self._cancel_event = cancel_event
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True
        )

    async def close(self) -> None:
        await self._client.aclose()

    # --- autenticazione ---

    async def _acquire_token(self) -> str:
        auth = self._auth
        mode = auth.get("mode", "password")
        if mode == "device_code":
            return await self._acquire_device_token()
        tenant = (
            auth.get("tenant_id")
            or auth.get("tenant")
            or ("organizations" if mode == "password" else None)
        )
        if not tenant:
            raise M365AuthError("tenant_id mancante nella configurazione")
        if mode == "client_credentials":
            data = {
                "grant_type": "client_credentials",
                "client_id": auth.get("client_id", ""),
                "client_secret": auth.get("client_secret", ""),
                "scope": "https://graph.microsoft.com/.default",
            }
        else:  # password (ROPC)
            data = {
                "grant_type": "password",
                "client_id": auth.get("client_id") or _DEFAULT_PUBLIC_CLIENT,
                "username": auth.get("username", ""),
                "password": auth.get("password", ""),
                "scope": "https://graph.microsoft.com/.default",
            }
        response = await self._client.post(
            f"{_LOGIN}/{tenant}/oauth2/v2.0/token", data=data
        )
        payload = response.json()
        if "access_token" not in payload:
            description = payload.get("error_description", str(payload))
            # Messaggi d'errore Entra ID tradotti in indicazioni operative
            if "AADSTS50079" in description or "AADSTS50076" in description:
                raise M365AuthError(
                    "Il tenant Microsoft richiede l'MFA per questa utenza: "
                    "l'accesso con sola password è bloccato. Usare la modalità "
                    "'Codice dispositivo' (approvazione una tantum con MFA) "
                    "oppure una app registration con client_id/client_secret."
                )
            if "AADSTS7000218" in description:
                raise M365AuthError(
                    "Il client non è registrato come 'public client': abilitare "
                    "'Allow public client flows' nell'app registration."
                )
            raise M365AuthError(f"Autenticazione fallita: {description[:400]}")
        self._token = payload["access_token"]
        self._token_exp = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    async def _acquire_device_token(self) -> str:
        """Token via device code flow (MFA-compatible).

        Se il flusso ha già un ``refresh_token`` (approvazione già fatta) lo
        usa direttamente; altrimenti fa polling del token endpoint finché
        l'utente non approva il codice su ``login.microsoft.com/device``.
        """
        flow = _device_flow
        if flow is None:
            raise M365AuthError(
                "Nessun flusso device code attivo: generare prima il codice "
                "dalla pagina Microsoft 365"
            )
        tenant = flow.tenant
        if flow.refresh_token:
            data = {
                "grant_type": "refresh_token",
                "client_id": flow.client_id,
                "refresh_token": flow.refresh_token,
                "scope": "https://graph.microsoft.com/.default offline_access",
            }
            response = await self._client.post(
                f"{_LOGIN}/{tenant}/oauth2/v2.0/token", data=data
            )
            payload = response.json()
            if "access_token" not in payload:
                # Refresh token revocato/scaduto: serve una nuova approvazione
                flow.refresh_token = None
                raise M365AuthError(
                    "Sessione Microsoft scaduta: generare un nuovo codice "
                    "dispositivo e approvarlo di nuovo"
                )
            return self._store_device_tokens(payload)

        # Polling in attesa dell'approvazione dell'utente
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": flow.client_id,
            "device_code": flow.device_code,
        }
        while True:
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise asyncio.CancelledError
            if time.monotonic() > flow.expires_at:
                raise M365AuthError(
                    "Codice dispositivo scaduto: generarne uno nuovo"
                )
            response = await self._client.post(
                f"{_LOGIN}/{tenant}/oauth2/v2.0/token", data=data
            )
            payload = response.json()
            if "access_token" in payload:
                return self._store_device_tokens(payload)
            error = payload.get("error", "")
            if error == "authorization_pending":
                await asyncio.sleep(max(flow.interval, 5))
                continue
            if error == "authorization_declined":
                raise M365AuthError("Accesso negato dall'utente")
            if error == "expired_token":
                raise M365AuthError(
                    "Codice dispositivo scaduto: generarne uno nuovo"
                )
            if error == "bad_verification_code":
                raise M365AuthError("Flusso device code non valido: rigenerare il codice")
            raise M365AuthError(
                "Autenticazione device code fallita: "
                + payload.get("error_description", str(payload))[:400]
            )

    def _store_device_tokens(self, payload: dict[str, Any]) -> str:
        """Salva access/refresh token (il refresh resta nel flusso condiviso)."""
        assert _device_flow is not None
        if payload.get("refresh_token"):
            _device_flow.refresh_token = payload["refresh_token"]
        self._token = payload["access_token"]
        self._token_exp = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    async def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_exp:
            await self._acquire_token()
        assert self._token is not None
        return {"Authorization": f"Bearer {self._token}"}

    # --- GET con retry/backoff (429 e 5xx) ---

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(8):
            try:
                response = await asyncio.wait_for(
                    self._client.get(
                        url,
                        headers=await self._headers(),
                        params={k: v for k, v in (params or {}).items() if v is not None},
                    ),
                    timeout=_REQUEST_HARD_CAP_S,
                )
            except TimeoutError:
                # Risposta appesa (trickle-hang Graph): abort e retry su
                # connessione fresca, stessa politica del throttling
                log.warning(
                    "Graph richiesta appesa oltre il cap, retry",
                    url=url[:120],
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                # Onora il Retry-After di Microsoft fino a 120s: ignorarlo o
                # tagliarlo peggiora il throttling (finestre di penalità
                # crescenti sul tenant)
                wait = float(retry_after) if retry_after else delay
                log.warning(
                    "Graph throttling/errore, retry",
                    status=response.status_code,
                    wait=wait,
                )
                await asyncio.sleep(min(wait, 120.0))
                delay = min(delay * 2, 120.0)
                continue
            if response.status_code == 401 and attempt == 0:
                # Token rifiutato: forza il rinnovo una sola volta
                self._token = None
                continue
            if response.status_code >= 400:
                detail = response.text[:300]
                raise M365AuthError(
                    f"Graph API HTTP {response.status_code} su {url}: {detail}"
                )
            return response.json()
        raise M365AuthError(f"Graph API: troppi retry su {url}")

    async def get_all(
        self, url: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Itera tutte le pagine di una collection Graph."""
        next_url: str | None = url
        first = True
        while next_url:
            page = await self.get_json(next_url, params if first else None)
            first = False
            for node in page.get("value", []):
                yield node
            next_url = page.get("@odata.nextLink")

    async def download(
        self, url: str, dest: Path, on_progress: Callable[[int], None]
    ) -> int:
        """Scarica un file in ``dest`` chiamando ``on_progress(bytes_done)``."""
        done = 0
        async with self._client.stream(
            "GET", url, headers=await self._headers()
        ) as response:
            if response.status_code >= 400:
                raise M365AuthError(
                    f"Download fallito HTTP {response.status_code} per {url}"
                )
            with open(dest, "wb") as fh:
                async for block in response.aiter_bytes(1 << 20):
                    fh.write(block)
                    done += len(block)
                    on_progress(done)
        return done


# ---------------------------------------------------------------------------
# Device code flow (stato condiviso tra endpoint e job)
# ---------------------------------------------------------------------------


@dataclass
class _DeviceFlow:
    """Flusso device code in corso/completato (uno alla volta per processo)."""

    tenant: str
    client_id: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float
    refresh_token: str | None = None


_device_flow: _DeviceFlow | None = None


async def begin_device_flow(
    tenant: str | None = None, client_id: str | None = None
) -> dict[str, Any]:
    """Richiede un nuovo codice dispositivo a Entra ID.

    L'utente approva su ``verification_uri`` inserendo ``user_code`` (con
    MFA); da quel momento il flusso ottiene un refresh token riutilizzabile.
    """
    global _device_flow
    tenant = tenant or "organizations"
    client_id = client_id or _DEFAULT_PUBLIC_CLIENT
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_LOGIN}/{tenant}/oauth2/v2.0/devicecode",
            data={
                "client_id": client_id,
                "scope": "https://graph.microsoft.com/.default offline_access",
            },
        )
    payload = response.json()
    if "device_code" not in payload:
        raise M365AuthError(
            "Impossibile avviare il device code flow: "
            + payload.get("error_description", str(payload))[:400]
        )
    _device_flow = _DeviceFlow(
        tenant=tenant,
        client_id=client_id,
        device_code=payload["device_code"],
        user_code=payload["user_code"],
        verification_uri=payload["verification_uri"],
        interval=int(payload.get("interval", 5)),
        expires_at=time.monotonic() + int(payload.get("expires_in", 900)),
    )
    return device_flow_status()


def device_flow_status() -> dict[str, Any]:
    """Stato pubblico del flusso device code (per la UI)."""
    if _device_flow is None:
        return {"active": False}
    return {
        "active": True,
        "user_code": _device_flow.user_code,
        "verification_uri": _device_flow.verification_uri,
        "authorized": _device_flow.refresh_token is not None,
        "expires_in_seconds": max(0, round(_device_flow.expires_at - time.monotonic())),
    }


async def try_complete_device_flow() -> dict[str, Any]:
    """Tenta UNA richiesta al token endpoint: se l'utente ha già approvato
    il codice, cattura e salva il refresh token nel flusso.

    Chiamata dal polling di ``/api/m365/device/status``: il refresh token
    viene catturato solo quando qualcuno interroga il token endpoint, e il
    job potrebbe partire dopo — senza questo completamento attivo lo stato
    resterebbe ``authorized: false`` anche ad approvazione avvenuta.
    """
    flow = _device_flow
    if flow is None or flow.refresh_token is not None:
        return device_flow_status()
    if time.monotonic() > flow.expires_at:
        return device_flow_status()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_LOGIN}/{flow.tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": flow.client_id,
                "device_code": flow.device_code,
            },
        )
    payload = response.json()
    if "access_token" in payload and payload.get("refresh_token"):
        flow.refresh_token = payload["refresh_token"]
        log.info("device code flow approvato: refresh token catturato")
    return device_flow_status()


# ---------------------------------------------------------------------------
# Stato del job
# ---------------------------------------------------------------------------


@dataclass
class _FileTask:
    """Un elemento da scaricare e indicizzare."""

    category: str  # onedrive | sharepoint | mail | allegato
    title: str
    url: str  # URL Graph del contenuto
    size: int
    external_id: str
    modified_at: datetime | None = None
    author: str = ""
    raw_bytes: bytes | None = None  # per i messaggi mail (già in memoria)


@dataclass
class _JobState:
    """Stato osservabile del job di import."""

    state: str = "idle"  # idle|running|done|error|cancelled
    phase: str = ""  # auth|scan|download|ingest
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    dry_run: bool = False
    # Scan: conteggi per categoria
    scan: dict[str, int] = field(default_factory=lambda: {
        "onedrive_files": 0,
        "sharepoint_sites": 0,
        "sharepoint_files": 0,
        "mail_messages": 0,
        "mail_attachments": 0,
    })
    # Download
    total_files: int = 0
    done_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    current_file: str = ""
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    # Ingest (documenti accodati nella pipeline)
    ingest_queued: int = 0
    ingest_indexed: int = 0
    ingest_failed: int = 0
    ingest_pending: int = 0
    # Eventi ed errori
    events: deque = field(default_factory=lambda: deque(maxlen=_EVENTS_MAX))
    file_errors: list = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "dry_run": self.dry_run,
            "scan": dict(self.scan),
            "download": {
                "total_files": self.total_files,
                "done_files": self.done_files,
                "failed_files": self.failed_files,
                "skipped_files": self.skipped_files,
                "total_bytes": self.total_bytes,
                "done_bytes": self.done_bytes,
                "current_file": self.current_file,
                "speed_bps": round(self.speed_bps),
                "eta_seconds": round(self.eta_seconds) if self.eta_seconds is not None else None,
                "percent": round(100 * self.done_files / self.total_files, 1)
                if self.total_files else 0,
            },
            "ingest": {
                "queued": self.ingest_queued,
                "indexed": self.ingest_indexed,
                "failed": self.ingest_failed,
                "pending": self.ingest_pending,
            },
            "events": list(self.events)[::-1],
            "file_errors": self.file_errors[-50:],
        }


# ---------------------------------------------------------------------------
# Job di import
# ---------------------------------------------------------------------------


class ImportJob:
    """Job di import M365: auth → scan → download → attesa ingest."""

    def __init__(self) -> None:
        self._state = _JobState()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._client: GraphClient | None = None
        self._source_id: int | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        # Finestra mobile per la velocità di download (bytes, timestamp)
        self._speed_window: deque = deque(maxlen=400)
        # Dedup scan (driveId:itemId): anti-duplicati di paginazione Graph
        self._seen_folders: set[str] = set()
        self._seen_files: set[str] = set()
        # Tetto dimensione file (byte) e contatore dei salti per superamento
        self._max_file_bytes: int = _DEFAULT_MAX_FILE_MB * (1 << 20)
        self._oversize_skipped: int = 0

    @property
    def _graph(self) -> GraphClient:
        """Il client Graph del job (sempre presente durante le fasi)."""
        assert self._client is not None
        return self._client

    # --- API pubblica ---

    def status(self) -> dict[str, Any]:
        return self._state.snapshot()

    def start(
        self,
        auth: dict[str, Any],
        options: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        if self._task is not None and not self._task.done():
            raise M365JobRunningError("Un import è già in esecuzione")
        self._state = _JobState(
            state="running",
            phase="auth",
            started_at=datetime.now(UTC).isoformat(),
            dry_run=bool(options.get("dry_run")),
        )
        self._cancel = asyncio.Event()
        self._session_factory = session_factory
        self._speed_window.clear()
        self._seen_folders.clear()
        self._seen_files.clear()
        self._oversize_skipped = 0
        self._task = asyncio.create_task(self._run(auth, options))

    async def cancel(self) -> None:
        self._cancel.set()

    # --- utilità di stato ---

    def _event(self, message: str, level: str = "info") -> None:
        self._state.events.append(
            {"at": datetime.now(UTC).isoformat(), "level": level, "message": message}
        )
        log.info("m365 import", msg=message, level=level)

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise asyncio.CancelledError

    def _skip_oversize(self, title: str, size: int) -> None:
        """File oltre il tetto ``max_file_mb``: conteggia e logga in aggregato
        (un evento ogni ``_OVERSIZE_LOG_EVERY`` salti, non uno per file)."""
        self._state.skipped_files += 1
        self._oversize_skipped += 1
        if self._oversize_skipped == 1 or self._oversize_skipped % _OVERSIZE_LOG_EVERY == 0:
            self._event(
                f"{self._oversize_skipped} file oltre il tetto di "
                f"{self._max_file_bytes >> 20} MB saltati "
                f"(ultimo: {title}, {size / (1 << 20):.0f} MB)"
            )

    def _track_bytes(self, n: int) -> None:
        now = time.monotonic()
        self._speed_window.append((n, now))
        cutoff = now - 30.0
        while self._speed_window and self._speed_window[0][1] < cutoff:
            self._speed_window.popleft()
        window = self._speed_window
        if len(window) > 1:
            span = max(window[-1][1] - window[0][1], 0.5)
            self._state.speed_bps = sum(b for b, _ in window) / span
        remaining = self._state.total_bytes - self._state.done_bytes
        if self._state.speed_bps > 0 and remaining > 0:
            self._state.eta_seconds = remaining / self._state.speed_bps
        else:
            self._state.eta_seconds = None

    # --- fasi del job ---

    async def _run(self, auth: dict[str, Any], options: dict[str, Any]) -> None:
        st = self._state
        try:
            self._client = GraphClient(auth, cancel_event=self._cancel)
            # Fase 1: autenticazione
            await self._client._acquire_token()
            self._event("Autenticazione a Microsoft 365 riuscita")

            # Fase 2: scansione (raccolta di tutto ciò che va scaricato)
            st.phase = "scan"
            tasks = await self._scan(options)
            # Dedup finale per external_id (oltre ai set dello scan): lo
            # stesso item non deve mai essere scaricato due volte
            tasks = list({t.external_id: t for t in tasks}.values())
            st.total_files = len(tasks)
            st.total_bytes = sum(t.size for t in tasks)
            self._event(
                f"Scansione completata: {st.total_files} elementi "
                f"({st.total_bytes / (1 << 20):.1f} MB) da importare"
            )

            if st.dry_run:
                st.state = "done"
                st.finished_at = datetime.now(UTC).isoformat()
                self._event("Scansione (anteprima) terminata: nessun download")
                return

            # Fase 3: download + accodamento ingest
            st.phase = "download"
            await self._download_all(tasks)

            # Fase 4: attesa che la pipeline finisca di indicizzare
            st.phase = "ingest"
            await self._wait_ingest()

            st.state = "done"
            st.finished_at = datetime.now(UTC).isoformat()
            self._event(
                f"Import completato: {st.done_files} scaricati, "
                f"{st.failed_files} falliti, {st.ingest_indexed} indicizzati"
            )
        except asyncio.CancelledError:
            st.state = "cancelled"
            st.finished_at = datetime.now(UTC).isoformat()
            self._event("Import annullato dall'utente", "warning")
        except Exception as exc:
            st.state = "error"
            st.error = str(exc)
            st.finished_at = datetime.now(UTC).isoformat()
            self._event(f"Import fallito: {exc}", "error")
            log.error("m365 import fallito", error=str(exc))
        finally:
            if self._client is not None:
                await self._client.close()

    async def _users_scope(self, options: dict[str, Any]) -> list[dict[str, str]]:
        """Utenze da importare: /me con token delegato, uno/tutti in app-only."""
        auth_mode = self._graph._auth.get("mode") or "password"
        explicit_user = options.get("user") or self._graph._auth.get("username")
        if auth_mode in ("password", "device_code"):  # token delegato: solo /me
            me = await self._graph.get_json(
                f"{_GRAPH}/me", {"$select": "id,displayName,userPrincipalName"}
            )
            self._event(f"Utente: {me.get('displayName')} <{me.get('userPrincipalName')}>")
            return [me]
        if explicit_user:
            user = await self._graph.get_json(
                f"{_GRAPH}/users/{explicit_user}",
                {"$select": "id,displayName,userPrincipalName"},
            )
            return [user]
        # Lista esplicita di UPN: gli endpoint drive/mail accettano l'UPN
        # come id, quindi non serve alcun permesso di lettura directory
        explicit_users = options.get("users") or []
        if explicit_users:
            self._event(f"Utenze esplicite: {len(explicit_users)}")
            return [
                {"id": upn, "displayName": upn, "userPrincipalName": upn}
                for upn in explicit_users
            ]
        try:
            users = [
                u
                async for u in self._graph.get_all(
                    f"{_GRAPH}/users",
                    {"$select": "id,displayName,userPrincipalName", "$top": "100"},
                )
            ]
        except M365AuthError as exc:
            raise M365AuthError(
                "L'app registration non può enumerare le utenze "
                "(/users negato): aggiungere il permesso applicativo "
                "User.Read.All con consenso amministratore, oppure passare "
                "la lista esplicita delle utenze (campo 'users'). "
                f"Dettaglio: {exc}"
            ) from exc
        self._event(f"Tenant: {len(users)} utenze da importare")
        return users

    async def _scan_drive_search(
        self, drive_id: str, category: str, tasks: list[_FileTask],
        path_prefix: str = "",
    ) -> None:
        """Enumera i file del drive via search Graph, un'estensione alla volta.

        Alternativa "veloce" all'enumerazione completa: chiede all'indice di
        OneDrive solo i file con estensioni supportate, ignorando milioni di
        elementi non documentali senza percorrere l'albero delle cartelle.
        Trade-off documentato: la completezza dipende dall'indice di ricerca
        (latenza di indicizzazione, limiti sui risultati) — per questo resta
        una modalità opzionale (``scan_mode="search"``) e non il default.
        La search copre sempre l'intero drive: con ``path_prefix`` (scoping
        per cartella) i file fuori dal prefisso vengono scartati.
        """
        select = "id,name,size,lastModifiedDateTime,file,parentReference"
        for ext in sorted(_SUPPORTED):
            self._check_cancel()
            found = 0
            url = (
                f"{_GRAPH}/drives/{drive_id}/root/search(q='{ext}')"
                f"?$select={select}&$top=200"
            )
            try:
                async for node in self._graph.get_all(url):
                    self._check_cancel()
                    name = node.get("name", node["id"])
                    if "file" not in node or "deleted" in node:
                        continue
                    # La search matcha anche nomi/contenuti parziali: teniamo
                    # solo i file con l'estensione esatta richiesta
                    if Path(name).suffix.lower() != ext:
                        continue
                    parent = node.get("parentReference", {})
                    node_key = f"{parent.get('driveId', drive_id)}:{node['id']}"
                    if node_key in self._seen_files:
                        continue
                    self._seen_files.add(node_key)
                    # parentReference.path ha la forma
                    # "/drives/<id>/root:/Cartella/Sotto" ("root:" per la root)
                    rel = parent.get("path", "").split("root:", 1)[-1].strip("/")
                    # Scoping per cartella: la search copre tutto il drive,
                    # teniamo solo i file sotto il prefisso richiesto
                    if path_prefix:
                        base = path_prefix.strip("/").casefold()
                        if rel.casefold() != base and not rel.casefold().startswith(
                            base + "/"
                        ):
                            continue
                    title = f"{rel}/{name}" if rel else name
                    node_size = int(node.get("size", 0) or 0)
                    if node_size > self._max_file_bytes:
                        self._skip_oversize(title, node_size)
                        continue
                    tasks.append(
                        _FileTask(
                            category=category,
                            title=title,
                            url=(
                                f"{_GRAPH}/drives/{parent.get('driveId', drive_id)}"
                                f"/items/{node['id']}/content"
                            ),
                            size=node_size,
                            external_id=f"m365://{category}/{node['id']}",
                            modified_at=_parse_dt(node.get("lastModifiedDateTime")),
                        )
                    )
                    found += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(f"Ricerca '{ext}' fallita: {exc}", "warning")
            if found:
                self._event(f"Ricerca '{ext}': {found} file trovati")

    async def _scan_drive(
        self, drive_url: str, category: str, tasks: list[_FileTask], prefix: str = ""
    ) -> None:
        """Enumera ricorsivamente i file di un drive.

        BFS con pool di worker concorrenti: su drive molto grandi (centinaia
        di migliaia di elementi) la DFS sequenziale impiegava tempi biblici;
        con N richieste parallele il collo di bottiglia resta il rate limit
        di Graph (gestito da retry/backoff nel client).

        Elementi CONDIVISI (``remoteItem``): le cartelle/file condivisi da
        altri utenti compaiono nel drive del ricevente solo come riferimento
        al drive del proprietario; la ricorsione prosegue sul drive remoto
        (``remoteItem.parentReference.driveId`` + ``remoteItem.id``). La
        dedup usa la chiave ``driveId:itemId`` del drive remoto, così un
        contenuto visto sia dal proprietario sia dal ricevente è enumerato
        una volta sola. Un drive remoto non leggibile (403/404) produce solo
        un warning: il job prosegue.
        """
        select = (
            "id,name,size,lastModifiedDateTime,file,folder,deleted,"
            "parentReference,remoteItem"
        )
        work: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        await work.put((f"{drive_url}?$select={select}&$top=200", prefix))
        # Dedup per item id: Graph può restituire lo stesso figlio in più
        # pagine durante la paginazione di collection molto grandi; senza
        # questi insiemi lo stesso sottoalbero verrebbe ri-percorso (e
        # ri-contato) all'infinito. Condivisi tra tutti i drive del job.
        seen_folders = self._seen_folders
        seen_files = self._seen_files

        async def _worker() -> None:
            while True:
                item = await work.get()
                try:
                    if item is None:
                        return
                    url, pfx = item
                    try:
                        async for node in self._graph.get_all(url):
                            self._check_cancel()
                            name = node.get("name", node["id"])
                            drive_id = node.get("parentReference", {}).get("driveId", "")
                            node_key = f"{drive_id}:{node['id']}"
                            remote = node.get("remoteItem")
                            if remote is not None and "deleted" not in node:
                                # Elemento condiviso: cartella/file che vive
                                # sul drive del PROPRIETARIO — si prosegue là,
                                # con dedup sulla chiave del drive remoto
                                remote_drive = (
                                    remote.get("parentReference") or {}
                                ).get("driveId", "")
                                remote_id = remote.get("id", "")
                                remote_key = f"{remote_drive}:{remote_id}"
                                # Presenza del facet (può essere un dict vuoto)
                                if "folder" in remote:
                                    if remote_key in seen_folders:
                                        continue
                                    seen_folders.add(remote_key)
                                    if name.lower() in _EXCLUDED_DIR_NAMES:
                                        self._event(
                                            f"Cartella esclusa dallo scan: {pfx}{name}/"
                                        )
                                        continue
                                    await work.put(
                                        (
                                            f"{_GRAPH}/drives/{remote_drive}"
                                            f"/items/{remote_id}/children"
                                            f"?$select={select}&$top=200",
                                            f"{pfx}{name}/",
                                        )
                                    )
                                elif "file" in remote:
                                    if remote_key in seen_files:
                                        continue
                                    seen_files.add(remote_key)
                                    suffix = Path(name).suffix.lower()
                                    if suffix not in _SUPPORTED:
                                        self._state.skipped_files += 1
                                        continue
                                    remote_size = int(
                                        remote.get("size")
                                        or node.get("size", 0)
                                        or 0
                                    )
                                    if remote_size > self._max_file_bytes:
                                        self._skip_oversize(
                                            f"{pfx}{name}", remote_size
                                        )
                                        continue
                                    tasks.append(
                                        _FileTask(
                                            category=category,
                                            title=f"{pfx}{name}",
                                            url=(
                                                f"{_GRAPH}/drives/{remote_drive}"
                                                f"/items/{remote_id}/content"
                                            ),
                                            size=remote_size,
                                            external_id=(
                                                f"m365://{category}/{remote_id}"
                                            ),
                                            modified_at=_parse_dt(
                                                remote.get("lastModifiedDateTime")
                                                or node.get("lastModifiedDateTime")
                                            ),
                                        )
                                    )
                                continue
                            if "folder" in node and "deleted" not in node:
                                if node_key in seen_folders:
                                    continue
                                seen_folders.add(node_key)
                                if name.lower() in _EXCLUDED_DIR_NAMES:
                                    self._event(
                                        f"Cartella esclusa dallo scan: {pfx}{name}/"
                                    )
                                    continue
                                await work.put(
                                    (
                                        f"{_GRAPH}/drives/{drive_id}"
                                        f"/items/{node['id']}/children"
                                        f"?$select={select}&$top=200",
                                        f"{pfx}{name}/",
                                    )
                                )
                            elif "file" in node and "deleted" not in node:
                                if node_key in seen_files:
                                    continue
                                seen_files.add(node_key)
                                suffix = Path(name).suffix.lower()
                                if suffix not in _SUPPORTED:
                                    self._state.skipped_files += 1
                                    continue
                                node_size = int(node.get("size", 0) or 0)
                                if node_size > self._max_file_bytes:
                                    self._skip_oversize(f"{pfx}{name}", node_size)
                                    continue
                                tasks.append(
                                    _FileTask(
                                        category=category,
                                        title=f"{pfx}{name}",
                                        url=(
                                            f"{_GRAPH}/drives/{drive_id}"
                                            f"/items/{node['id']}/content"
                                        ),
                                        size=node_size,
                                        external_id=f"m365://{category}/{node['id']}",
                                        modified_at=_parse_dt(
                                            node.get("lastModifiedDateTime")
                                        ),
                                    )
                                )
                    except asyncio.CancelledError:
                        # Drena la coda così work.join() si sblocca e gli
                        # altri worker possono terminare a loro volta
                        while not work.empty():
                            try:
                                work.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            work.task_done()
                        raise
                    except Exception as exc:
                        self._event(
                            f"Cartella non leggibile ({pfx or 'root'}): {exc}",
                            "warning",
                        )
                finally:
                    work.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(_SCAN_WORKERS)]
        try:
            await work.join()
        except BaseException:
            for worker in workers:
                worker.cancel()
            raise
        for _ in workers:
            await work.put(None)
        await asyncio.gather(*workers)

    async def _scan_mailbox(self, user_id: str, tasks: list[_FileTask]) -> None:
        """Enumera tutte le cartelle di posta e i messaggi (con allegati)."""
        folders: list[tuple[str, str]] = []  # (id, nome)
        queue: list[tuple[str, str]] = []

        async def _children(url: str, path: str) -> None:
            async for folder in self._graph.get_all(
                url, {"$select": "id,displayName,totalItemCount", "$top": "100"}
            ):
                folder_path = f"{path}{folder['displayName']}"
                folders.append((folder["id"], folder_path))
                child_url = (
                    f"{_GRAPH}/users/{user_id}"
                    f"/mailFolders/{folder['id']}/childFolders"
                )
                queue.append((child_url, f"{folder_path}/"))

        await _children(f"{_GRAPH}/users/{user_id}/mailFolders", "")
        while queue:
            url, path = queue.pop()
            self._check_cancel()
            await _children(url, path)

        for folder_id, folder_path in folders:
            self._check_cancel()
            select = "id,subject,from,receivedDateTime,hasAttachments,bodyPreview"
            async for message in self._graph.get_all(
                f"{_GRAPH}/users/{user_id}/mailFolders/{folder_id}/messages",
                {"$select": select, "$top": "50"},
            ):
                subject = message.get("subject") or "(senza oggetto)"
                tasks.append(
                    _FileTask(
                        category="mail",
                        title=f"{folder_path}/{subject}",
                        url=f"{_GRAPH}/users/{user_id}/messages/{message['id']}",
                        size=0,
                        external_id=f"m365://mail/{message['id']}",
                        modified_at=_parse_dt(message.get("receivedDateTime")),
                        author=(message.get("from") or {})
                        .get("emailAddress", {})
                        .get("address", ""),
                    )
                )

    async def _scan_folder_delta(
        self, drive_id: str, item_id: str, path: str, tasks: list[_FileTask]
    ) -> None:
        """Enumera il sottoalbero di una cartella via endpoint ``delta``.

        Nato il 3/09/26: su drive grandi la BFS dei children muore di
        throttling (429 continui, vedi import 'Presente') e la search(q=)
        pagina all'infinito senza mai restituire i file della cartella.
        ``/drives/{drive}/items/{item}/delta`` è l'API pensata per
        l'enumerazione completa di un sottoalbero: poche pagine grandi,
        nessuna ricorsione, throttling molto più blando (12,8k nodi in
        ~30s sul caso reale). Come la search, i percorsi arrivano dal
        ``parentReference`` e vengono filtrati sul prefisso della cartella.

        ⚠️ 4/09/26: su questo drive il delta **non emette mai deltaLink**:
        esauriti i nodi veri ripropone all'infinito gli stessi 201 nodi con
        lo stesso token di paginazione (verificato anche via BFS: l'albero
        ha ~1000 nodi unici, il delta ne restituisce milioni). La scansione
        si interrompe quindi quando due pagine consecutive non portano nodi
        nuovi: senza questo guardrail lo scan non terminerebbe mai.
        """
        url = f"{_GRAPH}/drives/{drive_id}/items/{item_id}/delta"
        base = path.strip("/").casefold()
        # NIENTE $select su questo endpoint: collaudato 3/09/26 — con $select
        # la prima pagina non arriva mai (hang lato Graph), senza il delta
        # restituisce comunque tutti i facet necessari e vola (12,8k nodi/30s)
        seen = 0
        seen_ids: set[str] = set()
        stale_pages = 0
        next_url: str | None = url
        params: dict[str, Any] | None = {"$top": "999"}
        while next_url:
            self._check_cancel()
            page = await self._graph.get_json(next_url, params)
            params = None  # dal secondo giro i parametri sono nel nextLink
            new_in_page = 0
            for node in page.get("value", []):
                self._check_cancel()
                seen += 1
                if seen % 2000 == 0:
                    # Il contatore di stato si aggiorna solo a fine cartella:
                    # senza questi eventi uno scan lungo sembra piantato (4/09/26)
                    self._event(
                        f"Scansione /{path}: {seen} nodi esaminati, "
                        f"{len(tasks)} file trovati finora"
                    )
                node_id = node.get("id")
                if node_id is not None:
                    if node_id in seen_ids:
                        continue
                    seen_ids.add(node_id)
                new_in_page += 1
                name = node.get("name", node["id"])
                if "file" not in node or "deleted" in node:
                    continue
                if Path(name).suffix.lower() not in _SUPPORTED:
                    continue
                parent = node.get("parentReference", {})
                node_key = f"{parent.get('driveId', drive_id)}:{node['id']}"
                if node_key in self._seen_files:
                    continue
                self._seen_files.add(node_key)
                # parentReference.path: "/drives/<id>/root:/Cartella/Sotto"
                rel = parent.get("path", "").split("root:", 1)[-1].strip("/")
                # Il delta di un item include solo il suo sottoalbero, ma il
                # filtro sul prefisso è una verifica di coerenza a costo zero
                if base and rel.casefold() != base and not rel.casefold().startswith(
                    base + "/"
                ):
                    continue
                title = f"{rel}/{name}" if rel else name
                node_size = int(node.get("size", 0) or 0)
                if node_size > self._max_file_bytes:
                    self._skip_oversize(title, node_size)
                    continue
                tasks.append(
                    _FileTask(
                        category="onedrive",
                        title=title,
                        url=(
                            f"{_GRAPH}/drives/{parent.get('driveId', drive_id)}"
                            f"/items/{node['id']}/content"
                        ),
                        size=node_size,
                        external_id=f"m365://onedrive/{node['id']}",
                        modified_at=_parse_dt(node.get("lastModifiedDateTime")),
                    )
                )
            if new_in_page == 0:
                stale_pages += 1
                if stale_pages >= 2:
                    self._event(
                        f"Delta in ciclo su /{path}: interrotto dopo {seen} nodi "
                        f"({len(seen_ids)} unici)",
                        "warning",
                    )
                    break
            else:
                stale_pages = 0
            next_url = page.get("@odata.nextLink")

    async def _scan_folders(
        self, specs: list[tuple[str, str]], tasks: list[_FileTask],
        scan_mode: str = "enumerate",
    ) -> None:
        """Scan OneDrive limitato alle cartelle richieste (scoping per cartella).

        Ogni percorso è risolto via ``/users/{upn}/drive/root:/percorso:``
        (URL-encoded: spazi, ``&``, accenti) e la BFS standard riparte dai
        children dell'item risolto; con ``scan_mode="search"`` si usa invece
        la search Graph per estensione filtrata sul prefisso (molte meno
        richieste su alberi enormi, vedi throttling 429 del 2-3/09/26).
        Il titolo dei file conserva il percorso relativo prefissato dalla
        cartella scelta (es. "Presente/VIRTUALIS/doc.pdf"). Una cartella non
        risolvibile (404/permessi) produce solo un warning: il job prosegue.
        """
        st = self._state
        for upn, path in specs:
            self._check_cancel()
            try:
                item = await self._graph.get_json(
                    f"{_GRAPH}/users/{upn}/drive/root:/{quote(path)}:",
                    {"$select": "id,name,folder,parentReference"},
                )
                if "folder" not in item:
                    self._event(
                        f"'{upn}:/{path}' non è una cartella: ignorata", "warning"
                    )
                    continue
                drive_id = (item.get("parentReference") or {}).get("driveId", "")
                if not drive_id:
                    drive = await self._graph.get_json(
                        f"{_GRAPH}/users/{upn}/drive", {"$select": "id"}
                    )
                    drive_id = drive["id"]
                before = len(tasks)
                if scan_mode == "delta":
                    await self._scan_folder_delta(
                        drive_id, item["id"], path, tasks
                    )
                elif scan_mode == "search":
                    await self._scan_drive_search(
                        drive_id, "onedrive", tasks, path_prefix=path
                    )
                else:
                    await self._scan_drive(
                        f"{_GRAPH}/drives/{drive_id}/items/{item['id']}/children",
                        "onedrive",
                        tasks,
                        prefix=f"{path}/",
                    )
                st.scan["onedrive_files"] += len(tasks) - before
                self._event(f"Cartella {upn}:/{path}: {len(tasks) - before} file")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(
                    f"Cartella {upn}:/{path} non accessibile: {exc}", "warning"
                )

    async def _scan(self, options: dict[str, Any]) -> list[_FileTask]:
        st = self._state
        tasks: list[_FileTask] = []
        assert self._client is not None

        # Tetto dimensione file: default applicato anche agli avvii senza
        # campo esplicito (import da versioni precedenti della UI/API)
        self._max_file_bytes = (
            int(options.get("max_file_mb") or _DEFAULT_MAX_FILE_MB) * (1 << 20)
        )
        # Scoping per cartella: se presente, lo scan OneDrive NON parte dalla
        # root dei drive utente (e non serve enumerare le utenze per i drive)
        folder_specs = [parse_folder_spec(s) for s in options.get("folders") or []]

        users: list[dict[str, str]] = []
        onedrive_from_root = options.get("include_onedrive", True) and not folder_specs
        if onedrive_from_root or options.get("include_mail", True):
            users = await self._users_scope(options)

        # OneDrive
        if options.get("include_onedrive", True) and folder_specs:
            await self._scan_folders(
                folder_specs, tasks, options.get("scan_mode", "enumerate")
            )
        elif options.get("include_onedrive", True):
            for user in users:
                upn = user.get("userPrincipalName", "")
                try:
                    drive = await self._graph.get_json(
                        f"{_GRAPH}/users/{user['id']}/drive", {"$select": "id,name"}
                    )
                    before = len(tasks)
                    if options.get("scan_mode") == "search":
                        await self._scan_drive_search(drive["id"], "onedrive", tasks)
                    else:
                        await self._scan_drive(
                            f"{_GRAPH}/drives/{drive['id']}/root/children",
                            "onedrive",
                            tasks,
                        )
                    st.scan["onedrive_files"] += len(tasks) - before
                    self._event(f"OneDrive di {upn}: {len(tasks) - before} file")
                except Exception as exc:
                    self._event(f"OneDrive di {upn} non accessibile: {exc}", "warning")

        # SharePoint: tutti i siti e i loro drive
        if options.get("include_sharepoint", True):
            try:
                sites = [
                    s
                    async for s in self._graph.get_all(
                        f"{_GRAPH}/sites",
                        {"search": "*", "$select": "id,displayName,webUrl"},
                    )
                ]
            except Exception as exc:
                sites = []
                self._event(f"SharePoint non accessibile: {exc}", "warning")
            st.scan["sharepoint_sites"] = len(sites)
            for site in sites:
                self._check_cancel()
                try:
                    drives = [
                        d
                        async for d in self._graph.get_all(
                            f"{_GRAPH}/sites/{site['id']}/drives", {"$select": "id,name"}
                        )
                    ]
                    for drive in drives:
                        before = sum(
                            1 for t in tasks if t.category == "sharepoint"
                        )
                        await self._scan_drive(
                            f"{_GRAPH}/drives/{drive['id']}/root/children",
                            "sharepoint",
                            tasks,
                            prefix=f"{site.get('displayName', '')}/",
                        )
                        added = sum(1 for t in tasks if t.category == "sharepoint") - before
                        st.scan["sharepoint_files"] += added
                except Exception as exc:
                    self._event(
                        f"Sito {site.get('displayName')} non accessibile: {exc}", "warning"
                    )
            if sites:
                self._event(
                    f"SharePoint: {st.scan['sharepoint_sites']} siti, "
                    f"{st.scan['sharepoint_files']} file"
                )

        # Posta
        if options.get("include_mail", True):
            for user in users:
                upn = user.get("userPrincipalName", "")
                try:
                    before = sum(1 for t in tasks if t.category == "mail")
                    await self._scan_mailbox(user["id"], tasks)
                    added = sum(1 for t in tasks if t.category == "mail") - before
                    st.scan["mail_messages"] += added
                    self._event(f"Mailbox di {upn}: {added} messaggi")
                except Exception as exc:
                    self._event(f"Mailbox di {upn} non accessibile: {exc}", "warning")

        return tasks

    # --- download ---

    async def _get_source_id(self) -> int:
        assert self._session_factory is not None
        if self._source_id is not None:
            return self._source_id
        async with self._session_factory() as db:
            result = await db.execute(
                select(Source).where(
                    Source.type == SourceType.m365.value,
                    Source.name == M365_SOURCE_NAME,
                )
            )
            source = result.scalar_one_or_none()
            if source is None:
                source = Source(
                    name=M365_SOURCE_NAME,
                    type=SourceType.m365.value,
                    config_json={},
                )
                db.add(source)
                await db.commit()
                await db.refresh(source)
            self._source_id = source.id
            return source.id

    async def _fetch_mail(
        self, task: _FileTask, dest_dir: Path
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Scarica un messaggio come .md + eventuali allegati. Ritorna i file."""
        assert self._client is not None
        select = "subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments"
        message = await self._graph.get_json(task.url, {"$select": select})
        subject = message.get("subject") or "(senza oggetto)"
        body = message.get("body") or {}
        content = body.get("content", "")
        if (body.get("contentType") or "").lower() == "html":
            content = _html_to_text(content)
        sender = (message.get("from") or {}).get("emailAddress", {})
        to = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in message.get("toRecipients", [])
        )
        cc = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in message.get("ccRecipients", [])
        )
        markdown = (
            f"# {subject}\n\n"
            f"- **Da:** {sender.get('name', '')} <{sender.get('address', '')}>\n"
            f"- **A:** {to}\n"
            + (f"- **Cc:** {cc}\n" if cc else "")
            + f"- **Data:** {message.get('receivedDateTime', '')}\n\n---\n\n{content}\n"
        )
        dest = dest_dir / f"{uuid.uuid4().hex}.md"
        dest.write_text(markdown, encoding="utf-8")
        self._track_bytes(len(markdown))
        self._state.done_bytes += len(markdown)
        files = [
            (
                dest,
                {
                    "title": f"Email: {subject}"[:256],
                    "author": sender.get("address", ""),
                    "modified_at": _parse_dt(message.get("receivedDateTime")),
                },
            )
        ]

        # Allegati
        if message.get("hasAttachments"):
            try:
                async for attachment in self._graph.get_all(
                    f"{task.url}/attachments",
                    {"$select": "id,name,size,contentType", "$top": "50"},
                ):
                    name = attachment.get("name") or "allegato"
                    suffix = Path(name).suffix.lower()
                    if suffix not in _SUPPORTED:
                        self._state.skipped_files += 1
                        continue
                    att_size = int(attachment.get("size", 0) or 0)
                    if att_size > self._max_file_bytes:
                        self._skip_oversize(name, att_size)
                        continue
                    try:
                        response = await self._graph.get_json(
                            f"{task.url}/attachments/{attachment['id']}"
                        )
                        raw = base64.b64decode(response.get("contentBytes", ""))
                    except Exception:
                        # Fallback: stream del valore grezzo
                        att_dest = dest_dir / f"{uuid.uuid4().hex}{suffix}"
                        await self._graph.download(
                            f"{task.url}/attachments/{attachment['id']}/$value",
                            att_dest,
                            lambda n: None,
                        )
                        att_size = att_dest.stat().st_size
                        self._track_bytes(att_size)
                        self._state.done_bytes += att_size
                        files.append((att_dest, {"title": name}))
                        continue
                    att_dest = dest_dir / f"{uuid.uuid4().hex}{suffix}"
                    att_dest.write_bytes(raw)
                    self._track_bytes(len(raw))
                    self._state.done_bytes += len(raw)
                    self._state.scan["mail_attachments"] += 1
                    files.append((att_dest, {"title": name}))
            except Exception as exc:
                self._event(f"Allegati di '{subject}' non scaricabili: {exc}", "warning")
        return files

    async def _download_file(self, task: _FileTask, dest: Path) -> None:
        """Scarica un file Graph tracciando byte scaricati e velocità."""
        st = self._state
        progress = {"prev": 0}

        def _on_progress(done: int) -> None:
            # done è cumulativo per file: tracciamo i delta
            delta = done - progress["prev"]
            progress["prev"] = done
            st.done_bytes += delta
            self._track_bytes(delta)

        assert self._client is not None
        await self._graph.download(task.url, dest, _on_progress)

    async def _download_all(self, tasks: list[_FileTask]) -> None:
        st = self._state
        assert self._client is not None and self._session_factory is not None
        settings = get_settings()
        dest_dir = settings.upload_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        source_id = await self._get_source_id()

        for task in tasks:
            self._check_cancel()
            free = shutil.disk_usage(dest_dir).free
            if free < _MIN_FREE_BYTES:
                raise M365AuthError(
                    "Spazio disco insufficiente sul server "
                    f"({free / (1 << 30):.1f} GB liberi): import interrotto "
                    "per sicurezza. Liberare spazio e rilanciare; i file già "
                    "importati non verranno riscaricati (dedup per hash)."
                )
            st.current_file = task.title
            try:
                if task.category == "mail":
                    files = await self._fetch_mail(task, dest_dir)
                else:
                    suffix = Path(task.title).suffix.lower()
                    dest = dest_dir / f"{uuid.uuid4().hex}{suffix}"
                    await self._download_file(task, dest)
                    files = [
                        (
                            dest,
                            {
                                "title": Path(task.title).name,
                                "modified_at": task.modified_at,
                            },
                        )
                    ]

                for path, meta in files:
                    # Titolo: per i file di drive vince ``task.title`` (il
                    # percorso relativo "Cartella/Sotto/file.ext", che il nome
                    # estratto dal parser sovrascriveva perdendo la cartella
                    # di provenienza); per la posta resta prioritario il meta,
                    # che distingue corpo ("Email: ...") e allegati per nome
                    if task.category == "mail":
                        title = meta.get("title") or task.title
                    else:
                        title = task.title or meta.get("title") or ""
                    await pipeline.ingest_file(
                        path,
                        source_id,
                        # Il nome su disco resta nell'external_id (dopo '#'):
                        # rende i file ritrovabili dopo un riavvio a metà ingest
                        f"{task.external_id}#{path.name}",
                        self._session_factory,
                        doc_meta={
                            "title": title[:256],
                            "author": meta.get("author") or task.author,
                            "modified_at": meta.get("modified_at") or task.modified_at,
                        },
                    )
                    st.ingest_queued += 1
                st.done_files += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.failed_files += 1
                st.file_errors.append({"file": task.title, "error": str(exc)[:300]})
                self._event(f"Errore su '{task.title}': {exc}", "error")

    async def _wait_ingest(self) -> None:
        """Attende che la pipeline finisca i documenti della sorgente M365."""
        st = self._state
        assert self._session_factory is not None
        source_id = await self._get_source_id()
        idle_rounds = 0
        while True:
            self._check_cancel()
            async with self._session_factory() as db:
                rows = (
                    await db.execute(
                        select(Document.status, func.count())
                        .where(Document.source_id == source_id)
                        .group_by(Document.status)
                    )
                ).all()
            counts = {status: n for status, n in rows}
            st.ingest_indexed = counts.get("indexed", 0)
            st.ingest_failed = counts.get("failed", 0)
            st.ingest_pending = counts.get("pending", 0) + counts.get("processing", 0)
            if st.ingest_pending == 0:
                idle_rounds += 1
                if idle_rounds >= 3:  # stabile per ~6s: fine davvero
                    return
            else:
                idle_rounds = 0
            await asyncio.sleep(2)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html_content: str) -> str:
    """HTML mail → testo leggibile (strip tag + entità, senza dipendenze)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_content)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Singleton del job (uno per processo, come la pipeline)
_job = ImportJob()


def get_job() -> ImportJob:
    return _job


def job_status() -> dict[str, Any]:
    return _job.status()
