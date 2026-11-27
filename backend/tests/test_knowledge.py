"""Test del knowledge graph documentale (GraphRAG).

Il KimiClient è SEMPRE mockato (``_FakeLLM``): nessuna chiamata di rete.
I test di pipeline e API usano worker e app reali con DB/Qdrant embedded
temporanei (stesso setup di test_pipeline/test_api).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db as db_module
from app.agent import tools as agent_tools
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.ingestion import pipeline
from app.knowledge import extractor
from app.knowledge import query as kg_query
from app.llm import LLMNotConfiguredError
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    Entity,
    EntityMention,
    Relation,
    Source,
)


class _FakeLLM:
    """KimiClient finto: risposte in coda (o un'eccezione) e conteggio chiamate."""

    def __init__(self, responses: list[str] | Exception) -> None:
        self._error = responses if isinstance(responses, Exception) else None
        self._responses: deque[str] = (
            deque() if isinstance(responses, Exception) else deque(responses)
        )
        self.calls = 0

    async def chat(self, messages: Any, *, json_mode: bool = False, **kwargs: Any) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if len(self._responses) > 1:
            return self._responses.popleft()
        return self._responses[0]


_DOC1_JSON = json.dumps(
    {
        "entities": [
            {"name": "Mario Rossi", "type": "persona", "description": "Legale rappresentante"},
            {"name": "  ACME   S.p.A. ", "type": "organizzazione", "description": "Fornitore"},
            {"name": "Contratto 2024/15", "type": "contratto", "description": "Fornitura"},
            {"name": "Mister X", "type": "alieno", "description": "Tipo fuori vocabolario"},
        ],
        "relations": [
            {"subject": "Mario Rossi", "predicate": "ha firmato", "object": "Contratto 2024/15"},
            {"subject": "Mario Rossi", "predicate": "lavora per", "object": "ACME S.p.A."},
            {"subject": "Sconosciuto", "predicate": "vede", "object": "Mario Rossi"},
        ],
    }
)

_DOC2_JSON = json.dumps(
    {
        "entities": [
            {"name": "mario rossi", "type": "persona", "description": "Coordinatore"},
            {"name": "Progetto Alfa", "type": "progetto", "description": "Progetto interno"},
        ],
        "relations": [
            {"subject": "Mario Rossi", "predicate": "coordina", "object": "Progetto Alfa"},
        ],
    }
)


@pytest.fixture
async def session_factory(tmp_settings):
    """Session factory su DB temporaneo con tabelle create."""
    await init_db()
    yield get_session_factory()


async def _make_document(session_factory, title: str, text: str) -> int:
    """Documento indexed con un chunk, pronto per l'estrazione."""
    async with session_factory() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.flush()
        document = Document(
            source_id=source.id,
            external_id=f"/tmp/{title}.md",
            title=title,
            doc_type="md",
            status=DocumentStatus.indexed.value,
        )
        db.add(document)
        await db.flush()
        db.add(Chunk(document_id=document.id, ord=0, text=text, context_text=text))
        await db.commit()
        return document.id


async def _count(session_factory, model: Any) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(model)) or 0


async def _extract(session_factory, document_id: int, llm: Any) -> dict[str, Any]:
    async with session_factory() as db:
        return await extractor.extract_for_document(document_id, session=db, llm=llm)


async def _build_graph(session_factory) -> tuple[int, int]:
    """Grafo di esempio su due documenti che condividono "Mario Rossi"."""
    doc1 = await _make_document(session_factory, "Contratto fornitura", "Testo contratto.")
    doc2 = await _make_document(session_factory, "Piano progetti", "Testo piano.")
    report1 = await _extract(session_factory, doc1, _FakeLLM([_DOC1_JSON]))
    report2 = await _extract(session_factory, doc2, _FakeLLM([_DOC2_JSON]))
    assert report1["error"] is None and report2["error"] is None
    return doc1, doc2


# --- estrattore ---------------------------------------------------------------


async def test_extract_persiste_entita_relazioni_e_mention(session_factory) -> None:
    """JSON valido: entità (normalizzate, dedup, tipo fuori vocabolario → altro),
    mention e relation persistite; relazioni con entità non estratte scartate."""
    doc_id = await _make_document(session_factory, "Contratto fornitura", "Testo.")
    report = await _extract(session_factory, doc_id, _FakeLLM([_DOC1_JSON]))

    assert report["error"] is None
    assert report["n_entities"] == 4
    assert report["n_mentions"] == 4
    assert report["n_relations"] == 2  # la terza ha un soggetto non estratto

    async with session_factory() as db:
        entities = (await db.execute(select(Entity).order_by(Entity.name))).scalars().all()
        by_name = {e.name: e for e in entities}
        assert "acme s.p.a." in by_name  # normalizzato: lowercase, spazi compattati
        assert by_name["acme s.p.a."].display_name == "ACME   S.p.A.".strip()
        assert by_name["mario rossi"].type == "persona"
        assert by_name["mister x"].type == "altro"  # tipo fuori vocabolario
        mentions = (await db.execute(select(EntityMention))).scalars().all()
        assert len(mentions) == 4
        assert all(m.document_id == doc_id for m in mentions)
        relations = (await db.execute(select(Relation))).scalars().all()
        predicates = {r.predicate for r in relations}
        assert predicates == {"ha_firmato", "lavora_per"}  # snake_case


async def test_extract_dedup_entita_tra_documenti(session_factory) -> None:
    """Stessa entità in due documenti: una riga Entity, due mention."""
    doc1 = await _make_document(session_factory, "Doc A", "Testo A.")
    doc2 = await _make_document(session_factory, "Doc B", "Testo B.")
    await _extract(session_factory, doc1, _FakeLLM([_DOC1_JSON]))
    await _extract(session_factory, doc2, _FakeLLM([_DOC1_JSON]))

    assert await _count(session_factory, Entity) == 4
    assert await _count(session_factory, EntityMention) == 8
    # Le relation sono per documento: stessa tripla, due provenienze diverse
    assert await _count(session_factory, Relation) == 4


async def test_extract_retry_su_json_imperfetto(session_factory) -> None:
    """Prima risposta non JSON: singolo retry di correzione, poi persistenza."""
    doc_id = await _make_document(session_factory, "Doc", "Testo.")
    llm = _FakeLLM(["questo non è JSON", _DOC1_JSON])
    report = await _extract(session_factory, doc_id, llm)

    assert llm.calls == 2
    assert report["error"] is None
    assert report["n_entities"] == 4


async def test_extract_json_irrecuperabile_report_errore(session_factory) -> None:
    """JSON invalido anche dopo il retry: report con errore, nessuna eccezione."""
    doc_id = await _make_document(session_factory, "Doc", "Testo.")
    llm = _FakeLLM(["no", "ancora no"])
    report = await _extract(session_factory, doc_id, llm)

    assert llm.calls == 2
    assert report["error"]
    assert report["n_entities"] == 0
    assert await _count(session_factory, Entity) == 0


async def test_extract_llm_non_configurato_nessuna_eccezione(session_factory) -> None:
    """LLMNotConfiguredError → report con errore, MAI eccezione verso la pipeline."""
    doc_id = await _make_document(session_factory, "Doc", "Testo.")
    report = await _extract(
        session_factory, doc_id, _FakeLLM(LLMNotConfiguredError())
    )
    assert report["error"]
    assert "MOONSHOT_API_KEY" in report["error"]
    assert await _count(session_factory, Entity) == 0


async def test_reextract_idempotente(session_factory) -> None:
    """Seconda estrazione sullo stesso documento: niente duplicati."""
    doc_id = await _make_document(session_factory, "Doc", "Testo.")
    first = await _extract(session_factory, doc_id, _FakeLLM([_DOC1_JSON]))
    second = await _extract(session_factory, doc_id, _FakeLLM([_DOC1_JSON]))

    assert first["error"] is None and second["error"] is None
    assert await _count(session_factory, Entity) == 4
    assert await _count(session_factory, EntityMention) == 4  # non 8
    assert await _count(session_factory, Relation) == 2  # non 4


# --- interrogazione del grafo ---------------------------------------------------


async def test_search_entities_match_e_conteggio_documenti(session_factory) -> None:
    """LIKE case-insensitive su name/display_name con conteggio documenti."""
    await _build_graph(session_factory)
    async with session_factory() as db:
        results = await kg_query.search_entities("ROSSI", session=db)
    assert len(results) == 1
    assert results[0]["display_name"] == "Mario Rossi"
    assert results[0]["document_count"] == 2
    async with session_factory() as db:
        acme = await kg_query.search_entities("acme", session=db)
    assert len(acme) == 1
    assert acme[0]["document_count"] == 1


async def test_entity_context_documenti_e_relazioni(session_factory) -> None:
    """Contesto: documenti che menzionano l'entità e relazioni con controparti."""
    await _build_graph(session_factory)
    async with session_factory() as db:
        entity = await db.scalar(select(Entity).where(Entity.name == "mario rossi"))
        assert entity is not None
        context = await kg_query.entity_context(entity.id, session=db)
    assert context is not None
    assert len(context["documents"]) == 2
    titles = {d["title"] for d in context["documents"]}
    assert titles == {"Contratto fornitura", "Piano progetti"}
    predicates = {r["predicate"] for r in context["relations"]}
    assert predicates == {"ha_firmato", "lavora_per", "coordina"}
    firmato = next(r for r in context["relations"] if r["predicate"] == "ha_firmato")
    assert firmato["object"]["display_name"] == "Contratto 2024/15"
    assert firmato["document_title"] == "Contratto fornitura"

    async with session_factory() as db:
        assert await kg_query.entity_context(99999, session=db) is None


async def test_documents_for_entities_any_e_all(session_factory) -> None:
    """Recupero multi-documento: ANY (almeno una) e ALL (tutte le entità)."""
    doc1, doc2 = await _build_graph(session_factory)
    async with session_factory() as db:
        any_docs = await kg_query.documents_for_entities(
            ["Mario Rossi", "Progetto Alfa"], session=db, mode="any"
        )
        all_docs = await kg_query.documents_for_entities(
            ["Mario Rossi", "Progetto Alfa"], session=db, mode="all"
        )
    assert {d["document_id"] for d in any_docs} == {doc1, doc2}
    assert [d["document_id"] for d in all_docs] == [doc2]
    assert all_docs[0]["matched_count"] == 2


async def test_entity_subgraph_espansione_per_profondita(session_factory) -> None:
    """Sottografo: depth=1 solo relazioni dirette, depth=2 anche indirette."""
    await _build_graph(session_factory)
    async with session_factory() as db:
        depth1 = await kg_query.entity_subgraph(["Contratto 2024/15"], session=db, depth=1)
        depth2 = await kg_query.entity_subgraph(["Contratto 2024/15"], session=db, depth=2)
    names1 = {e["name"] for e in depth1["entities"]}
    assert names1 == {"contratto 2024/15", "mario rossi"}
    assert len(depth1["relations"]) == 1
    names2 = {e["name"] for e in depth2["entities"]}
    assert {"contratto 2024/15", "mario rossi", "acme s.p.a.", "progetto alfa"} <= names2
    rel = depth1["relations"][0]
    assert rel["subject"] == "Mario Rossi"
    assert rel["object"] == "Contratto 2024/15"
    assert rel["document_title"] == "Contratto fornitura"


async def test_delete_document_graph_rimuove_e_pulisce_orfane(session_factory) -> None:
    """Cancellazione documento: mention/relation rimosse; entità orfane pulite,
    entità condivise con altri documenti conservate."""
    doc1, _doc2 = await _build_graph(session_factory)
    async with session_factory() as db:
        await extractor.delete_document_graph(doc1, session=db)
        await db.commit()

    async with session_factory() as db:
        mentions_doc1 = await db.scalar(
            select(func.count())
            .select_from(EntityMention)
            .where(EntityMention.document_id == doc1)
        )
        relations_doc1 = await db.scalar(
            select(func.count())
            .select_from(Relation)
            .where(Relation.document_id == doc1)
        )
        names = set((await db.execute(select(Entity.name))).scalars().all())
    assert mentions_doc1 == 0
    assert relations_doc1 == 0
    # Orfane sparite; condivisa (doc2) e legata a doc2 conservate
    assert "acme s.p.a." not in names
    assert "contratto 2024/15" not in names
    assert "mario rossi" in names
    assert "progetto alfa" in names


# --- pipeline -------------------------------------------------------------------


@pytest.fixture
async def pipeline_factory(tmp_settings):
    """Worker di ingestion reale su DB/Qdrant temporanei (come test_pipeline)."""
    await init_db()
    sf = get_session_factory()
    await pipeline.start_worker(sf)
    yield sf
    await pipeline.stop_worker()


@pytest.fixture
def kg_on(tmp_settings, monkeypatch):
    """Attiva il knowledge graph nelle settings (cache azzerata)."""
    monkeypatch.setenv("KG_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_pipeline_invoca_estrazione_con_kg_abilitato(
    pipeline_factory, kg_on, tmp_path, monkeypatch
) -> None:
    """Con kg_enabled=True l'ingest completo invoca l'estrazione (mockata)."""
    calls: list[int] = []

    async def fake_extract(document_id: int, *, session: Any, llm: Any) -> dict[str, Any]:
        calls.append(document_id)
        return {
            "document_id": document_id,
            "n_entities": 0,
            "n_mentions": 0,
            "n_relations": 0,
            "error": None,
        }

    monkeypatch.setattr(pipeline, "extract_for_document", fake_extract)
    monkeypatch.setattr(pipeline, "get_kimi_client", lambda: object())

    md = tmp_path / "kg.md"
    md.write_text("# Contratto\n\nMario Rossi firma per Acme S.p.A.\n", encoding="utf-8")
    async with pipeline_factory() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        source_id = source.id

    document_id = await pipeline.ingest_file(md, source_id, str(md), pipeline_factory)
    await pipeline.wait_idle()

    assert calls == [document_id]
    async with pipeline_factory() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value


async def test_pipeline_estrazione_fallita_non_blocca_ingest(
    pipeline_factory, kg_on, tmp_path, monkeypatch
) -> None:
    """Eccezione nell'estrattore: il documento resta indexed (best-effort)."""

    async def boom(document_id: int, *, session: Any, llm: Any) -> dict[str, Any]:
        raise RuntimeError("LLM irraggiungibile")

    monkeypatch.setattr(pipeline, "extract_for_document", boom)
    monkeypatch.setattr(pipeline, "get_kimi_client", lambda: object())

    md = tmp_path / "kg_fail.md"
    md.write_text("# Doc\n\nContenuto qualsiasi.\n", encoding="utf-8")
    async with pipeline_factory() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        source_id = source.id

    document_id = await pipeline.ingest_file(md, source_id, str(md), pipeline_factory)
    await pipeline.wait_idle()

    async with pipeline_factory() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value
        assert document.error is None


# --- API REST ----------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Any) -> AsyncIterator[None]:
    from app.api.main import lifespan

    async with lifespan(app):
        yield


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App di test: DB/Qdrant/upload in tmp_path, segreti fissi (come test_api)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "segreto-di-test-lungo-almeno-32-bytes!!")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    get_settings.cache_clear()
    db_module.reset_engine()
    pipeline.reset_state()

    from app.api.main import create_app

    app = create_app()
    async with (
        _lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c

    db_module.reset_engine()
    pipeline.reset_state()
    get_settings.cache_clear()


async def _login(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _enable_kg(monkeypatch: Any) -> None:
    """Attiva il KG a caldo (le route leggono get_settings() a ogni richiesta)."""
    monkeypatch.setenv("KG_ENABLED", "true")
    get_settings.cache_clear()


async def _seed_graph() -> tuple[int, int]:
    """Documento + entità/relation direttamente nel DB dell'app di test."""
    sf = get_session_factory()
    async with sf() as db:
        source = Source(name="archivio", type="local_fs", config_json={})
        db.add(source)
        await db.flush()
        document = Document(
            source_id=source.id,
            external_id="/tmp/contratto.md",
            title="Contratto fornitura",
            doc_type="md",
            status=DocumentStatus.indexed.value,
        )
        db.add(document)
        await db.flush()
        rossi = Entity(
            name="mario rossi",
            display_name="Mario Rossi",
            type="persona",
            description="Legale rappresentante",
        )
        contratto = Entity(
            name="contratto 2024/15",
            display_name="Contratto 2024/15",
            type="contratto",
            description="Fornitura",
        )
        db.add_all([rossi, contratto])
        await db.flush()
        db.add_all(
            [
                EntityMention(entity_id=rossi.id, document_id=document.id),
                EntityMention(entity_id=contratto.id, document_id=document.id),
                Relation(
                    subject_id=rossi.id,
                    predicate="ha_firmato",
                    object_id=contratto.id,
                    document_id=document.id,
                ),
            ]
        )
        await db.commit()
        return document.id, rossi.id


async def test_api_kg_disabilitato_risponde_404(client, monkeypatch) -> None:
    """KG disabilitato: 404 con dettaglio che spiega l'attivazione."""
    # Ermetico rispetto al .env reale della macchina (che può avere KG_ENABLED=true)
    monkeypatch.setenv("KG_ENABLED", "false")
    get_settings.cache_clear()
    token = await _login(client)
    resp = await client.get("/api/kg/entities", headers=_auth(token))
    assert resp.status_code == 404
    assert "KG_ENABLED" in resp.json()["detail"]


async def test_api_kg_richiede_autenticazione(client) -> None:
    """Senza token le route del knowledge graph non sono accessibili."""
    resp = await client.get("/api/kg/entities")
    assert resp.status_code == 401


async def test_api_kg_entita_contesto_e_documenti(client, monkeypatch) -> None:
    """KG abilitato: ricerca entità, contesto completo e documenti per entità."""
    _enable_kg(monkeypatch)
    doc_id, rossi_id = await _seed_graph()
    token = await _login(client)

    resp = await client.get("/api/kg/entities?q=rossi", headers=_auth(token))
    assert resp.status_code == 200
    entities = resp.json()["entities"]
    assert len(entities) == 1
    assert entities[0]["display_name"] == "Mario Rossi"
    assert entities[0]["document_count"] == 1

    resp = await client.get(f"/api/kg/entities/{rossi_id}", headers=_auth(token))
    assert resp.status_code == 200
    context = resp.json()
    assert context["entity"]["display_name"] == "Mario Rossi"
    assert context["documents"] == [
        {"document_id": doc_id, "title": "Contratto fornitura"}
    ]
    assert context["relations"][0]["predicate"] == "ha_firmato"
    assert context["relations"][0]["object"]["display_name"] == "Contratto 2024/15"

    resp = await client.get("/api/kg/entities/99999", headers=_auth(token))
    assert resp.status_code == 404

    resp = await client.get(
        "/api/kg/documents?entities=Mario Rossi,Contratto 2024/15&mode=all",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    documents = resp.json()["documents"]
    assert [d["document_id"] for d in documents] == [doc_id]
    assert documents[0]["matched_count"] == 2


async def test_api_kg_graph_nodi_e_archi(client, monkeypatch) -> None:
    """GET /api/kg/graph: nodi (con conteggio documenti), archi e totali."""
    _enable_kg(monkeypatch)
    doc_id, rossi_id = await _seed_graph()
    token = await _login(client)

    resp = await client.get("/api/kg/graph", headers=_auth(token))
    assert resp.status_code == 200
    graph = resp.json()

    nodes = {n["id"]: n for n in graph["nodes"]}
    assert len(nodes) == 2
    assert nodes[rossi_id]["display_name"] == "Mario Rossi"
    assert nodes[rossi_id]["document_count"] == 1
    assert graph["total_nodes"] == 2
    assert graph["total_edges"] == 1

    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["subject_id"] == rossi_id
    assert edge["predicate"] == "ha_firmato"
    assert edge["document_id"] == doc_id
    assert edge["document_title"] == "Contratto fornitura"
    assert edge["object_id"] in nodes


async def test_api_kg_graph_limit_top_n(client, monkeypatch) -> None:
    """Con ``limit`` il grafo espone le top-N entità per document_count
    (a parità, id crescente) e SOLO gli archi interni al sottoinsieme;
    i totali restano quelli complessivi."""
    _enable_kg(monkeypatch)
    doc_id, _rossi_id = await _seed_graph()
    # Terza entità con DUE documenti: deve vincere sul document_count
    sf = get_session_factory()
    async with sf() as db:
        doc2 = Document(
            source_id=1,
            external_id="/tmp/allegato.md",
            title="Allegato",
            doc_type="md",
            status=DocumentStatus.indexed.value,
        )
        db.add(doc2)
        await db.flush()
        alfa = Entity(name="progetto alfa", display_name="Progetto Alfa", type="progetto")
        db.add(alfa)
        await db.flush()
        db.add_all(
            [
                EntityMention(entity_id=alfa.id, document_id=doc_id),
                EntityMention(entity_id=alfa.id, document_id=doc2.id),
            ]
        )
        await db.commit()
        alfa_id = alfa.id
    token = await _login(client)

    resp = await client.get("/api/kg/graph?limit=1", headers=_auth(token))
    assert resp.status_code == 200
    graph = resp.json()
    assert [n["id"] for n in graph["nodes"]] == [alfa_id]
    # L'unico arco (rossi → contratto) ha entrambe le estremità FUORI dal
    # sottoinsieme: niente archi
    assert graph["edges"] == []
    assert graph["total_nodes"] == 3
    assert graph["total_edges"] == 1

    # limit fuori range → 422
    assert (
        await client.get("/api/kg/graph?limit=0", headers=_auth(token))
    ).status_code == 422
    assert (
        await client.get("/api/kg/graph?limit=2001", headers=_auth(token))
    ).status_code == 422


async def test_api_delete_documento_pulisce_knowledge_graph(client) -> None:
    """La route DELETE rimuove anche mention/relation e le entità orfane."""
    doc_id, rossi_id = await _seed_graph()
    token = await _login(client)

    resp = await client.delete(f"/api/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 204

    sf = get_session_factory()
    async with sf() as db:
        assert await db.scalar(select(func.count()).select_from(EntityMention)) == 0
        assert await db.scalar(select(func.count()).select_from(Relation)) == 0
        # Entità rimaste orfane: pulite dalla cancellazione
        assert await db.scalar(select(func.count()).select_from(Entity)) == 0
        assert await db.get(Entity, rossi_id) is None


# --- strumenti agente -----------------------------------------------------------


async def test_tools_kg_disabilitato_messaggio_esplicito(tmp_settings, monkeypatch) -> None:
    """Con KG spento gli strumenti rispondono "disabilitato" senza fallire."""
    monkeypatch.setenv("KG_ENABLED", "false")
    get_settings.cache_clear()
    try:
        search = await agent_tools.cerca_entita("rossi", session=None)
        context = await agent_tools.contesto_entita(1, session=None)
        docs = await agent_tools.documenti_per_entita(["rossi"], session=None)
    finally:
        get_settings.cache_clear()
    assert "disabilitato" in search["detail"].lower()
    assert search["entities"] == []
    assert "disabilitato" in context["detail"].lower()
    assert "disabilitato" in docs["detail"].lower()
    assert docs["documents"] == []
