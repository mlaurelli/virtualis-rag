"""Estrazione di entità e relazioni dai documenti indicizzati.

Il flusso per documento è: raccolta del testo (``context_text`` dei chunk in
ordine, troncato a ``kg_max_chars_per_doc``) → chiamata LLM in JSON mode con
un singolo retry su JSON imperfetto → upsert delle entità (match su
``name`` normalizzato + ``type``) → cancellazione e riscrittura di mention e
relation del documento (idempotente al re-ingest) → pulizia delle entità
orfane.

Best-effort per contratto: :func:`extract_for_document` non solleva MAI
eccezioni verso la pipeline; qualunque fallimento (LLM non configurato, JSON
irrecuperabile, errore DB) è riportato nel campo ``error`` del report.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..llm import KimiClient, extract_json
from ..models import Chunk, Document, Entity, EntityMention, Relation
from .prompts import EXTRACT_PROMPT

log = structlog.get_logger(__name__)

# Vocabolario chiuso dei tipi di entità (vedi prompts.EXTRACT_PROMPT)
ENTITY_TYPES = frozenset(
    {
        "persona",
        "organizzazione",
        "contratto",
        "progetto",
        "data",
        "importo",
        "prodotto",
        "altro",
    }
)


def normalize_name(name: str) -> str:
    """Forma normalizzata per il match: lowercase, spazi compattati."""
    return " ".join(name.strip().lower().split())


def _normalize_predicate(predicate: str) -> str:
    """Predicato in forma snake_case breve (es. "ha firmato" → "ha_firmato")."""
    return "_".join(predicate.strip().lower().split())[:128]


async def _chat_json(llm: KimiClient, system: str, user: str) -> dict[str, Any]:
    """Chat in JSON mode con parsing tollerante e UN retry di correzione.

    Stessa meccanica di :func:`app.agent.graph._chat_json`: se il modello
    produce JSON imperfetto gli si rimanda l'output chiedendo la correzione;
    al secondo fallimento solleva ``ValueError``.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    first = await llm.chat(messages, json_mode=True)
    try:
        return extract_json(first)
    except ValueError:
        log.warning("JSON imperfetto dall'estrattore KG: retry di correzione")
    messages.append({"role": "assistant", "content": first})
    messages.append(
        {
            "role": "user",
            "content": (
                "La risposta precedente non era un JSON valido. "
                "Rispondi SOLO con l'oggetto JSON richiesto, senza testo aggiuntivo."
            ),
        }
    )
    return extract_json(await llm.chat(messages, json_mode=True))


async def _delete_orphan_entities(session: AsyncSession) -> None:
    """Elimina le entità senza alcuna mention né relazione residua."""
    has_mention = (
        select(EntityMention.id).where(EntityMention.entity_id == Entity.id).exists()
    )
    has_relation = (
        select(Relation.id)
        .where((Relation.subject_id == Entity.id) | (Relation.object_id == Entity.id))
        .exists()
    )
    orphan_ids = select(Entity.id).where(~has_mention, ~has_relation)
    await session.execute(delete(Entity).where(Entity.id.in_(orphan_ids)))


async def delete_document_graph(document_id: int, *, session: AsyncSession) -> None:
    """Rimuove mention e relation di un documento e le entità orfane.

    Chiamata da ogni percorso di cancellazione (route DELETE, connettori,
    re-ingest): il commit è responsabilità del chiamante.
    """
    # Import locale: query.py importa questo modulo a livello di modulo
    from .query import invalidate_entity_cache

    await session.execute(
        delete(EntityMention).where(EntityMention.document_id == document_id)
    )
    await session.execute(
        delete(Relation).where(Relation.document_id == document_id)
    )
    await _delete_orphan_entities(session)
    invalidate_entity_cache()


async def _upsert_entity(
    session: AsyncSession, *, name: str, display: str, etype: str, description: str
) -> Entity:
    """Entità esistente (match su name+type normalizzati) o nuova riga."""
    entity = await session.scalar(
        select(Entity).where(Entity.name == name, Entity.type == etype)
    )
    if entity is None:
        entity = Entity(
            name=name, display_name=display, type=etype, description=description
        )
        session.add(entity)
        await session.flush()
        return entity
    if display and not entity.display_name:
        entity.display_name = display
    if description and not entity.description:
        entity.description = description
    return entity


async def extract_for_document(
    document_id: int, *, session: AsyncSession, llm: KimiClient
) -> dict[str, Any]:
    """Estrae entità e relazioni di un documento e le persiste nel grafo.

    Restituisce un report ``{"document_id", "n_entities", "n_mentions",
    "n_relations", "error"}``. NON solleva eccezioni: ogni fallimento è
    riportato in ``error`` (il documento resta comunque ``indexed``).
    """
    settings = get_settings()
    report: dict[str, Any] = {
        "document_id": document_id,
        "n_entities": 0,
        "n_mentions": 0,
        "n_relations": 0,
        "error": None,
    }
    try:
        document = await session.get(Document, document_id)
        if document is None:
            report["error"] = f"documento {document_id} non trovato"
            return report
        chunks = (
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.document_id == document_id)
                    .order_by(Chunk.ord)
                )
            )
            .scalars()
            .all()
        )
        text = "\n\n".join((c.context_text or c.text) for c in chunks).strip()
        if not text:
            report["error"] = "documento senza testo indicizzato"
            return report
        text = text[: settings.kg_max_chars_per_doc]

        data = await _chat_json(llm, EXTRACT_PROMPT, text)

        # Reset idempotente: mention e relation del documento sono riscritte
        await session.execute(
            delete(EntityMention).where(EntityMention.document_id == document_id)
        )
        await session.execute(
            delete(Relation).where(Relation.document_id == document_id)
        )

        raw_entities = data.get("entities") or []
        raw_relations = data.get("relations") or []

        by_name: dict[str, Entity] = {}  # name normalizzato → entità (per le relation)
        for raw in raw_entities[: settings.kg_max_entities_per_doc]:
            if not isinstance(raw, dict):
                continue
            display = str(raw.get("name") or "").strip()
            if not display:
                continue
            name = normalize_name(display)
            if name in by_name:
                continue  # duplicato nel output del modello
            etype = str(raw.get("type") or "").strip().lower()
            if etype not in ENTITY_TYPES:
                etype = "altro"
            description = str(raw.get("description") or "").strip()
            entity = await _upsert_entity(
                session,
                name=name,
                display=display,
                etype=etype,
                description=description,
            )
            session.add(
                EntityMention(
                    entity_id=entity.id, document_id=document_id, chunk_id=None
                )
            )
            by_name[name] = entity

        seen: set[tuple[int, str, int]] = set()
        for raw in raw_relations:
            if not isinstance(raw, dict):
                continue
            subject = by_name.get(normalize_name(str(raw.get("subject") or "")))
            obj = by_name.get(normalize_name(str(raw.get("object") or "")))
            if subject is None or obj is None:
                continue  # relazioni solo tra entità estratte
            predicate = _normalize_predicate(str(raw.get("predicate") or ""))
            if not predicate:
                continue
            key = (subject.id, predicate, obj.id)
            if key in seen:
                continue
            seen.add(key)
            session.add(
                Relation(
                    subject_id=subject.id,
                    predicate=predicate,
                    object_id=obj.id,
                    document_id=document_id,
                    chunk_id=None,
                )
            )

        # Entità rimaste orfane dopo la riscrittura (versioni precedenti)
        await session.flush()
        await _delete_orphan_entities(session)
        await session.commit()
        # Import locale: query.py importa questo modulo a livello di modulo
        from .query import invalidate_entity_cache

        invalidate_entity_cache()

        report["n_entities"] = len(by_name)
        report["n_mentions"] = len(by_name)
        report["n_relations"] = len(seen)
    except Exception as exc:  # best-effort: MAI propagare verso la pipeline
        await session.rollback()
        log.warning("estrazione KG fallita", document_id=document_id, error=str(exc))
        report["error"] = str(exc) or type(exc).__name__
    return report
