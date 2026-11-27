"""Research agent ReAct con tool-calling (architettura agentica v2).

Sul percorso agentic (``agent_mode="react"``) questo modulo sostituisce i
nodi ``retrieve``/``evaluate`` del grafo classico con un loop *Reason+Act*:
il modello, tramite il function calling OpenAI (``chat_with_tools`` di
:class:`app.llm.KimiClient`), decide quale strumento documentale invocare;
gli esiti (compatti) rientrano nei messaggi e il loop itera finché il modello
risponde senza tool_call, fino a ``react_max_rounds`` round.

I passaggi raccolti da ``hybrid_search`` sono restituiti nello STESSO formato
del nodo ``retrieve`` (dict serializzati per ``chunk_id``), così i nodi a
valle (``synthesize``/``verify``) restano invariati. Ogni chiamata a
strumento è tracciata come step ``research_tool`` (strumento, argomenti,
n. risultati) e finisce quindi in SSE/audit.

RESILIENZA: se il modello non produce tool_call validi per 2 round
consecutivi (strumento sconosciuto o argomenti JSON malformati) o la chiamata
LLM fallisce, :func:`run_research` solleva :class:`ReActUnavailable` e il
grafo ripiega sul percorso classico (``retrieve`` + ``evaluate``) con un
warning tracciato. ``LLMNotConfiguredError`` ed errori di saldo si propagano
invece al chiamante.

HYDE: se ``hyde_enabled``, ogni ``hybrid_search`` (qui e nel nodo ``retrieve``
classico) è preceduta dalla generazione di un breve passaggio IPOTETICO di
risposta; si eseguono due ricerche ibride (quesito + testo HyDE) e i
risultati sono fusi tenendo per ogni chunk lo score massimo (fusione
max-score). HyDE riduce il mismatch lessicale tra la forma della domanda e il
linguaggio dei documenti: il testo ipotetico "somiglia" ai documenti cercati
più della domanda stessa.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..llm import (
    KimiClient,
    LLMInsufficientBalanceError,
    LLMNotConfiguredError,
    ToolCall,
)
from . import tools
from .prompts import HYDE_PROMPT, RESEARCH_PROMPT

log = structlog.get_logger(__name__)

# Risultati di hybrid_search riportati al modello per ogni ricerca
_MAX_HITS_IN_MESSAGES = 5
# Troncamento dello snippet nei messaggi di esito (caratteri)
_SNIPPET_MAX_CHARS = 300
# Troncamento del contenuto di read_document nei messaggi (caratteri)
_READ_MAX_CHARS = 2000
# Troncamento degli esiti JSON degli strumenti KG (caratteri)
_KG_MAX_CHARS = 1500
# Documenti elencati al massimo nell'esito di list_documents
_LIST_MAX_ITEMS = 10
# Round consecutivi senza tool_call validi prima del fallback al grafo
_MAX_MALFORMED_ROUNDS = 2


class ReActUnavailable(RuntimeError):
    """Il loop ReAct non è utilizzabile: il grafo ripiega sul percorso classico."""


# --- guardia sulla lingua delle interrogazioni generate dal modello -------------
# I modelli locali a volte "scivolano" in inglese nelle query generate (HyDE,
# plan, riformulazioni, ReAct): una query inglese su un corpus italiano
# produce match deboli e fuorvianti. Euristica CONSERVATIVA senza dipendenze:
# si scarta una query solo con evidenza netta di inglese e nessuna di italiano.

_IT_MARKERS = frozenset({
    "il", "lo", "la", "le", "gli", "di", "del", "della", "dei", "delle",
    "che", "per", "con", "una", "uno", "un", "quali", "quale", "come",
    "dove", "quando", "chi", "cosa", "sono", "nel", "nella", "nelle",
    "negli", "tra", "fra", "non", "si", "al", "alla", "ai", "alle",
    "degli", "sul", "sulla", "è", "ed", "anche", "più", "questo",
    "questa", "essere", "viene", "vengono", "deve", "devono",
})
_EN_MARKERS = frozenset({
    "the", "and", "for", "with", "how", "what", "which", "when", "where",
    "from", "this", "that", "are", "is", "of", "to", "by", "on", "you",
    "does", "do", "can", "will", "should", "would", "about", "into",
    "their", "there",
})


def looks_italian(text: str) -> bool:
    """True se il testo sembra italiano (o non è giudicabile).

    Regole, in ordine: lettere accentate italiane → italiano; almeno una
    stopword italiana → italiano; due o più stopword inglesi (e nessuna
    italiana) → NON italiano; altrimenti (query di sole keyword) si assume
    italiano — meglio una query dubbia in più che scartare keyword valide.
    """
    lowered = text.lower()
    if any(ch in "àèéìòù" for ch in lowered):
        return True
    words = re.findall(r"[a-z]+", lowered)
    if not words:
        return True
    if any(w in _IT_MARKERS for w in words):
        return True
    return sum(w in _EN_MARKERS for w in words) < 2


def ensure_italian_query(query: str, fallback: str) -> str:
    """Restituisce ``query`` se sembra italiana, altrimenti ``fallback``.

    Usata su TUTTE le interrogazioni generate dal modello (HyDE, plan,
    riformulazioni di evaluate, tool-calling ReAct): il fallback è il
    quesito autonomo dell'utente.
    """
    if looks_italian(query):
        return query
    log.warning(
        "query generata non in italiano: sostituita con il quesito",
        query=query[:120],
    )
    return fallback


def _step(step: str, detail: str) -> dict[str, Any]:
    """Voce di tracciamento (stessa forma di ``app.agent.graph._step``)."""
    return {
        "step": step,
        "detail": detail,
        "ts": datetime.now(UTC).isoformat(),
    }


# --- schemi degli strumenti (function calling OpenAI) -------------------------

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "hybrid_search",
            "description": (
                "Ricerca ibrida (semantica + lessicale) sui passaggi dei "
                "documenti aziendali indicizzati. Strumento principale."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Interrogazione autonoma, con entità, nomi propri "
                            "e date espliciti"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Numero massimo di passaggi (default 8)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Legge un documento intero: metadati e testo completo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "ID del documento da leggere",
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "Elenca i documenti noti al sistema (metadati, non il "
                "contenuto), con filtri opzionali."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "integer",
                        "description": "Filtra per sorgente (ID)",
                    },
                    "doc_type": {
                        "type": "string",
                        "description": "Filtra per tipo documento (es. pdf, docx)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cerca_entita",
            "description": (
                "Cerca entità del knowledge graph per nome (persone, "
                "organizzazioni, contratti, date, importi)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Nome (o parte) dell'entità da cercare",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contesto_entita",
            "description": (
                "Documenti che menzionano un'entità del knowledge graph e le "
                "sue relazioni con le altre entità."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "integer",
                        "description": "ID dell'entità (da cerca_entita)",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
]

_TOOL_NAMES = frozenset(
    spec["function"]["name"] for spec in TOOLS_SPEC
)


# --- HyDE e fusione dei risultati ----------------------------------------------


def fuse_hits(*hit_lists: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Fonde più liste di risultati tenendo per ogni chunk lo score MASSIMO.

    Fusione max-score (semplice e robusta anche con una sola lista extra):
    gli score delle ricerche ibride sono già normalizzati sulla stessa scala,
    quindi il massimo preserva l'ordinamento migliore tra le due viste
    (quesito originale e passaggio HyDE).
    """
    fused: dict[int, dict[str, Any]] = {}
    for hits in hit_lists:
        for hit in hits:
            chunk_id = int(hit["chunk_id"])
            current = fused.get(chunk_id)
            if current is None or (hit.get("score") or 0.0) > (
                current.get("score") or 0.0
            ):
                fused[chunk_id] = hit
    return sorted(
        fused.values(), key=lambda h: h.get("score") or 0.0, reverse=True
    )[:limit]


async def hyde_search(
    query: str,
    *,
    client: KimiClient,
    session: AsyncSession | None,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], str]:
    """Ricerca ibrida con HyDE: restituisce ``(hits fusi, testo HyDE)``.

    Se ``hyde_enabled`` è spento o la generazione del passaggio ipotetico
    fallisce, esegue la singola ricerca classica (testo HyDE vuoto).
    """
    settings = get_settings()
    if not settings.hyde_enabled:
        return await tools.hybrid_search(query, limit=limit, session=session), ""
    hyde_text = ""
    try:
        hyde_text = (
            await client.chat(
                [
                    {"role": "system", "content": HYDE_PROMPT},
                    {"role": "user", "content": f"QUESITO: {query}"},
                ],
                # Temperatura contenuta: il passaggio ipotetico deve restare
                # aderente ai termini del quesito (0.7 produceva derive
                # lessicali e cambi di lingua che inquinavano la ricerca)
                temperature=0.3,
            )
        ).strip()
    except (LLMNotConfiguredError, LLMInsufficientBalanceError):
        raise
    except Exception as exc:
        # HyDE è un arricchimento: senza di esso la ricerca resta valida
        log.warning("generazione HyDE non riuscita: ricerca singola", error=str(exc))
    if hyde_text and not looks_italian(hyde_text):
        # Testo ipotetico non in italiano: inquinerebbe la ricerca sul
        # corpus italiano — si degrada alla ricerca singola
        log.warning("testo HyDE non in italiano: scartato", testo=hyde_text[:120])
        hyde_text = ""
    if not hyde_text:
        return await tools.hybrid_search(query, limit=limit, session=session), ""
    original = await tools.hybrid_search(query, limit=limit, session=session)
    hyde = await tools.hybrid_search(hyde_text, limit=limit, session=session)
    return fuse_hits(original, hyde, limit=limit), hyde_text


# --- esecuzione degli strumenti ------------------------------------------------


def _compact_hits(hits: list[dict[str, Any]]) -> str:
    """Esito compatto di hybrid_search per i messaggi del loop ReAct."""
    if not hits:
        return "Nessun passaggio trovato per questa interrogazione."
    lines = []
    for n, hit in enumerate(hits[:_MAX_HITS_IN_MESSAGES], start=1):
        pagina = f", pag. {hit['page']}" if hit.get("page") is not None else ""
        snippet = (hit.get("snippet") or "")[:_SNIPPET_MAX_CHARS]
        lines.append(
            f"[{n}] «{hit.get('title') or ''}» — fonte: "
            f"{hit.get('source_name') or 'sconosciuta'}{pagina} "
            f"(documento {hit.get('document_id')}, chunk {hit['chunk_id']})\n"
            f"{snippet}"
        )
    extra = len(hits) - _MAX_HITS_IN_MESSAGES
    if extra > 0:
        lines.append(f"(...altri {extra} passaggi non mostrati)")
    return "\n\n".join(lines)


async def _exec_tool(
    call: ToolCall,
    *,
    client: KimiClient,
    session: AsyncSession | None,
    question: str = "",
) -> dict[str, Any]:
    """Esegue uno strumento reale di :mod:`app.agent.tools`.

    Restituisce ``{"text", "hits", "count", "query", "hyde"}``: ``text`` è
    l'esito compatto per i messaggi del loop, ``hits`` i passaggi da
    accumulare nello stato (solo hybrid_search), ``query`` l'interrogazione
    da tracciare in ``queries_run`` (se applicabile). ``question`` è il
    quesito autonomo: sostituisce le interrogazioni generate dal modello che
    non sembrano italiane (guardia sulla lingua).
    """
    name = call["name"]
    args = call["arguments"] or {}

    if name == "hybrid_search":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("argomento 'query' mancante o vuoto")
        if question:
            query = ensure_italian_query(query, question)
        raw_limit = args.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 8
        hits, hyde_text = await hyde_search(
            query, client=client, session=session, limit=limit
        )
        return {
            "text": _compact_hits(hits),
            "hits": hits,
            "count": len(hits),
            "query": query,
            "hyde": bool(hyde_text),
        }

    if name == "read_document":
        doc = await tools.read_document(int(args["document_id"]), session=session)  # type: ignore[arg-type]
        content = (doc.get("content") or "")[:_READ_MAX_CHARS]
        text = (
            f"«{doc.get('title') or ''}» ({doc.get('doc_type') or 'tipo?'}, "
            f"{doc.get('chunk_count') or 0} chunk, fonte: "
            f"{doc.get('source_name') or 'sconosciuta'})\n{content}"
        )
        return {"text": text, "hits": [], "count": 1, "query": None, "hyde": False}

    if name == "list_documents":
        docs = await tools.list_documents(
            session=session,  # type: ignore[arg-type]
            source_id=int(args["source_id"]) if args.get("source_id") is not None else None,
            doc_type=str(args["doc_type"]) if args.get("doc_type") else None,
        )
        lines = [
            f"- [{d['document_id']}] «{d.get('title') or ''}» "
            f"({d.get('doc_type') or 'tipo?'}, fonte: "
            f"{d.get('source_name') or 'sconosciuta'})"
            for d in docs[:_LIST_MAX_ITEMS]
        ]
        text = "\n".join(lines) or "Nessun documento corrisponde ai filtri."
        return {
            "text": text,
            "hits": [],
            "count": len(docs),
            "query": None,
            "hyde": False,
        }

    if name == "cerca_entita":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("argomento 'query' mancante o vuoto")
        outcome = await tools.cerca_entita(query, session=session)
        entities = outcome.get("entities") or []
        text = json.dumps(outcome, ensure_ascii=False, default=str)[:_KG_MAX_CHARS]
        return {
            "text": text,
            "hits": [],
            "count": len(entities),
            "query": None,
            "hyde": False,
        }

    if name == "contesto_entita":
        outcome = await tools.contesto_entita(int(args["entity_id"]), session=session)
        text = json.dumps(outcome, ensure_ascii=False, default=str)[:_KG_MAX_CHARS]
        return {"text": text, "hits": [], "count": 1, "query": None, "hyde": False}

    raise ValueError(f"strumento sconosciuto: {name!r}")


def _assistant_tool_message(content: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
    """Messaggio assistant con tool_calls nel formato atteso dall'API OpenAI."""
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(
                        call["arguments"] or {}, ensure_ascii=False
                    ),
                },
            }
            for call in tool_calls
        ],
    }


# --- loop ReAct -----------------------------------------------------------------


async def run_research(
    question: str,
    *,
    sub_questions: list[str] | None = None,
    kg_context: str = "",
    client: KimiClient,
    session: AsyncSession | None,
) -> dict[str, Any]:
    """Esegue il loop ReAct sul quesito e raccoglie i passaggi rilevanti.

    Restituisce ``{"results", "queries_run", "steps"}`` nello stesso formato
    prodotto dal nodo ``retrieve`` del grafo classico. Solleva
    :class:`ReActUnavailable` se il tool-calling non è utilizzabile (2 round
    consecutivi senza tool_call validi, o eccezione nella chiamata LLM): il
    grafo ripiega allora sul percorso classico.
    """
    settings = get_settings()
    user_parts = [f"QUESITO: {question}"]
    if sub_questions:
        user_parts.append(
            "SOTTO-QUESITI PIANIFICATI (coprili tutti con le ricerche):\n"
            + "\n".join(f"- {q}" for q in sub_questions)
        )
    if kg_context.strip():
        user_parts.append(
            "CONTESTO DAL KNOWLEDGE GRAPH (guida, non fonte citabile):\n"
            + kg_context
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": RESEARCH_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    results: dict[int, dict[str, Any]] = {}
    queries_run: list[str] = []
    steps: list[dict[str, Any]] = [
        _step("research", "avvio ricerca ReAct con tool-calling")
    ]
    malformed_rounds = 0

    for round_n in range(1, settings.react_max_rounds + 1):
        try:
            content, tool_calls = await client.chat_with_tools(messages, TOOLS_SPEC)
        except (LLMNotConfiguredError, LLMInsufficientBalanceError):
            raise
        except Exception as exc:
            raise ReActUnavailable(f"chiamata LLM con tools fallita: {exc}") from exc

        if not tool_calls:
            steps.append(
                _step(
                    "research",
                    f"raccolta conclusa dal modello al round {round_n}: "
                    f"{len(results)} passaggi unici",
                )
            )
            break

        # Il messaggio assistant con i tool_calls rientra nella conversazione,
        # seguito dagli esiti (uno per chiamata): è il formato del protocollo
        messages.append(_assistant_tool_message(content, tool_calls))
        valid_calls = 0
        for call in tool_calls:
            if call["name"] not in _TOOL_NAMES or call["arguments"] is None:
                log.warning(
                    "tool_call malformato dal modello",
                    nome=call["name"],
                    round=round_n,
                )
                steps.append(
                    _step(
                        "research_tool",
                        f"tool_call malformato: {call['name']!r} "
                        "(strumento sconosciuto o argomenti non validi)",
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            "Errore: strumento sconosciuto o argomenti non "
                            "validi. Usa solo gli strumenti dichiarati, con "
                            "argomenti JSON validi."
                        ),
                    }
                )
                continue
            valid_calls += 1
            try:
                outcome = await _exec_tool(
                    call, client=client, session=session, question=question
                )
            except Exception as exc:
                # Un singolo strumento fallito non ferma il loop: l'errore
                # rientra nei messaggi e il modello può correggere il tiro
                log.warning(
                    "strumento del research agent fallito",
                    nome=call["name"],
                    error=str(exc),
                )
                steps.append(
                    _step(
                        "research_tool",
                        f"{call['name']}: errore durante l'esecuzione ({exc})",
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": f"Errore durante l'esecuzione dello strumento: {exc}",
                    }
                )
                continue
            added = 0
            for hit in outcome["hits"]:
                chunk_id = int(hit["chunk_id"])
                current = results.get(chunk_id)
                if current is None or (hit.get("score") or 0.0) > (
                    current.get("score") or 0.0
                ):
                    results[chunk_id] = hit
                    added += 1
            if outcome["query"]:
                queries_run.append(outcome["query"])
            detail = (
                f"{call['name']}({json.dumps(call['arguments'], ensure_ascii=False)}) "
                f"→ {outcome['count']} risultati"
            )
            if outcome["hyde"]:
                detail += " (con HyDE)"
            if added and outcome["query"]:
                detail += f", {added} passaggi nuovi accumulati"
            steps.append(_step("research_tool", detail))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": outcome["text"],
                }
            )

        if valid_calls == 0:
            malformed_rounds += 1
            if malformed_rounds >= _MAX_MALFORMED_ROUNDS:
                raise ReActUnavailable(
                    f"tool_call malformati per {_MAX_MALFORMED_ROUNDS} "
                    "round consecutivi"
                )
        else:
            malformed_rounds = 0
    else:
        steps.append(
            _step(
                "research",
                f"massimo di {settings.react_max_rounds} round raggiunto: "
                f"si procede con i {len(results)} passaggi raccolti",
            )
        )

    return {"results": results, "queries_run": queries_run, "steps": steps}
