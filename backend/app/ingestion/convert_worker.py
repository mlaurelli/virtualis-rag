"""Worker persistente di conversione in sottoprocesso (isolamento memoria).

Perché: la conversione Docling di alcuni documenti patologici (planimetrie
raster, PDF enormi) può superare il ``mem_limit`` del container; l'OOM killer
del cgroup ucciderebbe l'INTERO processo Python dell'app, abbattendo anche i
job in corso (import M365, code). Eseguendo la conversione in un processo
figlio con limite di memoria proprio (``RLIMIT_AS``), il documento patologico
fallisce DA SOLO — marcato ``failed`` con un errore chiaro — e l'app resta
in piedi.

Architettura:

- un unico processo figlio **persistente** (la pipeline è seriale), creato
  con il context multiprocessing ``spawn`` (niente ``fork`` con asyncio):
  Docling carica modelli pesanti al primo uso e un processo nuovo per ogni
  documento pagherebbe il caricamento ogni volta; il figlio viene comunque
  **riciclato ogni ``convert_worker_max_tasks`` conversioni** perché
  torch/Docling non restituiscono la RSS al sistema e sotto carico il
  container andrebbe contro il ``mem_limit``;
- avvio lazy alla prima richiesta; comunicazione via ``Pipe`` con messaggi
  ``("convert", path, doc_meta)`` → ``("ok", NormalizedDoc)`` oppure
  ``("error", messaggio)``. :class:`~.converter.NormalizedDoc` è una
  dataclass di soli tipi base (picklabile), quindi attraversa la pipe senza
  rappresentazioni intermedie;
- se il figlio muore (OOM, segfault) o la richiesta scade, il padre solleva
  :class:`~.converter.ConversionError` con messaggio pulito e il worker viene
  ricreato automaticamente alla richiesta successiva;
- shutdown pulito con l'app tramite :func:`shutdown` (lifespan).

Hook di test (vedi ``tests/test_convert_worker.py``): le variabili d'ambiente
``_VIRTUALIS_TEST_CONVERT_EXIT`` e ``_VIRTUALIS_TEST_CONVERT_SLEEP_S``, lette
dal figlio a ogni richiesta, simulano in modo deterministico un figlio che
muore o lento (le monkeypatch non attraversano il confine di processo).
"""

from __future__ import annotations

import contextlib
import gc
import multiprocessing
import os
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import structlog

from ..config import get_settings
from .converter import ConversionError, NormalizedDoc, convert_document

log = structlog.get_logger(__name__)

# Messaggio utente quando il figlio esaurisce la memoria o muore di colpo
# (nel contesto del worker l'OOM è di gran lunga la causa più probabile)
MEMORY_ERROR_MESSAGE = (
    "Conversione interrotta: limite di memoria superato (file troppo complesso)"
)

# Timeout di default di una singola richiesta di conversione (la pipeline
# passa il proprio, allineato al timeout per documento)
DEFAULT_TIMEOUT_S = 18 * 60


# ---------------------------------------------------------------------------
# Lato figlio
# ---------------------------------------------------------------------------


def _apply_memory_limit(max_memory_mb: int) -> None:
    """Imposta ``RLIMIT_AS`` nel processo figlio (best-effort).

    Su Linux il limite è applicato (le allocazioni oltre soglia sollevano
    ``MemoryError`` o fanno fallire i malloc nativi); su macOS la chiamata
    riesce ma il kernel non la applica — accettabile: la produzione è Linux.
    """
    if max_memory_mb <= 0:
        return
    try:
        import resource

        limit_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, ValueError, OSError) as exc:
        log.warning(
            "limite di memoria del worker di conversione non applicato",
            max_memory_mb=max_memory_mb,
            error=str(exc),
        )


def _safe_send(conn: Connection, message: tuple[str, Any]) -> None:
    """Invia al padre; se la serializzazione esaurisce la memoria, degrada
    a un errore pulito invece di morire con il risultato a metà."""
    try:
        conn.send(message)
    except MemoryError:
        gc.collect()
        conn.send(("error", MEMORY_ERROR_MESSAGE))


def _child_main(conn: Connection, max_memory_mb: int) -> None:
    """Corpo del processo figlio: applica il limite di memoria e serve le
    richieste di conversione in loop finché il padre non chiude la pipe."""
    _apply_memory_limit(max_memory_mb)
    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return  # padre sparito o pipe chiusa: uscita pulita
        if not (isinstance(request, tuple) and request and request[0] == "convert"):
            return  # richiesta di stop
        _, path_str, doc_meta = request

        # Hook di test: simulano deterministicamente morte/lentezza del figlio
        if os.environ.get("_VIRTUALIS_TEST_CONVERT_EXIT"):
            os._exit(137)
        sleep_s = float(os.environ.get("_VIRTUALIS_TEST_CONVERT_SLEEP_S") or 0)
        if sleep_s:
            time.sleep(sleep_s)

        try:
            result = convert_document(Path(path_str), doc_meta=doc_meta)
        except MemoryError:
            gc.collect()
            _safe_send(conn, ("error", MEMORY_ERROR_MESSAGE))
        except ConversionError as exc:
            _safe_send(conn, ("error", str(exc)))
        except Exception as exc:  # rete di sicurezza: il loop deve sopravvivere
            _safe_send(
                conn, ("error", f"Impossibile convertire {Path(path_str).name}: {exc}")
            )
        else:
            _safe_send(conn, ("ok", result))


# ---------------------------------------------------------------------------
# Lato padre
# ---------------------------------------------------------------------------


class _WorkerCrashedError(Exception):
    """Il figlio è morto durante la richiesta (OOM killer, segfault...)."""


class _WorkerTimeoutError(Exception):
    """La richiesta non ha prodotto risposta entro il timeout."""


class _ConvertWorker:
    """Handle del processo figlio: avvio, richiesta sincrona, arresto."""

    def __init__(self, max_memory_mb: int) -> None:
        ctx = multiprocessing.get_context("spawn")
        self.conn, child_conn = ctx.Pipe()
        # daemon=True: se l'app muore senza shutdown, il figlio muore con lei
        self.process = ctx.Process(
            target=_child_main,
            args=(child_conn, max_memory_mb),
            daemon=True,
            name="virtualis-convert",
        )
        self.process.start()
        child_conn.close()
        log.info(
            "worker di conversione avviato",
            pid=self.process.pid,
            max_memory_mb=max_memory_mb,
        )

    def request(
        self, path: Path, doc_meta: dict[str, Any], timeout: float
    ) -> NormalizedDoc:
        """Invia una richiesta di conversione e attende la risposta."""
        try:
            self.conn.send(("convert", str(path), doc_meta))
        except (OSError, ValueError) as exc:
            raise _WorkerCrashedError(str(exc)) from exc
        # poll() torna True anche a pipe chiusa (EOF): la morte del figlio
        # durante la conversione emerge come EOFError dalla recv()
        if not self.conn.poll(timeout):
            raise _WorkerTimeoutError
        try:
            status, payload = self.conn.recv()
        except (EOFError, OSError) as exc:
            raise _WorkerCrashedError(
                f"exitcode={self.process.exitcode}"
            ) from exc
        if status == "ok":
            # payload deserializzato dalla pipe: il figlio invia un NormalizedDoc
            return payload
        raise ConversionError(str(payload))

    def close(self) -> None:
        """Arresto: stop cortese, poi kill se il figlio non esce in tempo."""
        with contextlib.suppress(Exception):
            self.conn.send(("stop",))
        with contextlib.suppress(Exception):
            self.conn.close()
        if self.process.is_alive():
            self.process.join(timeout=3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=3)


# Un solo worker per processo (la pipeline è seriale); il lock protegge le
# transizioni di stato dalle chiamate concorrenti via asyncio.to_thread
_worker: _ConvertWorker | None = None
_lock = threading.Lock()
_tasks_since_spawn = 0


def _discard_worker() -> None:
    """Elimina il worker corrente (con ``_lock`` già acquisito)."""
    global _worker, _tasks_since_spawn
    if _worker is not None:
        _worker.close()
        _worker = None
    _tasks_since_spawn = 0


def convert(
    path: Path | str,
    *,
    doc_meta: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> NormalizedDoc:
    """Converte ``path`` nel worker in sottoprocesso (bloccante).

    Va chiamata fuori dall'event loop (es. ``asyncio.to_thread``). Solleva
    :class:`ConversionError` con messaggio pulito se la conversione fallisce,
    se il figlio muore (limite di memoria) o se la richiesta scade; in questi
    ultimi due casi il worker è scartato e ricreato alla richiesta successiva.
    """
    global _worker, _tasks_since_spawn
    with _lock:
        if _worker is None or not _worker.process.is_alive():
            if _worker is not None:
                log.warning(
                    "worker di conversione morto tra una richiesta e l'altra",
                    exitcode=_worker.process.exitcode,
                )
            _discard_worker()
            _worker = _ConvertWorker(get_settings().convert_max_memory_mb)
        worker = _worker
        try:
            result = worker.request(Path(path), doc_meta or {}, timeout)
        except _WorkerCrashedError as exc:
            log.error(
                "worker di conversione morto durante la conversione",
                path=str(path),
                detail=str(exc),
            )
            _discard_worker()
            raise ConversionError(MEMORY_ERROR_MESSAGE) from exc
        except _WorkerTimeoutError:
            log.error(
                "conversione scaduta: worker terminato e ricreato al prossimo uso",
                path=str(path),
                timeout_s=timeout,
            )
            _discard_worker()
            raise ConversionError(
                f"Conversione interrotta: tempo massimo superato ({int(timeout)} s)"
            ) from None
        # Riciclo periodico: torch/Docling non restituiscono la RSS al sistema
        # e sotto carico il container va contro il mem_limit; ricreando il
        # figlio ogni N conversioni la memoria torna indietro. Il costo è il
        # reload dei modelli una volta ogni N documenti
        _tasks_since_spawn += 1
        max_tasks = get_settings().convert_worker_max_tasks
        if 0 < max_tasks <= _tasks_since_spawn:
            log.info(
                "riciclo worker di conversione dopo N conversioni",
                conversioni=_tasks_since_spawn,
            )
            _discard_worker()
        return result


def shutdown() -> None:
    """Arresta il worker (lifespan dell'app e test); il prossimo uso lo ricrea."""
    with _lock:
        if _worker is not None:
            _discard_worker()
            log.info("worker di conversione arrestato")
