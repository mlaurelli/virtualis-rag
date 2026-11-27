"""Contratto comune dei connettori verso le sorgenti documentali.

Un connettore sa fare quattro cose:

1. **Enumerare** gli elementi remoti (:meth:`Connector.enumerate`): per
   ogni documento accessibile ritorna un :class:`RemoteItem` con
   ``external_id`` stabile, titolo, data di modifica, dimensione e metadati
   opzionali.
2. **Rilevare le variazioni** (:meth:`Connector.changes`): ritorna gli
   elementi nuovi/modificati e i *tombstone* (elementi spariti, da
   cancellare dall'indice) più il nuovo stato da persistere in
   ``Source.state_json``. L'implementazione di default esegue uno scan
   completo e lascia il diff (basato su content hash) al chiamante; i
   connettori con API delta (Drive changes, Graph delta) la sovrascrivono.
3. **Scaricare** un elemento (:meth:`Connector.fetch`) in un file locale
   sotto ``upload_dir/staging`` (o restituire il path originale, se il file
   è già accessibile localmente).
4. **Propagare le cancellazioni**: i tombstone devono tradursi in
   ``VectorIndex.delete_document`` + rimozione della riga Document.

Le credenziali vivono in ``Source.config_json``; se mancano, il connettore
solleva :class:`ConnectorNotConfiguredError` alla costruzione.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings


class ConnectorNotConfiguredError(Exception):
    """Il connettore non ha le credenziali/configurazione necessarie."""


@dataclass
class RemoteItem:
    """Elemento remoto enumerato da un connettore."""

    external_id: str  # id stabile nella sorgente (path, file id, ...)
    title: str
    modified_at: datetime | None = None
    size: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tombstone:
    """Marcatore di elemento eliminato dalla sorgente."""

    external_id: str


class Connector(ABC):
    """Interfaccia base dei connettori (vedi docstring del modulo)."""

    name: str = "base"

    def __init__(self, config: dict[str, Any], settings: Settings | None = None) -> None:
        self.config = config
        self.settings = settings or get_settings()

    @abstractmethod
    async def enumerate(self) -> list[RemoteItem]:
        """Elenca tutti gli elementi attualmente presenti nella sorgente."""

    @abstractmethod
    async def fetch(self, item: RemoteItem) -> Path:
        """Rende disponibile l'elemento come file locale e ne torna il path."""

    async def changes(
        self, state: dict[str, Any]
    ) -> tuple[list[RemoteItem | Tombstone], dict[str, Any]]:
        """Rileva variazioni rispetto a ``state``.

        Default: scan completo (nessun tombstone); il diff vero e proprio è
        fatto dal chiamante confrontando i content hash. I connettori con API
        delta sovrascrivono questo metodo.
        """
        items = await self.enumerate()
        return list(items), state
