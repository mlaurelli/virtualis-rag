"""Test dell'import Microsoft 365 (Graph mockato) e del reset del materiale.

Il client Graph è sostituito da un fake in-process (niente rete): il job
esegue davvero scan → download → ingest attraverso la pipeline reale su
DB/Qdrant temporanei, quindi si verifica anche l'integrazione end-to-end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import db as db_module
from app import m365_import
from app.config import get_settings
from app.ingestion import pipeline
from app.m365_import import GraphClient, ImportJob, _html_to_text
from app.retrieval import service as retrieval_service
from app.retrieval.rerank import Reranker


@asynccontextmanager
async def _lifespan(app):
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test con job M365 fresco e Graph fake (vedi _install_fake_graph)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    monkeypatch.setenv("KG_ENABLED", "false")
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()
    retrieval_service.reset_state()
    Reranker.reset()
    monkeypatch.setattr(m365_import, "_job", ImportJob())
    _install_fake_graph(monkeypatch)

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    db_module.reset_engine()
    pipeline.reset_state()
    retrieval_service.reset_state()
    Reranker.reset()
    get_settings.cache_clear()


def _install_fake_graph(monkeypatch) -> None:
    """Fake di un tenant minimo: 1 utente, 1 file OneDrive, 1 mail, 0 siti."""

    async def fake_acquire(self: GraphClient) -> str:
        self._token = "fake-token"
        self._token_exp = float("inf")
        return self._token

    async def fake_get_json(self: GraphClient, url: str, params=None) -> dict[str, Any]:
        if url.endswith("/me"):
            return {
                "id": "u1",
                "displayName": "Service Account",
                "userPrincipalName": "sa@contoso.it",
            }
        if url.endswith("/users/u1/drive"):
            return {"id": "drive1", "name": "OneDrive"}
        if "/drive/root:/" in url:
            # Risoluzione di un percorso cartella (scoping per cartella)
            return {
                "id": "dirP",
                "name": "VIRTUALIS",
                "folder": {"childCount": 1},
                "parentReference": {"driveId": "drive1"},
            }
        if url.endswith("/users/u1/messages/m1"):
            return {
                "subject": "Contratto rinnovo",
                "from": {"emailAddress": {"name": "Mario", "address": "mario@contoso.it"}},
                "toRecipients": [{"emailAddress": {"address": "sa@contoso.it"}}],
                "ccRecipients": [],
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "body": {
                    "contentType": "html",
                    "content": "<p>Gentile collega,</p><p>in allegato il contratto.</p>",
                },
                "hasAttachments": False,
            }
        raise AssertionError(f"GET inattesa: {url}")

    async def fake_get_all(
        self: GraphClient, url: str, params=None
    ) -> AsyncIterator[dict[str, Any]]:
        if "/drives/drive1/root/children" in url:
            yield {
                "id": "f1",
                "name": "nota.md",
                "size": 42,
                "lastModifiedDateTime": "2026-07-01T09:00:00Z",
                "file": {"mimeType": "text/markdown"},
                "parentReference": {"driveId": "drive1"},
            }
        elif "/drives/drive1/items/dirP/children" in url:
            # Contenuto della cartella risolta con lo scoping `folders`
            yield {
                "id": "fP",
                "name": "piano.md",
                "size": 12,
                "file": {"mimeType": "text/markdown"},
                "parentReference": {"driveId": "drive1"},
            }
        elif url.endswith("/users/u1/mailFolders"):
            yield {"id": "inbox", "displayName": "Posta in arrivo", "totalItemCount": 1}
        elif "/childFolders" in url:
            return
        elif "/mailFolders/inbox/messages" in url:
            yield {
                "id": "m1",
                "subject": "Contratto rinnovo",
                "from": {"emailAddress": {"address": "mario@contoso.it"}},
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "hasAttachments": False,
            }
        elif "/sites" in url:
            return
        else:
            raise AssertionError(f"GET all inattesa: {url}")

    async def fake_download(self: GraphClient, url: str, dest: Path, on_progress) -> int:
        data = b"# Nota OneDrive\nContenuto di test.\n"
        dest.write_bytes(data)
        on_progress(len(data))
        return len(data)

    monkeypatch.setattr(GraphClient, "_acquire_token", fake_acquire)
    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    monkeypatch.setattr(GraphClient, "download", fake_download)


async def _login(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wait_job(client: AsyncClient, token: str, timeout: float = 120.0) -> dict[str, Any]:
    """Poll dello stato del job fino a stato terminale."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/api/m365/status", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        status = resp.json()
        if status["state"] in {"done", "error", "cancelled"}:
            return status
        await asyncio.sleep(0.5)
    raise AssertionError("job non terminato entro il timeout")


# --- test --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_iniziale_idle(client: AsyncClient):
    token = await _login(client)
    resp = await client.get("/api/m365/status", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


@pytest.mark.asyncio
async def test_route_m365_solo_admin(client: AsyncClient):
    """Tutte le route /api/m365/* (status inclusa) richiedono il ruolo admin."""
    admin_token = await _login(client)
    resp = await client.post(
        "/api/admin/users",
        json={"username": "mario", "password": "segretissima", "role": "user"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/auth/login", json={"username": "mario", "password": "segretissima"}
    )
    token = resp.json()["access_token"]

    assert (
        await client.get("/api/m365/status", headers=_auth(token))
    ).status_code == 403
    assert (
        await client.post(
            "/api/m365/test", json={"mode": "password"}, headers=_auth(token)
        )
    ).status_code == 403
    assert (
        await client.post("/api/m365/cancel", headers=_auth(token))
    ).status_code == 403


@pytest.mark.asyncio
async def test_test_connection_credenziali_mancanti(client: AsyncClient):
    token = await _login(client)
    resp = await client.post(
        "/api/m365/test", json={"mode": "password"}, headers=_auth(token)
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_ok(client: AsyncClient):
    token = await _login(client)
    resp = await client.post(
        "/api/m365/test",
        json={"mode": "password", "username": "sa@contoso.it", "password": "x"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_dry_run_solo_scansione(client: AsyncClient):
    token = await _login(client)
    resp = await client.post(
        "/api/m365/start",
        json={
            "auth": {"mode": "password", "username": "sa@contoso.it", "password": "x"},
            "dry_run": True,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 202, resp.text
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")
    assert status["dry_run"] is True
    assert status["scan"]["onedrive_files"] == 1
    assert status["scan"]["mail_messages"] == 1
    assert status["scan"]["sharepoint_sites"] == 0
    assert status["download"]["total_files"] == 2
    assert status["download"]["done_files"] == 0  # anteprima: nessun download


@pytest.mark.asyncio
async def test_import_completo_indicizza(client: AsyncClient):
    """Import reale (Graph fake): file OneDrive + mail → 2 documenti indicizzati."""
    token = await _login(client)
    resp = await client.post(
        "/api/m365/start",
        json={"auth": {"mode": "password", "username": "sa@contoso.it", "password": "x"}},
        headers=_auth(token),
    )
    assert resp.status_code == 202, resp.text
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")
    assert status["download"]["done_files"] == 2
    assert status["download"]["failed_files"] == 0
    assert status["ingest"]["indexed"] == 2
    assert status["ingest"]["failed"] == 0

    # I documenti sono visibili via API con sorgente "Microsoft 365"
    resp = await client.get("/api/documents", headers=_auth(token))
    docs = resp.json()["documents"]
    assert len(docs) == 2
    resp = await client.get("/api/ingest/status", headers=_auth(token))
    sources = {s["name"]: s for s in resp.json()["per_source"]}
    assert "Microsoft 365" in sources
    assert sources["Microsoft 365"]["indexed"] == 2


@pytest.mark.asyncio
async def test_reset_materiale(client: AsyncClient):
    """Il reset elimina documenti, sorgenti e file; richiede confirm."""
    token = await _login(client)

    # Popola con un import
    await client.post(
        "/api/m365/start",
        json={"auth": {"mode": "password", "username": "sa@contoso.it", "password": "x"}},
        headers=_auth(token),
    )
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")

    # Senza confirm → 400
    resp = await client.post(
        "/api/admin/material/reset", json={}, headers=_auth(token)
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/api/admin/material/reset",
        json={"confirm": True},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"]["documents"] == 2

    resp = await client.get("/api/documents", headers=_auth(token))
    assert resp.json()["documents"] == []
    resp = await client.get("/api/admin/sources", headers=_auth(token))
    assert resp.json()["sources"] == []


def test_html_to_text():
    html = (
        "<html><body><p>Ciao <b>mondo</b></p><br>"
        "<p>Seconda riga &amp; co</p><script>x()</script></body></html>"
    )
    text = _html_to_text(html)
    assert "Ciao mondo" in text
    assert "Seconda riga & co" in text
    assert "<" not in text and "script" not in text


@pytest.mark.asyncio
async def test_scan_drive_dedup_duplicati_paginazione(monkeypatch, tmp_path):
    """Graph può restituire lo stesso figlio in più pagine: lo scanner deve
    enumerare ogni cartella/file una sola volta (niente effetto valanga)."""
    folder_node = {
        "id": "dir1",
        "name": "sottocartella",
        "folder": {},
        "parentReference": {"driveId": "d1"},
    }
    nm_node = {
        "id": "nm1",
        "name": "node_modules",
        "folder": {},
        "parentReference": {"driveId": "d1"},
    }
    file_node = {
        "id": "f1",
        "name": "doc.md",
        "size": 10,
        "file": {},
        "parentReference": {"driveId": "d1"},
    }

    async def fake_get_all(self, url, params=None):
        # La root restituisce DUE VOLTE la stessa cartella e lo stesso file
        # (duplicati di paginazione) + una node_modules da ignorare;
        # la sottocartella è vuota
        if "root/children" in url:
            yield folder_node
            yield file_node
            yield folder_node
            yield file_node
            yield nm_node
        elif "nm1" in url:
            raise AssertionError("node_modules non doveva essere enumerata")
        # children di dir1: niente

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_drive("https://x/drives/d1/root/children", "onedrive", tasks)
    assert len(tasks) == 1
    assert tasks[0].title == "doc.md"
    assert job._seen_folders == {"d1:dir1", "d1:nm1"}
    assert job._seen_files == {"d1:f1"}


@pytest.mark.asyncio
async def test_scan_drive_segue_remote_item(monkeypatch):
    """Cartelle e file CONDIVISI (remoteItem) sono seguiti sul drive del
    proprietario, mantenendo il prefisso percorso visto dal ricevente."""
    shared_folder = {
        "id": "local1",
        "name": "Presente - MR&MM",
        "parentReference": {"driveId": "dR"},
        "remoteItem": {
            "id": "rf1",
            "folder": {},
            "parentReference": {"driveId": "dO"},
        },
    }
    shared_file = {
        "id": "local2",
        "name": "condiviso.md",
        "parentReference": {"driveId": "dR"},
        "remoteItem": {
            "id": "rx9",
            "file": {},
            "size": 7,
            "parentReference": {"driveId": "dO"},
            "lastModifiedDateTime": "2026-07-01T09:00:00Z",
        },
    }
    inner_file = {
        "id": "f5",
        "name": "preventivo.md",
        "size": 11,
        "file": {},
        "parentReference": {"driveId": "dO"},
    }

    async def fake_get_all(self, url, params=None):
        if "drives/dR/root/children" in url:
            yield shared_folder
            yield shared_file
        elif "drives/dO/items/rf1/children" in url:
            yield inner_file
        else:
            raise AssertionError(f"URL inattesa: {url}")

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_drive("https://x/drives/dR/root/children", "onedrive", tasks)

    by_title = {t.title: t for t in tasks}
    assert set(by_title) == {"Presente - MR&MM/preventivo.md", "condiviso.md"}
    # Download ed external_id puntano al drive/item REMOTI (proprietario)
    assert "/drives/dO/items/f5/content" in by_title["Presente - MR&MM/preventivo.md"].url
    assert "/drives/dO/items/rx9/content" in by_title["condiviso.md"].url
    assert by_title["condiviso.md"].external_id == "m365://onedrive/rx9"
    assert by_title["condiviso.md"].size == 7
    # La dedup della cartella condivisa usa la chiave del drive remoto
    assert "dO:rf1" in job._seen_folders
    assert {"dO:f5", "dO:rx9"} <= job._seen_files


@pytest.mark.asyncio
async def test_scan_drive_dedup_proprietario_e_condiviso(monkeypatch):
    """Una cartella vista sia sul drive del proprietario sia come remoteItem
    del ricevente è enumerata (e scaricata) una volta sola."""
    owner_folder = {
        "id": "rf1",
        "name": "Presente - MR&MM",
        "folder": {},
        "parentReference": {"driveId": "dO"},
    }
    inner_file = {
        "id": "f5",
        "name": "preventivo.md",
        "size": 11,
        "file": {},
        "parentReference": {"driveId": "dO"},
    }
    receiver_stub = {
        "id": "local1",
        "name": "Presente - MR&MM",
        "parentReference": {"driveId": "dR"},
        "remoteItem": {
            "id": "rf1",
            "folder": {},
            "parentReference": {"driveId": "dO"},
        },
    }

    calls: list[str] = []

    async def fake_get_all(self, url, params=None):
        calls.append(url)
        if "drives/dO/root/children" in url:
            yield owner_folder
        elif "drives/dO/items/rf1/children" in url:
            yield inner_file
        elif "drives/dR/root/children" in url:
            yield receiver_stub

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    # Prima il drive del proprietario, poi quello del ricevente (stesso job)
    await job._scan_drive("https://x/drives/dO/root/children", "onedrive", tasks)
    await job._scan_drive("https://x/drives/dR/root/children", "onedrive", tasks)

    assert len(tasks) == 1
    assert tasks[0].title == "Presente - MR&MM/preventivo.md"
    # La cartella remota è stata percorsa UNA volta sola
    assert sum("items/rf1/children" in c for c in calls) == 1


@pytest.mark.asyncio
async def test_scan_drive_remote_drive_non_leggibile(monkeypatch):
    """403/404 sul drive remoto di una cartella condivisa: warning nel feed
    eventi, lo scan prosegue senza far fallire il job."""
    receiver_stub = {
        "id": "local1",
        "name": "Presente - MR&MM",
        "parentReference": {"driveId": "dR"},
        "remoteItem": {
            "id": "rf1",
            "folder": {},
            "parentReference": {"driveId": "dO"},
        },
    }

    async def fake_get_all(self, url, params=None):
        if "drives/dR/root/children" in url:
            yield receiver_stub
        elif "drives/dO/items/rf1/children" in url:
            raise RuntimeError("403 Forbidden: accesso negato al drive remoto")

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_drive("https://x/drives/dR/root/children", "onedrive", tasks)

    assert tasks == []
    warnings = [e for e in job._state.events if e["level"] == "warning"]
    assert warnings and "Presente - MR&MM" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_download_titolo_conserva_percorso_cartella(monkeypatch, tmp_settings):
    """Il titolo del documento di drive è ``task.title`` (percorso relativo),
    non il solo nome file; per la posta resta prioritario il titolo del
    parser (distingue corpo e allegati)."""
    from app.db import get_session_factory, init_db

    await init_db()
    job = ImportJob()
    job._session_factory = get_session_factory()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})

    async def fake_download(self, url, dest, on_progress):
        dest.write_bytes(b"# Doc\n\ncontenuto di prova.\n")
        return 24

    async def fake_get_json(self, url, params=None):
        return {
            "subject": "Oggetto importante",
            "from": {"emailAddress": {"name": "Mario", "address": "mario@contoso.it"}},
            "toRecipients": [],
            "ccRecipients": [],
            "receivedDateTime": "2026-07-01T10:00:00Z",
            "body": {"contentType": "text", "content": "corpo del messaggio"},
            "hasAttachments": False,
        }

    monkeypatch.setattr(GraphClient, "download", fake_download)
    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)

    ingested: list[dict[str, Any]] = []

    async def spy_ingest(path, source_id, external_id, session_factory, *, doc_meta=None):
        ingested.append(dict(doc_meta or {}))
        return 1

    monkeypatch.setattr(pipeline, "ingest_file", spy_ingest)

    drive_task = m365_import._FileTask(
        category="onedrive",
        title="Presente - MR&MM/Preventivi/doc.md",
        url="https://graph/x/content",
        size=10,
        external_id="m365://onedrive/f1",
    )
    mail_task = m365_import._FileTask(
        category="mail",
        title="Posta in arrivo/Oggetto importante",
        url="https://graph/x/messages/m1",
        size=0,
        external_id="m365://mail/m1",
    )
    await job._download_all([drive_task, mail_task])

    assert ingested[0]["title"] == "Presente - MR&MM/Preventivi/doc.md"
    assert ingested[1]["title"] == "Email: Oggetto importante"


@pytest.mark.asyncio
async def test_scan_drive_search_per_estensione(monkeypatch):
    """Modalità search: chiede a Graph solo le estensioni supportate,
    filtra i match laschi e ricostruisce il path dal parentReference."""
    calls: list[str] = []

    async def fake_get_all(self, url, params=None):
        calls.append(url)
        if "search(q='.md')" in url:
            yield {
                "id": "f1",
                "name": "doc.md",
                "size": 10,
                "file": {},
                "parentReference": {
                    "driveId": "d1",
                    "path": "/drives/d1/root:/Clienti/ACME",
                },
            }
            yield {  # match lasco: nome senza estensione giusta → scartato
                "id": "f2",
                "name": "note.md.bak",
                "size": 5,
                "file": {},
                "parentReference": {"driveId": "d1", "path": "/drives/d1/root:"},
            }
            yield {  # duplicato di paginazione → scartato
                "id": "f1",
                "name": "doc.md",
                "size": 10,
                "file": {},
                "parentReference": {
                    "driveId": "d1",
                    "path": "/drives/d1/root:/Clienti/ACME",
                },
            }
        # le altre estensioni non hanno risultati

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_drive_search("d1", "onedrive", tasks)
    # una chiamata di ricerca per ogni estensione supportata
    assert sum("search(q=" in c for c in calls) == len(m365_import._SUPPORTED)
    assert len(tasks) == 1
    assert tasks[0].title == "Clienti/ACME/doc.md"
    assert tasks[0].external_id == "m365://onedrive/f1"


@pytest.mark.asyncio
async def test_scan_folders_search_scoping(monkeypatch):
    """folders + scan_mode=search: la search interroga tutto il drive ma
    tiene solo i file sotto la cartella richiesta (nessuna BFS sui children)."""
    calls: list[str] = []

    async def fake_get_json(self, url, params=None):
        if url.endswith("/users/marco@contoso.it/drive/root:/Presente:"):
            return {
                "id": "folder1",
                "name": "Presente",
                "folder": {},
                "parentReference": {"driveId": "d1"},
            }
        raise AssertionError(f"GET inattesa: {url}")

    async def fake_get_all(self, url, params=None):
        calls.append(url)
        assert "/children" not in url, "con scan_mode=search non si fa BFS"
        if "search(q='.md')" in url:
            yield {
                "id": "f1",
                "name": "dentro.md",
                "size": 10,
                "file": {},
                "parentReference": {
                    "driveId": "d1",
                    "path": "/drives/d1/root:/Presente/CLIENTI",
                },
            }
            yield {  # direttamente nella radice della cartella: tenuto
                "id": "f2",
                "name": "radice.md",
                "size": 10,
                "file": {},
                "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Presente"},
            }
            yield {  # fuori cartella: scartato
                "id": "f3",
                "name": "fuori.md",
                "size": 10,
                "file": {},
                "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Altra"},
            }

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_folders(
        [("marco@contoso.it", "Presente")], tasks, scan_mode="search"
    )
    assert sum("search(q=" in c for c in calls) == len(m365_import._SUPPORTED)
    assert [t.title for t in tasks] == [
        "Presente/CLIENTI/dentro.md",
        "Presente/radice.md",
    ]
    assert job._state.scan["onedrive_files"] == 2


@pytest.mark.asyncio
async def test_scan_folders_delta_scoping(monkeypatch):
    """folders + scan_mode=delta: enumera il sottoalbero via endpoint delta,
    tiene solo i file supportati sotto la cartella (niente BFS né search)."""
    calls: list[str] = []

    nodes = [
        {  # file supportato in sottocartella: tenuto
            "id": "f1",
            "name": "doc.pdf",
            "size": 10,
            "file": {},
            "parentReference": {
                "driveId": "d1",
                "path": "/drives/d1/root:/Presente/CLIENTI",
            },
        },
        {  # estensione non supportata: scartato
            "id": "f2",
            "name": "foto.jpg",
            "size": 10,
            "file": {},
            "parentReference": {
                "driveId": "d1",
                "path": "/drives/d1/root:/Presente/Foto",
            },
        },
        {  # eliminato: scartato
            "id": "f3",
            "name": "vecchio.pdf",
            "size": 10,
            "file": {},
            "deleted": {},
            "parentReference": {
                "driveId": "d1",
                "path": "/drives/d1/root:/Presente",
            },
        },
        {  # cartella: scartata
            "id": "f4",
            "name": "Sottocartella",
            "folder": {},
            "parentReference": {
                "driveId": "d1",
                "path": "/drives/d1/root:/Presente",
            },
        },
        {  # fuori prefisso (incoerenza simulata): scartato
            "id": "f5",
            "name": "fuori.md",
            "size": 10,
            "file": {},
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Altra"},
        },
    ]

    async def fake_get_json(self, url, params=None):
        if url.endswith("/users/marco@contoso.it/drive/root:/Presente:"):
            return {
                "id": "folder1",
                "name": "Presente",
                "folder": {},
                "parentReference": {"driveId": "d1"},
            }
        if "/delta" in url:
            calls.append(url)
            assert "/children" not in url and "search(q=" not in url
            return {"value": nodes}
        raise AssertionError(f"GET inattesa: {url}")

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_folders(
        [("marco@contoso.it", "Presente")], tasks, scan_mode="delta"
    )
    assert [t.title for t in tasks] == ["Presente/CLIENTI/doc.pdf"]
    assert job._state.scan["onedrive_files"] == 1


@pytest.mark.asyncio
async def test_scan_folders_delta_ciclo_infinito_interrotto(monkeypatch):
    """4/09/26: su alcuni drive il delta non emette mai deltaLink e ripropone
    all'infinito gli stessi nodi con lo stesso nextLink. Lo scan deve
    interrompersi dopo due pagine consecutive senza nodi nuovi, tenendo i
    file unici trovati."""
    pages = 0
    nodes = [
        {
            "id": "f1",
            "name": "doc.pdf",
            "size": 10,
            "file": {},
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Presente"},
        },
        {
            "id": "f2",
            "name": "altro.xlsx",
            "size": 10,
            "file": {},
            "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Presente"},
        },
    ]

    async def fake_get_json(self, url, params=None):
        nonlocal pages
        if url.endswith("/users/marco@contoso.it/drive/root:/Presente:"):
            return {
                "id": "folder1",
                "name": "Presente",
                "folder": {},
                "parentReference": {"driveId": "d1"},
            }
        if "/delta" in url:
            pages += 1
            # Stessi nodi e stesso nextLink a ogni pagina: ciclo infinito
            return {"value": nodes, "@odata.nextLink": "https://graph/delta?token=abc"}
        raise AssertionError(f"GET inattesa: {url}")

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    job = ImportJob()
    job._client = GraphClient({"mode": "password", "username": "u", "password": "p"})
    tasks: list = []
    await job._scan_folders(
        [("marco@contoso.it", "Presente")], tasks, scan_mode="delta"
    )
    assert [t.title for t in tasks] == [
        "Presente/doc.pdf",
        "Presente/altro.xlsx",
    ]
    assert pages == 3  # pagina buona + due pagine "stale" prima del break
    assert any("Delta in ciclo" in e["message"] for e in job._state.events)


@pytest.mark.asyncio
async def test_requeue_dopo_riavvio(client: AsyncClient):
    """Simula un riavvio a metà ingest: documenti pending con external_id
    non-path (m365://) vengono riaccodati ritrovando il file per content_hash."""
    token = await _login(client)

    # Import completo → 2 documenti indicizzati
    await client.post(
        "/api/m365/start",
        json={"auth": {"mode": "device_code"}},
        headers=_auth(token),
    )
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")

    # Simula il riavvio: riporta i documenti a pending (la coda è persa)
    from app.db import get_session_factory
    from app.models import Document

    async with get_session_factory()() as db:
        for doc in (await db.execute(select(Document))).scalars().all():
            doc.status = "pending"
        await db.commit()

    resp = await client.post("/api/ingest/requeue", headers=_auth(token))
    assert resp.status_code == 202, resp.text
    assert resp.json()["requeued"] == 2
    assert resp.json()["missing"] == 0

    # La pipeline li riprocessa fino a indexed
    deadline = asyncio.get_event_loop().time() + 120
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/api/documents", headers=_auth(token))
        docs = resp.json()["documents"]
        if docs and all(d["status"] == "indexed" for d in docs):
            break
        await asyncio.sleep(0.5)
    assert all(d["status"] == "indexed" for d in docs)


# --- scoping per cartella e tetto dimensione ----------------------------------


def test_parse_folder_spec():
    """'upn:/percorso' → (upn, percorso); formati malformati → ValueError."""
    assert m365_import.parse_folder_spec(
        "marco@contoso.it:/Presente/VIRTUALIS"
    ) == ("marco@contoso.it", "Presente/VIRTUALIS")
    # Slash iniziali/finali del percorso tollerati e normalizzati
    assert m365_import.parse_folder_spec("mario@contoso.it://Presente/") == (
        "mario@contoso.it",
        "Presente",
    )
    for bad in ["senza-separatore", ":/solo-percorso", "solo-upn:/", "mario@x:"]:
        with pytest.raises(ValueError, match="upn:/percorso"):
            m365_import.parse_folder_spec(bad)


@pytest.mark.asyncio
async def test_scan_folders_parte_dalla_cartella(monkeypatch):
    """Con ``folders`` lo scan OneDrive risolve il percorso richiesto e la BFS
    parte dai suoi children: la root dei drive e le utenze (/me, /users) non
    vengono toccate; i titoli conservano il prefisso della cartella scelta."""
    get_all_urls: list[str] = []

    async def fake_get_json(self, url, params=None):
        if url.endswith("/users/marco@contoso.it/drive/root:/Presente/VIRTUALIS:"):
            return {
                "id": "dirV",
                "name": "VIRTUALIS",
                "folder": {"childCount": 2},
                "parentReference": {"driveId": "dM"},
            }
        raise AssertionError(f"GET inattesa: {url}")

    async def fake_get_all(self, url, params=None):
        get_all_urls.append(url)
        if "drives/dM/items/dirV/children" in url:
            yield {
                "id": "sub1",
                "name": "Preventivi",
                "folder": {},
                "parentReference": {"driveId": "dM"},
            }
            yield {
                "id": "f1",
                "name": "doc.pdf",
                "size": 10,
                "file": {},
                "parentReference": {"driveId": "dM"},
            }
        elif "drives/dM/items/sub1/children" in url:
            yield {
                "id": "f2",
                "name": "offerta.md",
                "size": 5,
                "file": {},
                "parentReference": {"driveId": "dM"},
            }
        else:
            raise AssertionError(f"GET all inattesa: {url}")

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "client_credentials"})
    tasks = await job._scan(
        {
            "include_onedrive": True,
            "include_sharepoint": False,
            "include_mail": False,
            "folders": ["marco@contoso.it:/Presente/VIRTUALIS"],
            "max_file_mb": 10,
        }
    )
    # Nessuna enumerazione della root dei drive
    assert all("root/children" not in u for u in get_all_urls)
    # Titoli prefissati dal percorso della cartella scelta
    assert {t.title for t in tasks} == {
        "Presente/VIRTUALIS/doc.pdf",
        "Presente/VIRTUALIS/Preventivi/offerta.md",
    }
    assert job._state.scan["onedrive_files"] == 2
    # L'opzione max_file_mb è stata applicata al job
    assert job._max_file_bytes == 10 * (1 << 20)


@pytest.mark.asyncio
async def test_scan_folders_url_encoding(monkeypatch):
    """Percorsi con '&' e spazi vengono URL-encodati nella risoluzione."""
    resolved: list[str] = []

    async def fake_get_json(self, url, params=None):
        resolved.append(url)
        return {
            "id": "dirB",
            "name": "Anno 2026",
            "folder": {},
            "parentReference": {"driveId": "dD"},
        }

    async def fake_get_all(self, url, params=None):
        # Cartella vuota: nessun child
        return
        yield  # pragma: no cover

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "client_credentials"})
    tasks: list = []
    await job._scan_folders(
        [("daniele@contoso.it", "Bandi&Normative/Anno 2026")], tasks
    )
    assert resolved == [
        "https://graph.microsoft.com/v1.0/users/daniele@contoso.it"
        "/drive/root:/Bandi%26Normative/Anno%202026:"
    ]
    assert tasks == []


@pytest.mark.asyncio
async def test_scan_folders_percorso_non_risolvibile(monkeypatch):
    """404 sulla risoluzione del percorso: warning nel feed, il job prosegue."""

    async def fake_get_json(self, url, params=None):
        raise m365_import.M365AuthError("Graph API HTTP 404: itemNotFound")

    monkeypatch.setattr(GraphClient, "get_json", fake_get_json)
    job = ImportJob()
    job._client = GraphClient({"mode": "client_credentials"})
    tasks: list = []
    await job._scan_folders([("marco@contoso.it", "Inesistente")], tasks)
    assert tasks == []
    warnings = [e for e in job._state.events if e["level"] == "warning"]
    assert warnings and "Inesistente" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_max_file_mb_salta_file_grandi(monkeypatch):
    """I file oltre il tetto vengono saltati e contati in skipped_files, con
    un evento aggregato nel feed (non uno per file)."""

    async def fake_get_all(self, url, params=None):
        yield {
            "id": "f1",
            "name": "piccolo.md",
            "size": 1024,
            "file": {},
            "parentReference": {"driveId": "d1"},
        }
        yield {
            "id": "f2",
            "name": "enorme.pdf",
            "size": 200 * (1 << 20),
            "file": {},
            "parentReference": {"driveId": "d1"},
        }

    monkeypatch.setattr(GraphClient, "get_all", fake_get_all)
    job = ImportJob()
    job._client = GraphClient({"mode": "client_credentials"})
    job._max_file_bytes = 50 * (1 << 20)
    tasks: list = []
    await job._scan_drive("https://x/drives/d1/root/children", "onedrive", tasks)
    assert [t.title for t in tasks] == ["piccolo.md"]
    assert job._state.skipped_files == 1
    messages = [e["message"] for e in job._state.events]
    assert any("tetto" in m and "enorme.pdf" in m for m in messages)


@pytest.mark.asyncio
async def test_dry_run_con_folders(client: AsyncClient):
    """End-to-end (Graph fake): scan limitato a una cartella, la root del
    drive non viene enumerata e il conteggio riflette solo la cartella."""
    token = await _login(client)
    resp = await client.post(
        "/api/m365/start",
        json={
            "auth": {"mode": "password", "username": "sa@contoso.it", "password": "x"},
            "folders": ["sa@contoso.it:/Presente/VIRTUALIS"],
            "include_mail": False,
            "include_sharepoint": False,
            "dry_run": True,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 202, resp.text
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")
    assert status["scan"]["onedrive_files"] == 1  # solo piano.md nella cartella
    assert status["scan"]["mail_messages"] == 0
    assert status["download"]["total_files"] == 1


@pytest.mark.asyncio
async def test_start_valida_folders_e_max_file_mb(client: AsyncClient):
    """Input malformati rifiutati all'avvio: folders senza 'upn:/percorso'
    e max_file_mb fuori range → 422 con messaggio chiaro."""
    token = await _login(client)
    base = {"auth": {"mode": "password", "username": "sa@contoso.it", "password": "x"}}
    resp = await client.post(
        "/api/m365/start",
        json={**base, "folders": ["senza-separatore"]},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text
    assert "upn:/percorso" in resp.text
    for mb in (0, 501):
        resp = await client.post(
            "/api/m365/start",
            json={**base, "max_file_mb": mb},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text


# --- device code flow --------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    """httpx.AsyncClient fake: risponde al token endpoint in sequenza."""

    def __init__(self, payloads: list[dict[str, Any]]):
        self._payloads = list(payloads)
        self.calls = 0

    async def post(self, url: str, data: dict[str, Any]) -> _FakeResponse:
        self.calls += 1
        assert "device_code" in data.get("grant_type", "") or data.get(
            "grant_type"
        ) == "refresh_token"
        return _FakeResponse(self._payloads.pop(0))


def _make_flow(monkeypatch, **kwargs) -> m365_import._DeviceFlow:
    flow = m365_import._DeviceFlow(
        tenant=kwargs.get("tenant", "contoso"),
        client_id="cid",
        device_code="dc",
        user_code="ABCD12345",
        verification_uri="https://login.microsoft.com/device",
        interval=kwargs.get("interval", 5),
        expires_at=kwargs.get("expires_at", 9e18),
        refresh_token=kwargs.get("refresh_token"),
    )
    monkeypatch.setattr(m365_import, "_device_flow", flow)
    return flow


@pytest.mark.asyncio
async def test_device_token_polling_fino_ad_approvazione(monkeypatch):
    """Polling: pending, pending, poi token + refresh token salvato nel flusso."""
    flow = _make_flow(monkeypatch)
    client = GraphClient({"mode": "device_code"})
    client._client = _FakeHttp(
        [
            {"error": "authorization_pending"},
            {"error": "authorization_pending"},
            {"access_token": "tok", "expires_in": 3600, "refresh_token": "rt1"},
        ]
    )

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(m365_import.asyncio, "sleep", _no_sleep)
    token = await client._acquire_token()
    assert token == "tok"
    assert flow.refresh_token == "rt1"
    assert client._client.calls == 3


@pytest.mark.asyncio
async def test_device_token_via_refresh(monkeypatch):
    """Con refresh token presente: rinnovo diretto senza polling."""
    flow = _make_flow(monkeypatch, refresh_token="rt1")
    client = GraphClient({"mode": "device_code"})
    client._client = _FakeHttp(
        [{"access_token": "tok2", "expires_in": 3600, "refresh_token": "rt2"}]
    )
    token = await client._acquire_token()
    assert token == "tok2"
    assert flow.refresh_token == "rt2"  # rotazione del refresh token


@pytest.mark.asyncio
async def test_device_token_senza_flusso():
    client = GraphClient({"mode": "device_code"})
    with pytest.raises(m365_import.M365AuthError, match="generare prima il codice"):
        await client._acquire_token()


@pytest.mark.asyncio
async def test_device_begin_endpoint(client: AsyncClient, monkeypatch):
    """L'endpoint begin ritorna codice e URL di verifica (Entra mockato)."""
    from app.api import routes_m365

    async def fake_begin(tenant=None, client_id=None):
        return {
            "active": True,
            "user_code": "ABCD12345",
            "verification_uri": "https://login.microsoft.com/device",
            "authorized": False,
            "expires_in_seconds": 900,
        }

    monkeypatch.setattr(routes_m365, "begin_device_flow", fake_begin)
    token = await _login(client)
    resp = await client.post(
        "/api/m365/device/begin",
        json={"tenant": "contoso.onmicrosoft.com"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_code"] == "ABCD12345"


@pytest.mark.asyncio
async def test_import_con_device_code(client: AsyncClient):
    """Start con mode device_code: validazione ok, import completo (Graph fake)."""
    token = await _login(client)
    resp = await client.post(
        "/api/m365/start",
        json={"auth": {"mode": "device_code"}},
        headers=_auth(token),
    )
    assert resp.status_code == 202, resp.text
    status = await _wait_job(client, token)
    assert status["state"] == "done", status.get("error")
    assert status["ingest"]["indexed"] == 2


# --- hard cap sulle richieste appese (trickle-hang Graph) --------------------


class _GetResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_get_json_riprova_su_richiesta_appesa(monkeypatch):
    """Una risposta che non arriva mai (trickle-hang Graph) viene abortita
    dopo il cap lato asyncio e ritentata su connessione fresca: alla seconda
    prova la pagina arriva e get_json ritorna."""
    import time

    client = GraphClient(
        {
            "mode": "client_credentials",
            "tenant_id": "contoso",
            "client_id": "cid",
            "client_secret": "sec",
        }
    )
    client._token = "tok"
    client._token_exp = time.monotonic() + 3600

    calls = 0

    class _HangingHttp:
        async def get(self, url, headers=None, params=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()  # risposta che non arriva mai
            return _GetResponse(200, {"value": [1, 2, 3]})

    client._client = _HangingHttp()
    monkeypatch.setattr(m365_import, "_REQUEST_HARD_CAP_S", 0.05)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(m365_import.asyncio, "sleep", _no_sleep)
    result = await client.get_json("https://graph.microsoft.com/v1.0/x")
    assert result == {"value": [1, 2, 3]}
    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_cap_superato_su_tutti_i_tentativi(monkeypatch):
    """Se Graph appende tutte le risposte, dopo 8 tentativi get_json molla
    con M365AuthError (il job fallisce visibilmente invece di restare muto)."""
    import time

    client = GraphClient(
        {
            "mode": "client_credentials",
            "tenant_id": "contoso",
            "client_id": "cid",
            "client_secret": "sec",
        }
    )
    client._token = "tok"
    client._token_exp = time.monotonic() + 3600

    class _AlwaysHangingHttp:
        async def get(self, url, headers=None, params=None):
            await asyncio.Event().wait()

    client._client = _AlwaysHangingHttp()
    monkeypatch.setattr(m365_import, "_REQUEST_HARD_CAP_S", 0.02)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(m365_import.asyncio, "sleep", _no_sleep)
    with pytest.raises(m365_import.M365AuthError, match="troppi retry"):
        await client.get_json("https://graph.microsoft.com/v1.0/x")
