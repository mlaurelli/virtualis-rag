"""API di interrogazione del knowledge graph documentale.

Funzioni di sola lettura usate dagli strumenti dell'agente, dal server MCP e
dalle route REST: ricerca testuale di entità, contesto di una singola entità,
recupero multi-documento per entità e sottografo di relazioni.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Document, Entity, EntityMention, Relation
from .extractor import normalize_name

# Nomi di entità più corti di così non sono mai cercati per sottostringa
# nel testo libero: troppo ambigui (falsi positivi quasi certi)
_MIN_NAME_MATCH_LEN = 4

# Cache in memoria dell'elenco entità per il match deterministico
# (:func:`match_entities_in_text`): con migliaia di entità la SELECT completa
# a ogni domanda agentica è inutilmente costosa. La cache è legata all'engine
# di provenienza (URL del DB: i test usano DB temporanei diversi) e scade dopo
# ``_ENTITY_CACHE_TTL_S`` o su scrittura del grafo (:func:`invalidate_entity_cache`).
_ENTITY_CACHE_TTL_S = 120.0
# (chiave engine, timestamp monotonic, righe (id, name, display_name, type))
_entity_cache: tuple[str, float, list[tuple[int, str, str, str]]] | None = None


def invalidate_entity_cache() -> None:
    """Invalida la cache delle entità (chiamata dopo ogni scrittura del KG)."""
    global _entity_cache
    _entity_cache = None


def _entity_out(entity: Entity) -> dict[str, Any]:
    """Serializza un'entità nei campi pubblici."""
    return {
        "id": entity.id,
        "name": entity.name,
        "display_name": entity.display_name,
        "type": entity.type,
        "description": entity.description,
    }


async def search_entities(
    q: str, *, session: AsyncSession, limit: int = 10
) -> list[dict[str, Any]]:
    """Match testuale (LIKE case-insensitive) su name/display_name.

    Ogni entità è corredata dal numero di documenti che la menzionano.
    """
    pattern = f"%{q.strip().lower()}%"
    doc_count = func.count(func.distinct(EntityMention.document_id))
    stmt = (
        select(Entity, doc_count)
        .outerjoin(EntityMention, EntityMention.entity_id == Entity.id)
        .where(
            func.lower(Entity.name).like(pattern)
            | func.lower(Entity.display_name).like(pattern)
        )
        .group_by(Entity.id)
        .order_by(doc_count.desc(), Entity.name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {**_entity_out(entity), "document_count": count} for entity, count in rows
    ]


async def entity_context(
    entity_id: int, *, session: AsyncSession
) -> dict[str, Any] | None:
    """Contesto completo di un'entità: documenti che la menzionano e relazioni.

    Le relazioni sono riportate in entrambe le direzioni (l'entità come
    soggetto e come oggetto), con la controparte e il documento di
    provenienza. ``None`` se l'entità non esiste.
    """
    entity = await session.get(Entity, entity_id)
    if entity is None:
        return None

    documents = (
        (
            await session.execute(
                select(Document.id, Document.title)
                .join(EntityMention, EntityMention.document_id == Document.id)
                .where(EntityMention.entity_id == entity_id)
                .order_by(Document.id)
            )
        )
        .all()
    )

    relations = (
        (
            await session.execute(
                select(Relation).where(
                    (Relation.subject_id == entity_id)
                    | (Relation.object_id == entity_id)
                )
            )
        )
        .scalars()
        .all()
    )
    entity_ids = {r.subject_id for r in relations} | {r.object_id for r in relations}
    entities_by_id: dict[int, Entity] = {}
    if entity_ids:
        entities_by_id = {
            e.id: e
            for e in (
                (
                    await session.execute(
                        select(Entity).where(Entity.id.in_(entity_ids))
                    )
                )
                .scalars()
                .all()
            )
        }
    doc_ids = {r.document_id for r in relations}
    titles: dict[int, str] = {}
    if doc_ids:
        titles = {
            doc_id: title
            for doc_id, title in (
                await session.execute(
                    select(Document.id, Document.title).where(Document.id.in_(doc_ids))
                )
            )
            .tuples()
            .all()
        }

    def _ref(eid: int) -> dict[str, Any]:
        e = entities_by_id.get(eid)
        return _entity_out(e) if e else {"id": eid, "display_name": "", "type": ""}

    return {
        "entity": _entity_out(entity),
        "documents": [
            {"document_id": doc_id, "title": title} for doc_id, title in documents
        ],
        "relations": [
            {
                "id": r.id,
                "predicate": r.predicate,
                "subject": _ref(r.subject_id),
                "object": _ref(r.object_id),
                "document_id": r.document_id,
                "document_title": titles.get(r.document_id, ""),
            }
            for r in relations
        ],
    }


async def documents_for_entities(
    names: list[str], *, session: AsyncSession, mode: str = "any"
) -> list[dict[str, Any]]:
    """Documenti che menzionano le entità date (recupero multi-documento).

    ``mode="any"``: almeno una delle entità; ``mode="all"``: tutte (almeno
    una mention per ciascun nome richiesto). I nomi sono confrontati in forma
    normalizzata (lowercase, spazi compattati).
    """
    requested = {normalize_name(n) for n in names if n and n.strip()}
    if not requested:
        return []
    rows = (
        (
            await session.execute(
                select(
                    Document.id,
                    Document.title,
                    Document.doc_type,
                    Entity.name,
                    Entity.display_name,
                )
                .join(EntityMention, EntityMention.document_id == Document.id)
                .join(Entity, EntityMention.entity_id == Entity.id)
                .where(Entity.name.in_(requested))
                .order_by(Document.id)
            )
        )
        .all()
    )
    by_doc: dict[int, dict[str, Any]] = {}
    matched_names: dict[int, set[str]] = {}
    for doc_id, title, doc_type, name, display in rows:
        entry = by_doc.setdefault(
            doc_id,
            {
                "document_id": doc_id,
                "title": title,
                "doc_type": doc_type,
                "matched_entities": [],
            },
        )
        if display not in entry["matched_entities"]:
            entry["matched_entities"].append(display)
        matched_names.setdefault(doc_id, set()).add(name)

    result = []
    for doc_id, entry in by_doc.items():
        matched = matched_names[doc_id]
        if mode == "all" and not requested.issubset(matched):
            continue
        entry["matched_count"] = len(matched)
        result.append(entry)
    result.sort(key=lambda e: (-int(e["matched_count"]), int(e["document_id"])))
    return result


async def match_entities_in_text(
    text: str, *, session: AsyncSession, limit: int = 20
) -> list[dict[str, Any]]:
    """Entità del grafo il cui nome compare in ``text`` (match deterministico).

    Il confronto è case-insensitive sulla forma normalizzata (lowercase,
    spazi compattati) e richiede i confini di parola: ``alfa`` non matcha
    ``alfabetico``. I nomi più corti di ``_MIN_NAME_MATCH_LEN`` caratteri
    sono ignorati: per essi una sottostringa è quasi sempre un falso
    positivo. Serve al nodo ``kg_lookup`` del ciclo agentico per riconoscere
    le entità citate nel quesito senza una chiamata LLM dedicata.
    """
    normalized = normalize_name(text)
    if not normalized:
        return []
    rows = await _entity_rows(session)
    # Matching CPU-bound (regex su ogni candidato): in thread, fuori dal loop
    return await asyncio.to_thread(_match_rows, normalized, rows, limit)


async def _entity_rows(session: AsyncSession) -> list[tuple[int, str, str, str]]:
    """Righe ``(id, name, display_name, type)`` di tutte le entità, cacheate.

    La chiave della cache è l'URL dell'engine della sessione: un engine
    diverso (es. DB temporaneo di un altro test) non riusa righe stantie.
    """
    global _entity_cache
    # ``get_bind()`` può restituire Engine o Connection: ``.engine`` copre entrambi
    key = str(session.get_bind().engine.url)
    now = time.monotonic()
    if (
        _entity_cache is not None
        and _entity_cache[0] == key
        and now - _entity_cache[1] < _ENTITY_CACHE_TTL_S
    ):
        return _entity_cache[2]
    rows = [
        (int(entity_id), name, display_name, entity_type)
        for entity_id, name, display_name, entity_type in (
            await session.execute(
                select(Entity.id, Entity.name, Entity.display_name, Entity.type)
                .order_by(Entity.id)
            )
        ).all()
    ]
    _entity_cache = (key, now, rows)
    return rows


def _match_rows(
    normalized: str, rows: list[tuple[int, str, str, str]], limit: int
) -> list[dict[str, Any]]:
    """Match deterministico (sincrono): prefiltro veloce + conferma regex.

    Il prefiltro ``name in normalized`` scarta in O(len) la stragrande
    maggioranza dei nomi; la regex con i confini di parola (costosa) è
    compilata solo per i candidati sopravvissuti.
    """
    matches: list[dict[str, Any]] = []
    for entity_id, name, display_name, entity_type in rows:
        if len(name) < _MIN_NAME_MATCH_LEN:
            continue
        # Prefiltro economico: se il nome non è nemmeno una sottostringa,
        # inutile pagare la regex
        if name not in normalized:
            continue
        # Confini "di parola" che tollerano segni non alfanumerici ai bordi
        # del nome (es. "acme s.p.a."): niente caratteri di parola adiacenti
        pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
        if re.search(pattern, normalized):
            matches.append(
                {
                    "id": entity_id,
                    "name": name,
                    "display_name": display_name,
                    "type": entity_type,
                }
            )
            if len(matches) >= limit:
                break
    return matches


async def full_graph(*, session: AsyncSession, limit: int = 300) -> dict[str, Any]:
    """Nodi e archi del grafo per la visualizzazione nella UI (top-N entità).

    Con grafi grandi (migliaia di entità) l'intero grafo non è né renderizzabile
    né trasferibile: si restituiscono le ``limit`` entità più menzionate
    (``document_count`` decrescente, poi id crescente per determinismo) e i
    soli archi con ENTRAMBE le estremità nel sottoinsieme. ``total_nodes`` e
    ``total_edges`` riportano i conteggi complessivi. Nessun dato LLM.
    """
    total_nodes = (
        await session.scalar(select(func.count()).select_from(Entity))
    ) or 0
    total_edges = (
        await session.scalar(select(func.count()).select_from(Relation))
    ) or 0

    doc_count = func.count(func.distinct(EntityMention.document_id))
    rows = (
        (
            await session.execute(
                select(Entity, doc_count)
                .outerjoin(EntityMention, EntityMention.entity_id == Entity.id)
                .group_by(Entity.id)
                .order_by(doc_count.desc(), Entity.id)
                .limit(limit)
            )
        )
        .all()
    )
    nodes = [{**_entity_out(entity), "document_count": count} for entity, count in rows]
    node_ids = {node["id"] for node in nodes}

    relations: list[Relation] = []
    if node_ids:
        relations = list(
            (
                await session.execute(
                    select(Relation)
                    .where(
                        Relation.subject_id.in_(node_ids),
                        Relation.object_id.in_(node_ids),
                    )
                    .order_by(Relation.id)
                )
            )
            .scalars()
            .all()
        )
    doc_ids = {r.document_id for r in relations}
    titles: dict[int, str] = {}
    if doc_ids:
        titles = {
            doc_id: title
            for doc_id, title in (
                await session.execute(
                    select(Document.id, Document.title).where(Document.id.in_(doc_ids))
                )
            )
            .tuples()
            .all()
        }
    edges = [
        {
            "id": r.id,
            "subject_id": r.subject_id,
            "object_id": r.object_id,
            "predicate": r.predicate,
            "document_id": r.document_id,
            "document_title": titles.get(r.document_id, ""),
        }
        for r in relations
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
    }


async def entity_subgraph(
    names: list[str], *, session: AsyncSession, depth: int = 1
) -> dict[str, Any]:
    """Sottografo delle relazioni attorno alle entità date.

    Espansione per ampiezza fino a ``depth`` salti (minimo 1): utile per
    domande tipo "che relazione c'è tra X e Y?". Restituisce le entità
    coinvolte e le relazioni con display delle controparti e provenienza.
    """
    requested = {normalize_name(n) for n in names if n and n.strip()}
    if not requested:
        return {"entities": [], "relations": []}
    seeds = (
        (
            await session.execute(select(Entity).where(Entity.name.in_(requested)))
        )
        .scalars()
        .all()
    )
    entities_by_id: dict[int, Entity] = {e.id: e for e in seeds}
    frontier = set(entities_by_id)
    relations: dict[int, Relation] = {}
    for _ in range(max(depth, 1)):
        if not frontier:
            break
        found = (
            (
                await session.execute(
                    select(Relation).where(
                        Relation.subject_id.in_(frontier)
                        | Relation.object_id.in_(frontier)
                    )
                )
            )
            .scalars()
            .all()
        )
        next_frontier: set[int] = set()
        for relation in found:
            relations[relation.id] = relation
            for eid in (relation.subject_id, relation.object_id):
                if eid not in entities_by_id:
                    next_frontier.add(eid)
        if next_frontier:
            for e in (
                (
                    await session.execute(
                        select(Entity).where(Entity.id.in_(next_frontier))
                    )
                )
                .scalars()
                .all()
            ):
                entities_by_id[e.id] = e
        frontier = next_frontier

    doc_ids = {r.document_id for r in relations.values()}
    titles: dict[int, str] = {}
    if doc_ids:
        titles = {
            doc_id: title
            for doc_id, title in (
                await session.execute(
                    select(Document.id, Document.title).where(Document.id.in_(doc_ids))
                )
            )
            .tuples()
            .all()
        }
    return {
        "entities": [_entity_out(e) for e in entities_by_id.values()],
        "relations": [
            {
                "id": r.id,
                "subject": entities_by_id[r.subject_id].display_name,
                "subject_id": r.subject_id,
                "predicate": r.predicate,
                "object": entities_by_id[r.object_id].display_name,
                "object_id": r.object_id,
                "document_id": r.document_id,
                "document_title": titles.get(r.document_id, ""),
            }
            for r in relations.values()
        ],
    }
