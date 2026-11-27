"""Connettore Microsoft 365 / SharePoint / OneDrive (Graph API, DISABILITATO
di default: richiede extra ``m365``).

Configurazione in ``Source.config_json``::

    {
        "tenant_id": "...",
        "client_id": "...",
        "client_secret": "...",
        "drive_id": "ID del drive (SharePoint/OneDrive) da indicizzare"
    }

Dipendenze opzionali: ``pip install virtualis-backend[m365]`` (``msal``;
le chiamate HTTP usano ``httpx``, già dipendenza del progetto). Senza
credenziali il costruttore solleva :class:`ConnectorNotConfiguredError`.

Comportamento:

- autenticazione client-credentials via MSAL (scope
  ``https://graph.microsoft.com/.default``);
- enumerazione ricorsiva di ``/drives/{drive_id}/root/children``;
- :meth:`changes` usa ``/drives/{drive_id}/root/delta`` con ``deltaToken``
  persistito in ``Source.state_json`` (elementi modificati + tombstone per
  i file con facet ``deleted``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from ..config import Settings
from .base import (
    Connector,
    ConnectorNotConfiguredError,
    RemoteItem,
    Tombstone,
)

log = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SELECT = "id,name,size,lastModifiedDateTime,file,folder,deleted"


def _parse_dt(value: str | None) -> datetime | None:
    """Converte il timestamp ISO 8601 di Graph in datetime naive UTC."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


class M365Connector(Connector):
    """Connettore Microsoft Graph (drive SharePoint/OneDrive), sola lettura."""

    name = "m365"

    def __init__(self, config: dict[str, Any], settings: Settings | None = None) -> None:
        super().__init__(config, settings)
        missing = [
            key
            for key in ("tenant_id", "client_id", "client_secret", "drive_id")
            if not self.config.get(key)
        ]
        if missing:
            raise ConnectorNotConfiguredError(
                "Connettore m365: configurazione incompleta, mancano: "
                + ", ".join(missing)
            )
        try:
            import msal  # noqa: F401
        except ImportError as exc:
            raise ConnectorNotConfiguredError(
                "Connettore m365: installa le dipendenze opzionali con "
                "`uv pip install -e .[m365]`"
            ) from exc
        self._token: str | None = None

    # --- autenticazione ---

    async def _access_token(self) -> str:
        """Token di accesso app-only via MSAL (cache gestita da msal)."""

        def _acquire() -> str:
            import msal

            app = msal.ConfidentialClientApplication(
                self.config["client_id"],
                authority=f"https://login.microsoftonline.com/{self.config['tenant_id']}",
                client_credential=self.config["client_secret"],
            )
            result = app.acquire_token_silent(
                ["https://graph.microsoft.com/.default"], account=None
            ) or app.acquire_token_for_client(
                ["https://graph.microsoft.com/.default"]
            )
            if "access_token" not in result:
                raise ConnectorNotConfiguredError(
                    "Connettore m365: autenticazione fallita: "
                    + result.get("error_description", str(result))
                )
            return result["access_token"]

        return await asyncio.to_thread(_acquire)

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        """GET autenticata verso Graph con gestione paginazione minima."""
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={k: v for k, v in params.items() if v is not None},
            )
            response.raise_for_status()
            return response.json()

    # --- contratto Connector ---

    @staticmethod
    def _to_item(node: dict[str, Any]) -> RemoteItem | None:
        if "file" not in node or "deleted" in node:
            return None
        return RemoteItem(
            external_id=node["id"],
            title=node.get("name", node["id"]),
            modified_at=_parse_dt(node.get("lastModifiedDateTime")),
            size=int(node.get("size", 0) or 0),
            meta={"mime_type": node.get("file", {}).get("mimeType", "")},
        )

    async def enumerate(self) -> list[RemoteItem]:
        """Elenca ricorsivamente tutti i file del drive (paginato)."""
        drive_id = self.config["drive_id"]
        items: list[RemoteItem] = []
        queue: list[str] = [
            f"{_GRAPH_BASE}/drives/{drive_id}/root/children?$select={_SELECT}"
        ]
        while queue:
            url = queue.pop()
            page = await self._get(url)
            for node in page.get("value", []):
                if "folder" in node and "deleted" not in node:
                    queue.append(
                        f"{_GRAPH_BASE}/drives/{drive_id}/items/{node['id']}"
                        f"/children?$select={_SELECT}"
                    )
                else:
                    item = self._to_item(node)
                    if item is not None:
                        items.append(item)
            next_link = page.get("@odata.nextLink")
            if next_link:
                queue.append(next_link)
        return items

    async def fetch(self, item: RemoteItem) -> Path:
        """Scarica il file in ``upload_dir/staging``."""
        drive_id = self.config["drive_id"]
        staging = self.settings.upload_dir / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        suffix = Path(item.title).suffix or ".bin"
        dest = staging / f"{uuid.uuid4().hex}{suffix}"

        token = await self._access_token()
        url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item.external_id}/content"
        async with (
            httpx.AsyncClient(timeout=120, follow_redirects=True) as client,
            client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response,
        ):
            response.raise_for_status()
            with open(dest, "wb") as fh:
                async for block in response.aiter_bytes(1 << 20):
                    fh.write(block)
        log.info("download da M365", external_id=item.external_id, title=item.title)
        return dest

    async def changes(
        self, state: dict[str, Any]
    ) -> tuple[list[RemoteItem | Tombstone], dict[str, Any]]:
        """Delta via ``/root/delta`` con ``deltaToken`` persistito nello state."""
        drive_id = self.config["drive_id"]
        url = state.get("delta_link") or (
            f"{_GRAPH_BASE}/drives/{drive_id}/root/delta?$select={_SELECT}"
        )
        events: list[RemoteItem | Tombstone] = []
        new_state = dict(state)
        while url:
            page = await self._get(url)
            for node in page.get("value", []):
                if "deleted" in node:
                    events.append(Tombstone(external_id=node["id"]))
                else:
                    item = self._to_item(node)
                    if item is not None:
                        events.append(item)
            url = page.get("@odata.nextLink")
            delta_link = page.get("@odata.deltaLink")
            if delta_link:
                new_state["delta_link"] = delta_link
                url = None
        return events, new_state
