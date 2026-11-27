"""Grafo di stati LangGraph dell'agente documentale (Fase 4, architettura v2).

Topologia (tutti i nodi appendono uno ``step`` tracciato nello stato)::

    START → classify ─┬─ fast ────→ retrieve ─┬─ fast ────→ synthesize → verify → END
                      └─ agentic → plan → kg_lookup ─┬─ react → research ─┬─ ok ─┘
                                                     │                    └─ ko → retrieve
                                                     └─ graph → retrieve ─┤
                                             evaluate ←───────────────────┘
                                               └─ (lacune, iterazioni < max) → retrieve

    (con reflexion attiva, un verdict "fragile" in ``verify`` devia su
    ``refine`` → ``verify``, una sola volta, prima di END)

    GRADING CONDIZIONALE (anti-allucinazione): se il massimo score RERANKATO
    dei passaggi è sotto ``retrieval_strong_score``, anche i percorsi fast e
    ReAct passano da ``evaluate`` (grading CRAG) prima della sintesi; la
    sintesi stessa SI ASTIENE (risposta deterministica, nessuna chiamata LLM)
    se lo score massimo è sotto ``retrieval_min_score`` o se nessun passaggio
    menziona i termini distintivi del quesito.

- ``classify`` decide il percorso (fast = singola ricerca puntuale,
  agentic = scomposizione, valutazione di copertura e riformulazioni).
- ``research`` (solo percorso agentic, con ``agent_mode="react"`` — default)
  è il research agent ReAct di :mod:`app.agent.research`: il modello decide
  via tool-calling quali strumenti documentali invocare e il loop raccoglie
  i passaggi. Se il tool-calling fallisce (tool_call malformati ripetuti o
  eccezione) il grafo ripiega automaticamente sul ciclo classico
  ``retrieve``/``evaluate`` con un warning tracciato.
- ``retrieve`` applica HyDE (``hyde_enabled``): per ogni interrogazione il
  modello scrive prima un passaggio ipotetico di risposta e i risultati
  delle due ricerche ibride sono fusi per score massimo.
- ``evaluate`` applica il grading CRAG (``crag_enabled``): classifica ogni
  passaggio top come rilevante/parziale/irrilevante; gli irrilevanti sono
  scartati dalla selezione, i parziali tenuti con peso minore.
- ``verify`` applica la critic avversariale (``critic_enabled``): dopo la
  verifica di aderenza tenta di confutare la risposta e produce la
  ``confidence`` (alta/media/bassa) esposta nell'evento SSE ``answer``.
- ``refine`` applica la reflexion (``reflexion_enabled``, richiede la
  critic): se il verdict è ``fragile`` (confidence bassa) la risposta è
  RIGENERATA una sola volta usando le criticità come feedback esplicito,
  poi riverificata. Le risposte solide non pagano alcuna latenza extra.
- ``kg_lookup`` (solo percorso agentic, solo con ``kg_enabled`` e grafo
  popolato) riconosce nel quesito le entità del knowledge graph: le
  RELAZIONI trovate diventano ``kg_context`` per la sintesi (guida, non
  fonte citabile) e i DOCUMENTI collegati alimentano una ricerca vincolata
  in ``retrieve``. Tutto best-effort: grafo vuoto o errore → flusso
  identico a quello senza KG.
- ``evaluate``/``retrieve`` formano il loop di raffinamento, limitato da
  ``settings.max_agent_iterations``.
- ``synthesize`` genera la risposta con citazioni ``[n]``; ``verify``
  controlla l'aderenza fattuale e aggiunge warning.

STREAMING DEI TOKEN: il nodo ``synthesize`` emette i delta di testo man mano
che il modello li produce. Il consumatore (route SSE della Fase 5) passa un
``token_sink`` nel ``configurable`` del grafo::

    config = {"configurable": {"session": db_session, "token_sink": sink}}

``token_sink`` può essere una ``asyncio.Queue`` (i token sono messi con
``put``), un callable sincrono o un callable async. Lo stato conserva comunque
la risposta completa in ``answer``: lo streaming è un effetto collaterale, non
il canale dati. Anche la sessione DB viaggia nel ``configurable``, così lo
stato del grafo resta serializzabile.

STREAMING DEGLI STEP: :func:`run_agent` accetta anche un ``step_sink``
opzionale (stessa meccanica del ``token_sink``). Se presente, il grafo è
eseguito con ``stream_mode="values"`` e ogni nuovo ``step`` tracciato dai nodi
è consegnato al sink appena appare nello stato, così la route SSE può emettere
l'avanzamento in tempo reale. Senza ``step_sink`` l'esecuzione è identica a
prima (``ainvoke``).

CONTESTO CONVERSAZIONALE: :func:`run_agent` accetta una ``history`` opzionale
(gli ultimi messaggi della sessione, ``[{"role", "content"}]``). Se presente,
il nodo ``classify`` la usa per produrre una ``standalone_question``: il
quesito riformulato in forma autonoma (pronomi e riferimenti risolti, es. "e
nel 2024?" → "quali sono le condizioni di garanzia del contratto di
manutenzione nel 2024?"). Lo stato conserva sia il quesito originale sia
quello autonomo: plan/retrieve/evaluate/synthesize operano su quest'ultimo,
così ricerche e risposta non dipendono dal dialogo. La riformulazione è
tracciata nello step ``classify`` (e quindi nell'audit) e le ricerche
riformulate compaiono in ``queries_run``. Il nodo ``synthesize`` riceve anche
le ultime battute, solo per tono e continuità: la storia NON è una fonte di
fatti (regola ribadita nel prompt). Senza ``history`` il comportamento è
identico a prima.
"""

from __future__ import annotations

import asyncio
import inspect
import operator
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session_factory
from ..evaluation import ABSTENTION_PREFIX
from ..ingestion.chunker import count_tokens
from ..knowledge import query as kg_query
from ..llm import KimiClient, LLMNotConfiguredError, extract_json, get_kimi_client
from ..retrieval.hybrid import SearchResult
from . import research, tools
from .citations import (
    Citation,
    build_citation_map,
    parse_citations,
    strip_or_report_uncited,
)
from .prompts import (
    CLASSIFY_PROMPT,
    CRITIC_PROMPT,
    EVALUATE_PROMPT,
    PLAN_PROMPT,
    REFINE_PROMPT,
    SYNTHESIZE_PROMPT,
    VERIFY_PROMPT,
)

log = structlog.get_logger(__name__)

# Massimo numero di passaggi inviati al prompt di sintesi/valutazione
_MAX_PASSAGGI_SINTESI = 12
# Limite di passaggi dallo stesso documento nella selezione (diversità fonti)
_MAX_PER_DOCUMENTO = 3
# Lunghezza massima del contesto di un passaggio nei prompt JSON (evaluate)
_CONTEXT_MAX_CHARS = 1200
# Relazioni del knowledge graph incluse al massimo nel contesto per la sintesi
_MAX_KG_RELAZIONI_CONTESTO = 20
# Fattore di abbattimento dello score per i passaggi giudicati "parziali"
# dal grading CRAG (restano dopo i rilevanti nella selezione)
_CRAG_PARTIAL_WEIGHT = 0.5
# Verdict della critic avversariale → affidabilità esposta all'utente
_CONFIDENCE_DA_VERDICT = {"solido": "alta", "parziale": "media", "fragile": "bassa"}
# Ordinamento dei livelli di confidence (per il minimo composito)
_CONF_ORDER = {"alta": 2, "media": 1, "bassa": 0}


def _min_confidence(*levels: str) -> str:
    """Minimo tra livelli di confidence; i valori non validi/vuoti sono
    ignorati (tutti vuoti → stringa vuota, come con la critic spenta)."""
    valid = [level for level in levels if level in _CONF_ORDER]
    if not valid:
        return ""
    return min(valid, key=lambda level: _CONF_ORDER[level])

# Tipo del sink di streaming: Queue, callable sync o callable async
TokenSink = "asyncio.Queue[str] | Callable[[str], Any]"


class AgentState(TypedDict, total=False):
    """Stato condiviso del grafo agentico.

    Le chiavi con reducer (``queries_run``, ``results``, ``warnings``,
    ``steps``) si accumulano tra i nodi; le altre sono sovrascritte.
    """

    question: str
    standalone_question: str  # quesito riformulato autonomo (default: question)
    history: list[dict[str, str]]  # ultime battute della sessione (contesto)
    session_id: int | None
    user_id: int | None
    path: str  # "fast" | "agentic"
    sub_questions: list[dict[str, str]]
    pending_queries: list[str]  # interrogazioni da eseguire al prossimo retrieve
    kg_context: str  # relazioni del knowledge graph, per la sintesi (guida, non fonte)
    kg_document_ids: list[int]  # documenti collegati alle entità riconosciute
    react_ok: bool  # research agent ReAct riuscito (False = fallback al grafo)
    excluded_passages: list[int]  # chunk_id scartati dal grading CRAG (evaluate)
    abstained: bool  # True se la sintesi si è astenuta (gate di pertinenza)
    scores_reranked: bool  # True se gli score dei passaggi sono del reranker
    confidence: str  # "alta"|"media"|"bassa" dalla critic avversariale (verify)
    critic_issues: list[str]  # criticità della critic (feedback per refine)
    refined: bool  # True se la risposta è già stata rigenerata (reflexion)
    queries_run: Annotated[list[str], operator.add]
    results: Annotated[dict[int, dict[str, Any]], operator.or_]  # chunk_id → dict
    iterations: int  # valutazioni di copertura eseguite
    answer: str
    citations: list[dict[str, Any]]
    warnings: Annotated[list[str], operator.add]
    steps: Annotated[list[dict[str, Any]], operator.add]


class AgentResult(BaseModel):
    """Esito di una run dell'agente (persistibile in AuditLog, Fase 5)."""

    answer: str
    path: str = ""
    standalone_question: str = ""
    citations: list[Citation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    queries_run: list[str] = Field(default_factory=list)
    retrieved_chunk_ids: list[int] = Field(default_factory=list)
    # Affidabilità stimata dalla critic avversariale ("alta"|"media"|"bassa";
    # vuoto se la critic è disabilitata o non è stata eseguita)
    confidence: str = ""
    latency_ms: int = 0


# --- helper -----------------------------------------------------------------


def _step(step: str, detail: str) -> dict[str, Any]:
    """Voce di tracciamento di un nodo (evento ``step`` dell'API SSE)."""
    return {
        "step": step,
        "detail": detail,
        "ts": datetime.now(UTC).isoformat(),
    }


def _get_client() -> KimiClient:
    """Client Kimi (punto unico di accesso, così i test lo sostituiscono)."""
    return get_kimi_client()


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable") or {})


async def _emit(sink: Any, value: Any) -> None:
    """Consegna un valore (token o step) al sink: Queue, callable sync/async."""
    if sink is None:
        return
    if isinstance(sink, asyncio.Queue):
        await sink.put(value)
        return
    outcome = sink(value)
    if inspect.isawaitable(outcome):
        await outcome


async def _chat_json(client: KimiClient, system: str, user: str) -> dict[str, Any]:
    """Chat in JSON mode con parsing tollerante e UN retry di correzione.

    Se il modello produce JSON imperfetto, gli si rimanda l'output chiedendo
    di correggerlo; al secondo fallimento si solleva ``ValueError``.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    first = await client.chat(messages, json_mode=True)
    try:
        return extract_json(first)
    except ValueError:
        log.warning("JSON imperfetto dal modello: retry di correzione", prompt=system[:40])
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
    return extract_json(await client.chat(messages, json_mode=True))


def _to_search_result(data: dict[str, Any]) -> SearchResult:
    """Ricostruisce un :class:`SearchResult` dal dict serializzato nello stato."""
    modified = data.get("modified_at")
    return SearchResult(
        chunk_id=int(data["chunk_id"]),
        document_id=int(data.get("document_id") or 0),
        title=data.get("title") or "",
        snippet=data.get("snippet") or "",
        context_text=data.get("context_text") or "",
        score=float(data.get("score") or 0.0),
        page=data.get("page"),
        section_path=list(data.get("section_path") or []),
        source_name=data.get("source_name") or "",
        doc_type=data.get("doc_type") or "",
        bboxes=list(data.get("bboxes") or []),
        modified_at=datetime.fromisoformat(modified) if modified else None,
    )


def _dedup_key(data: dict[str, Any]) -> str:
    """Chiave di quasi-duplicazione di un passaggio: primi 200 caratteri del
    testo normalizzato (lowercase, spazi compattati). Intercetta lo stesso
    contenuto indicizzato in documenti diversi (copie, allegati ripetuti)."""
    text = data.get("context_text") or data.get("snippet") or ""
    return " ".join(str(text).lower().split())[:200]


def _select_passages(
    results: dict[int, dict[str, Any]],
    max_passages: int = _MAX_PASSAGGI_SINTESI,
    max_per_doc: int = _MAX_PER_DOCUMENTO,
    exclude: set[int] | None = None,
) -> list[SearchResult]:
    """Seleziona i passaggi per il prompt: migliori per score, con diversità
    di documento (al massimo ``max_per_doc`` dallo stesso documento) e senza
    quasi-duplicati (stesso testo normalizzato: resta la copia con score
    migliore, che arriva per prima nell'ordinamento).

    ``exclude`` (grading CRAG del nodo ``evaluate``) elenca i ``chunk_id``
    giudicati irrilevanti: sono saltati dalla selezione anche se presenti
    nei risultati accumulati.
    """
    skip = exclude or set()
    ordered = sorted(results.values(), key=lambda d: d.get("score") or 0.0, reverse=True)
    selected: list[SearchResult] = []
    per_doc: dict[int, int] = {}
    seen_texts: set[str] = set()
    for data in ordered:
        if int(data.get("chunk_id") or 0) in skip:
            continue
        doc_id = int(data.get("document_id") or 0)
        if per_doc.get(doc_id, 0) >= max_per_doc:
            continue
        key = _dedup_key(data)
        if key and key in seen_texts:
            continue
        seen_texts.add(key)
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        selected.append(_to_search_result(data))
        if len(selected) >= max_passages:
            break
    return selected


def _reranked_max_score(
    results: dict[int, dict[str, Any]], exclude: set[int] | None = None
) -> tuple[float, bool]:
    """``(max_score, scores_reranked)`` dei risultati non esclusi.

    ``scores_reranked`` è True solo se TUTTI gli score provengono dal
    cross-encoder di reranking (scala sigmoid [0,1]): con score RRF grezzi
    (~0.01-0.03) o misti le soglie di pertinenza non sono applicabili.
    """
    skip = exclude or set()
    hits = [
        d for d in results.values() if int(d.get("chunk_id") or 0) not in skip
    ]
    if not hits:
        return 0.0, False
    max_score = max(float(d.get("score") or 0.0) for d in hits)
    return max_score, all(bool(d.get("reranked")) for d in hits)


# Parole a inizio frase (dopo questi caratteri la maiuscola non è indicativa)
_SENTENCE_BREAK = ".!?:;\n"


def _distinctive_terms(question: str) -> set[str]:
    """Termini "distintivi" del quesito (lowercase), per il check deterministico.

    Sono distintivi: sequenze tra virgolette, token con cifre E lettere o
    maiuscole interne (codici/modelli come "Florx2r", sigle come "DURC"),
    parole Capitalizzate non a inizio frase (nomi propri). I numeri puri
    (es. anni) NON sono considerati: troppo soggetti a riformulazioni.
    """
    terms: set[str] = set()
    for match in re.finditer(r"[\"“«]([^\"”»]{2,60})[\"”»]", question):
        terms.add(match.group(1).strip().lower())
    for match in re.finditer(r"\w+(?:[/-]\w+)*", question):
        token = match.group(0)
        if len(token) < 3:
            continue
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        internal_upper = any(ch.isupper() for ch in token[1:])
        if (has_digit and has_alpha) or internal_upper:
            terms.add(token.lower())
            continue
        # Nome proprio: Capitalizzato, resto minuscolo, non a inizio frase
        start = match.start()
        prefix = question[:start].rstrip()
        at_sentence_start = not prefix or prefix[-1] in _SENTENCE_BREAK
        if (
            not at_sentence_start
            and token[0].isupper()
            and token[1:].islower()
            and has_alpha
            and not has_digit
        ):
            terms.add(token.lower())
    return terms


def _passages_mention_any(passages: list[SearchResult], terms: set[str]) -> bool:
    """True se ALMENO un passaggio contiene ALMENO un termine distintivo."""
    for passage in passages:
        haystack = f"{passage.title}\n{passage.context_text}\n{passage.snippet}".lower()
        if any(term in haystack for term in terms):
            return True
    return False


def _state_exclusions(state: AgentState) -> set[int]:
    """Chunk scartati dal grading CRAG (chiave di stato ``excluded_passages``)."""
    return {int(c) for c in state.get("excluded_passages", [])}


def _format_passage(n: int, r: SearchResult, max_chars: int | None = None) -> str:
    """Blocco testuale di un passaggio numerato per i prompt."""
    data = r.modified_at.date().isoformat() if r.modified_at else "s.d."
    pagina = f", pag. {r.page}" if r.page is not None else ""
    header = f"[{n}] «{r.title}» — fonte: {r.source_name or 'sconosciuta'}, data: {data}{pagina}"
    text = r.context_text or r.snippet
    if max_chars is not None:
        text = text[:max_chars]
    return f"{header}\n{text}"


def _format_history(history: list[dict[str, str]]) -> str:
    """Ultime battute della conversazione in forma testuale, per i prompt."""
    lines = []
    for msg in history:
        ruolo = "utente" if msg.get("role") == "user" else "assistente"
        lines.append(f"{ruolo}: {msg.get('content', '')}")
    return "\n".join(lines)


def _fit_citation_map_to_budget(
    citation_map: dict[int, SearchResult],
    *,
    question: str,
    extra: str,
    budget: int,
) -> dict[int, SearchResult]:
    """Taglia i passaggi meno rilevanti finché il contesto di sintesi rientra
    nel budget di token (stima cl100k, ``synthesize_max_context_tokens``).

    La mappa è ordinata per score decrescente: si elimina sempre l'ultimo
    passaggio (il meno rilevante) e ogni taglio è loggato. ``extra`` è la
    parte fissa del messaggio oltre all'elenco (contesto KG, storia). Almeno
    un passaggio è sempre conservato: il budget è una guardia best-effort,
    non un motivo per rinunciare a rispondere.
    """
    kept = dict(citation_map)
    while len(kept) > 1:
        elenco = "\n\n".join(_format_passage(n, r) for n, r in kept.items())
        user = f"QUESITO: {question}\n\nPASSAGGI:\n{elenco}" + extra
        if count_tokens(SYNTHESIZE_PROMPT) + count_tokens(user) <= budget:
            return kept
        dropped_n = max(kept)
        log.warning(
            "contesto di sintesi oltre il budget di token: passaggio escluso",
            budget=budget,
            passaggio=dropped_n,
            titolo=kept[dropped_n].title,
            passaggi_rimanenti=len(kept) - 1,
        )
        del kept[dropped_n]
    return kept


def _effective_question(state: AgentState) -> str:
    """Quesito su cui operano plan/retrieve/evaluate/synthesize: quello
    autonomo prodotto da ``classify`` (riferimenti risolti) o l'originale."""
    return state.get("standalone_question") or state["question"]


# --- nodi -------------------------------------------------------------------


async def classify(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Classifica il quesito (``fast``/``agentic``) e, se c'è storia
    conversazionale, lo riformula in forma autonoma (``standalone_question``)."""
    client = _get_client()
    history = state.get("history") or []
    user = state["question"]
    if history:
        user = (
            "CONVERSAZIONE PRECEDENTE:\n"
            + _format_history(history)
            + f"\n\nQUESITO ATTUALE: {state['question']}"
        )
    data: dict[str, Any] = {}
    try:
        data = await _chat_json(client, CLASSIFY_PROMPT, user)
        path = data.get("path") if data.get("path") in ("fast", "agentic") else "agentic"
        reason = str(data.get("reason") or "")
    except ValueError:
        # JSON irrecuperabile anche dopo il retry: percorso più sicuro
        path, reason = "agentic", "classificazione non riuscita, percorso prudente"
    standalone = str(data.get("standalone_question") or "").strip() or state["question"]
    detail = f"percorso={path} — {reason}"
    if standalone != state["question"]:
        # Trasparenza: la riformulazione finisce nello step (e nell'audit)
        detail += f" — quesito riformulato: «{standalone}»"
    return {
        "path": path,
        "standalone_question": standalone,
        "steps": [_step("classify", detail)],
    }


async def plan(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Scompone il quesito agentico in sotto-quesiti di ricerca (max 5)."""
    client = _get_client()
    question = _effective_question(state)
    sub_questions: list[dict[str, str]] = []
    try:
        data = await _chat_json(client, PLAN_PROMPT, question)
        seen_queries: set[str] = set()
        for item in (data.get("sub_questions") or [])[:5]:
            query = str(item.get("query") or "").strip()
            if not query:
                continue
            # Guardia lingua: una query non italiana è sostituita dal
            # quesito autonomo (poi deduplicata)
            query = research.ensure_italian_query(query, question)
            if query in seen_queries:
                continue
            seen_queries.add(query)
            sub_questions.append(
                {"query": query, "purpose": str(item.get("purpose") or "")}
            )
    except ValueError:
        log.warning("pianificazione non riuscita: si ripiega sul quesito originale")
    pending = [sq["query"] for sq in sub_questions] or [question]
    return {
        "sub_questions": sub_questions,
        "pending_queries": pending,
        "steps": [_step("plan", f"{len(pending)} sotto-quesiti di ricerca")],
    }


def _format_kg_context(subgraph: dict[str, Any]) -> str:
    """Relazioni del sottografo in righe testuali per il prompt di sintesi.

    Forma: ``- Soggetto —predicato→ Oggetto (da «Titolo documento»)``.
    """
    lines = []
    for relation in subgraph.get("relations", [])[:_MAX_KG_RELAZIONI_CONTESTO]:
        provenance = relation.get("document_title") or ""
        origine = f" (da «{provenance}»)" if provenance else ""
        lines.append(
            f"- {relation['subject']} —{relation['predicate']}→ "
            f"{relation['object']}{origine}"
        )
    return "\n".join(lines)


async def kg_lookup(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Interroga il knowledge graph sulle entità citate nel quesito.

    Solo percorso agentic e solo con ``kg_enabled`` (il fast path non passa
    di qui: nessuna latenza aggiunta). Le entità sono riconosciute con un
    match deterministico dei nomi del grafo sul quesito (forma autonoma):
    nessuna chiamata LLM dedicata. Le relazioni trovate diventano
    ``kg_context`` (guida per la sintesi, NON fonte citabile) e i documenti
    che menzionano le entità alimentano una ricerca vincolata in
    ``retrieve``. Best-effort: KG vuoto o errore → stato immutato.
    """
    settings = get_settings()
    if not settings.kg_enabled:
        return {}
    session = _configurable(config).get("session")
    if session is None:
        return {}
    question = _effective_question(state)
    try:
        entities = await kg_query.match_entities_in_text(question, session=session)
        if not entities:
            return {}
        names = [e["name"] for e in entities]
        subgraph = await kg_query.entity_subgraph(
            names, session=session, depth=settings.kg_agent_depth
        )
        documents = await kg_query.documents_for_entities(
            names, session=session, mode="any"
        )
    except Exception as exc:
        # Il grafo è un arricchimento: un suo fallimento non deve mai
        # interrompere il flusso agentico
        log.warning("kg_lookup non riuscito: si prosegue senza KG", error=str(exc))
        return {}

    kg_context = _format_kg_context(subgraph)
    doc_ids = [int(d["document_id"]) for d in documents]
    display = ", ".join(e["display_name"] or e["name"] for e in entities)
    detail = (
        f"{len(entities)} entità riconosciute ({display}): "
        f"{len(subgraph.get('relations', []))} relazioni, "
        f"{len(doc_ids)} documenti collegati"
    )
    return {
        "kg_context": kg_context,
        "kg_document_ids": doc_ids,
        "steps": [_step("kg_lookup", detail)],
    }


async def retrieve_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Esegue le ricerche pendenti e accumula i passaggi per ``chunk_id``.

    Le interrogazioni (pianificate da ``plan`` o riformulate da ``evaluate``)
    sono eseguite IN PARALLELO con un tetto di concorrenza
    (``retrieve_max_concurrency``, default 3); ciascuna usa una sessione DB
    propria. La fusione per ``chunk_id`` resta deterministica (ordine delle
    interrogazioni).

    Percorso fast: una sola ricerca con il quesito originale. Percorso
    agentico: le interrogazioni prodotte da ``plan`` o da ``evaluate``; se
    ``kg_lookup`` ha collegato documenti via knowledge graph, una ricerca
    vincolata (max ``kg_agent_max_extra_docs`` documenti non già coperti)
    tira dentro i loro passaggi migliori rispetto al quesito.

    HYDE (``hyde_enabled``): prima di ogni ricerca il modello scrive un
    breve passaggio IPOTETICO che risponderebbe al quesito; si eseguono due
    ricerche ibride (interrogazione + testo HyDE) e i risultati sono fusi
    tenendo per ogni chunk lo score massimo (vedi
    :func:`research.hyde_search`). Flag spento → singola ricerca, come prima.
    """
    session = _configurable(config).get("session")
    settings = get_settings()
    question = _effective_question(state)
    # Anche il percorso fast usa le eventuali riformulazioni pendenti: con il
    # grading condizionale può passare da evaluate e tornare qui con query
    # riformulate (al primo giro pending è vuota → quesito originale)
    queries = list(state.get("pending_queries") or [question])

    def _merge(hits: list[dict[str, Any]], into: dict[int, dict[str, Any]]) -> int:
        added = 0
        for hit in hits:
            chunk_id = int(hit["chunk_id"])
            current = state.get("results", {}).get(chunk_id) or into.get(chunk_id)
            if current is None or (hit.get("score") or 0.0) > (current.get("score") or 0.0):
                into[chunk_id] = hit
                added += 1
        return added

    client: KimiClient | None = _get_client() if settings.hyde_enabled else None
    semaphore = asyncio.Semaphore(max(1, settings.retrieve_max_concurrency))

    async def _run_query(query: str) -> tuple[list[dict[str, Any]], str]:
        """Una ricerca (con HyDE se attivo), in parallelo con le altre.

        Le interrogazioni pianificate corrono in concorrenza (tetto
        ``retrieve_max_concurrency``): una ``AsyncSession`` non è sicura in
        uso concorrente, quindi ogni task ne apre una propria quando il grafo
        ha il DB configurato. Restituisce ``(hits, testo HyDE)``.
        """
        async with semaphore:
            if session is None:
                if client is not None:
                    return await research.hyde_search(query, client=client, session=None)
                return await tools.hybrid_search(query, session=None), ""
            async with get_session_factory()() as query_session:
                if client is not None:
                    return await research.hyde_search(
                        query, client=client, session=query_session
                    )
                return await tools.hybrid_search(query, session=query_session), ""

    hyde_searches = 0
    new_results: dict[int, dict[str, Any]] = {}
    # Fusione in ordine deterministico (quello delle interrogazioni), dopo
    # il completamento di tutte le ricerche parallele
    for hits, hyde_text in await asyncio.gather(*(_run_query(q) for q in queries)):
        if hyde_text:
            hyde_searches += 1
        _merge(hits, new_results)

    # Espansione via knowledge graph: ricerca ibrida vincolata ai documenti
    # collegati alle entità riconosciute e non già coperti dalle ricerche
    # ordinarie (best-effort: un fallimento non invalida le ricerche fatte)
    covered = {
        int(d.get("document_id") or 0)
        for d in (*state.get("results", {}).values(), *new_results.values())
    }
    extra_doc_ids = [
        doc_id
        for doc_id in state.get("kg_document_ids", [])
        if doc_id not in covered
    ][: get_settings().kg_agent_max_extra_docs]
    kg_hits = 0
    run_queries = list(queries)
    if extra_doc_ids:
        try:
            hits = await tools.hybrid_search(
                question,
                filters={"document_ids": extra_doc_ids},
                session=session,
            )
            kg_hits = _merge(
                [
                    h
                    for h in hits
                    # difensivo: solo chunk dei documenti effettivamente vincolati
                    if int(h.get("document_id") or 0) in extra_doc_ids
                ],
                new_results,
            )
            run_queries.append(question)
        except Exception as exc:
            log.warning("ricerca vincolata ai documenti del KG non riuscita", error=str(exc))

    total = len(state.get("results", {})) + len(new_results)
    detail = f"{len(run_queries)} ricerche: {total} passaggi unici accumulati"
    if hyde_searches:
        detail += f" ({hyde_searches} con espansione HyDE)"
    if extra_doc_ids:
        detail += (
            f" (+{kg_hits} passaggi da {len(extra_doc_ids)} "
            "documenti collegati dal knowledge graph)"
        )
    return {
        "results": new_results,
        "queries_run": run_queries,
        "pending_queries": [],
        "steps": [_step("retrieve", detail)],
    }


async def evaluate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Valuta la copertura dei passaggi e propone riformulazioni (loop).

    GRADING CRAG (``crag_enabled``): oltre al giudizio complessivo, il modello
    classifica ogni passaggio mostrato come ``rilevante``/``parziale``/
    ``irrilevante``. Gli irrilevanti finiscono in ``excluded_passages``
    (saltati dalla selezione per la sintesi), i parziali restano con lo score
    abbattuto (``_CRAG_PARTIAL_WEIGHT``), quindi DOPO i rilevanti. Se il JSON
    di risposta non contiene ``grades`` (modello lacunoso), il nodo si
    comporta esattamente come prima.
    """
    client = _get_client()
    settings = get_settings()
    iterations = state.get("iterations", 0) + 1
    passages = _select_passages(
        state.get("results", {}), exclude=_state_exclusions(state)
    )
    elenco = "\n\n".join(
        _format_passage(n, r, max_chars=_CONTEXT_MAX_CHARS)
        for n, r in enumerate(passages, start=1)
    ) or "(nessun passaggio recuperato)"
    user = (
        f"QUESITO: {_effective_question(state)}\n\n"
        f"RICERCHE GIÀ ESEGUITE: {state.get('queries_run', [])}\n\n"
        f"PASSAGGI RECUPERATI:\n{elenco}"
    )
    pending: list[str] = []
    detail = "copertura sufficiente"
    # Grading CRAG: chiavi dello stato aggiornate solo se il modello ha
    # prodotto la classificazione (parsing tollerante)
    excluded: list[int] | None = None
    score_overrides: dict[int, dict[str, Any]] = {}
    try:
        data = await _chat_json(client, EVALUATE_PROMPT, user)
        grade_info = ""
        if settings.crag_enabled:
            excluded, score_overrides, grade_info = _apply_crag_grades(
                data, passages, state.get("results", {})
            )
        if data.get("sufficient"):
            detail = "copertura sufficiente"
        else:
            missing = str(data.get("missing") or "")
            already = set(state.get("queries_run", []))
            # Guardia lingua sulle riformulazioni: quelle non italiane sono
            # sostituite dal quesito autonomo (il filtro "già eseguite" le
            # scarta se la sostituzione produce un doppione)
            refined = [
                research.ensure_italian_query(q, _effective_question(state))
                for q in (str(x).strip() for x in (data.get("refined_queries") or []))
                if q
            ]
            pending = list(dict.fromkeys(q for q in refined if q not in already))[:3]
            detail = f"lacuna: {missing or 'non specificata'}"
            if not pending:
                detail += " (nessuna riformulazione utile: si procede)"
        if grade_info:
            detail += f" — {grade_info}"
    except ValueError:
        # Valutazione irrecuperabile: meglio sintetizzare ciò che si ha
        detail = "valutazione non riuscita: si procede con i passaggi disponibili"
        excluded = None
        score_overrides = {}
    outcome: dict[str, Any] = {
        "iterations": iterations,
        "pending_queries": pending,
        "steps": [_step("evaluate", f"iterazione {iterations}: {detail}")],
    }
    if excluded is not None:
        outcome["excluded_passages"] = excluded
        if score_overrides:
            # Il reducer di ``results`` è dict-or: gli override riscrivono lo
            # score dei passaggi parziali (restano, ma dopo i rilevanti)
            outcome["results"] = score_overrides
    return outcome


def _apply_crag_grades(
    data: dict[str, Any],
    passages: list[SearchResult],
    results: dict[int, dict[str, Any]],
) -> tuple[list[int] | None, dict[int, dict[str, Any]], str]:
    """Applica il grading CRAG della risposta di ``evaluate``.

    Restituisce ``(excluded, score_overrides, detail)``: ``excluded`` è None
    se il JSON non contiene ``grades`` (comportamento classico); altrimenti
    contiene i chunk_id irrilevanti da scartare, mentre ``score_overrides``
    contiene le copie dei passaggi parziali con score abbattuto.
    """
    raw_grades = data.get("grades")
    if not isinstance(raw_grades, list):
        return None, {}, ""
    grade_by_n: dict[int, str] = {}
    for item in raw_grades:
        if not isinstance(item, dict):
            continue
        raw_n = item.get("n")
        if raw_n is None:
            continue
        try:
            grade_by_n[int(raw_n)] = str(item.get("grade") or "")
        except (TypeError, ValueError):
            continue
    excluded: list[int] = []
    overrides: dict[int, dict[str, Any]] = {}
    counts = {"rilevante": 0, "parziale": 0, "irrilevante": 0}
    for n, passage in enumerate(passages, start=1):
        grade = grade_by_n.get(n, "")
        if grade not in counts:
            continue
        counts[grade] += 1
        if grade == "irrilevante":
            excluded.append(passage.chunk_id)
        elif grade == "parziale":
            original = results.get(passage.chunk_id)
            if original is not None:
                downgraded = dict(original)
                downgraded["score"] = (
                    float(original.get("score") or 0.0) * _CRAG_PARTIAL_WEIGHT
                )
                overrides[passage.chunk_id] = downgraded
    detail = (
        f"grading: {counts['rilevante']} rilevanti, {counts['parziale']} "
        f"parziali, {counts['irrilevante']} irrilevanti"
    )
    return excluded, overrides, detail


def _abstain_answer(question: str, passages: list[SearchResult]) -> str:
    """Risposta di astensione deterministica (nessuna chiamata LLM).

    Usa la frase standard :data:`ABSTENTION_PREFIX` (riconosciuta dalla
    batteria di valutazione) e, se ci sono risultati "vicini" ma non
    pertinenti, ne elenca i titoli come suggerimento.
    """
    answer = f"{ABSTENTION_PREFIX} a: «{question}»."
    titles: list[str] = []
    for passage in passages:
        if passage.title and passage.title not in titles:
            titles.append(passage.title)
        if len(titles) >= 3:
            break
    if titles:
        answer += (
            " I documenti più vicini trovati (forse cercavi questi) sono: "
            + "; ".join(f"«{t}»" for t in titles)
            + "."
        )
    return answer


async def synthesize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Genera la risposta con citazioni ``[n]``, in streaming sul token_sink.

    BARRIERA ANTI-ALLUCINAZIONE (prima della chiamata LLM):

    - se il MASSIMO score dei passaggi selezionati è sotto
      ``retrieval_min_score`` (solo con score del reranker: gli score RRF
      grezzi vivono su un'altra scala e il gate è saltato con warning), la
      risposta è un'ASTENSIONE deterministica;
    - check dei TERMINI DISTINTIVI del quesito (codici/modelli, nomi propri,
      sequenze quotate): se nessun passaggio ne menziona nemmeno uno, i
      risultati parlano d'altro → astensione, a costo zero.

    L'astensione ha confidence "alta" (è una risposta certa), niente
    citazioni e uno step SSE dedicato (``abstain``, formato step standard).
    """
    cfg = _configurable(config)
    sink = cfg.get("token_sink")
    settings = get_settings()
    passages = _select_passages(
        state.get("results", {}), exclude=_state_exclusions(state)
    )

    if not passages:
        answer = (
            "Non ho trovato passaggi pertinenti nei documenti indicizzati per "
            "rispondere al quesito. Ho eseguito le ricerche: "
            + "; ".join(state.get("queries_run", []))
            + ". Prova a riformulare la domanda o ad arricchire l'archivio."
        )
        await _emit(sink, answer)
        return {
            "answer": answer,
            "citations": [],
            "warnings": ["nessun passaggio recuperato dalle ricerche"],
            "abstained": True,
            "confidence": "alta",  # l'assenza di fonti è un esito certo
            "steps": [_step("synthesize", "nessun passaggio: risposta di cortesia")],
        }

    question = _effective_question(state)
    max_score, scores_reranked = _reranked_max_score(
        state.get("results", {}), _state_exclusions(state)
    )
    if settings.agent_rerank_enabled and not scores_reranked:
        # Retrieve degradato a score RRF (reranker non disponibile): la
        # soglia non è applicabile su quella scala — gate saltato
        log.warning(
            "score non rerankati (RRF o misti): gate di pertinenza saltato",
            max_score=round(max_score, 4),
        )
    abstain_reason = ""
    if scores_reranked and max_score < settings.retrieval_min_score:
        abstain_reason = (
            f"pertinenza massima {max_score:.2f} sotto la soglia "
            f"{settings.retrieval_min_score:.2f}"
        )
    else:
        terms = _distinctive_terms(question)
        if terms and not _passages_mention_any(passages, terms):
            abstain_reason = (
                "nessun passaggio menziona i termini distintivi del quesito ("
                + ", ".join(sorted(terms)[:5])
                + ")"
            )
    if abstain_reason:
        answer = _abstain_answer(question, passages)
        await _emit(sink, answer)
        return {
            "answer": answer,
            "citations": [],
            "warnings": [f"astensione: {abstain_reason}"],
            "abstained": True,
            "scores_reranked": scores_reranked,
            "confidence": "alta",  # l'astensione è una risposta certa
            "steps": [_step("abstain", abstain_reason)],
        }
    # Parte fissa del messaggio utente oltre all'elenco dei passaggi:
    # - relazioni del knowledge graph: guida su dove guardare, MAI fonte
    #   citabile (regola ribadita nel SYNTHESIZE_PROMPT)
    # - storia leggera: solo per tono e continuità, NON una fonte di fatti
    #   (regola ribadita nel SYNTHESIZE_PROMPT)
    extra = ""
    kg_context = (state.get("kg_context") or "").strip()
    if kg_context:
        extra += (
            "\n\nCONTESTO DAL KNOWLEDGE GRAPH (relazioni verificate dai "
            "documenti indicizzati):\n" + kg_context
        )
    history = state.get("history") or []
    if history:
        extra += (
            "\n\nCONVERSAZIONE PRECEDENTE (solo per tono e continuità, "
            "NON una fonte di fatti):\n" + _format_history(history)
        )
    # Guardia budget: oltre synthesize_max_context_tokens i passaggi meno
    # rilevanti sono tagliati PRIMA di assegnare i numeri di citazione
    citation_map = _fit_citation_map_to_budget(
        build_citation_map(passages),
        question=question,
        extra=extra,
        budget=get_settings().synthesize_max_context_tokens,
    )
    elenco = "\n\n".join(
        _format_passage(n, r) for n, r in citation_map.items()
    )
    user = f"QUESITO: {question}\n\nPASSAGGI:\n{elenco}" + extra
    messages = [
        {"role": "system", "content": SYNTHESIZE_PROMPT},
        {"role": "user", "content": user},
    ]

    parts: list[str] = []
    client = _get_client()
    stream: AsyncIterator[str] = client.chat_stream(messages)
    async for token in stream:
        parts.append(token)
        await _emit(sink, token)
    answer = "".join(parts)

    parsed = parse_citations(answer, citation_map)
    warnings = strip_or_report_uncited(answer, citation_map)
    if parsed.invalid_markers:
        warnings.append(
            "marcatori di citazione non risolvibili nella risposta: "
            + ", ".join(f"[{n}]" for n in parsed.invalid_markers)
        )
    return {
        "answer": answer,
        "citations": [c.model_dump() for c in parsed.citations],
        "warnings": warnings,
        "abstained": False,
        "scores_reranked": scores_reranked,
        "steps": [
            _step(
                "synthesize",
                f"risposta generata ({len(answer)} caratteri, "
                f"{len(parsed.citations)} citazioni)",
            )
        ],
    }


async def verify(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Verifica l'aderenza fattuale della risposta ai passaggi citati.

    CRITIC AVVERSARIALE (``critic_enabled``): dopo la verifica di aderenza, un
    secondo prompt cerca attivamente di CONFUTARE ogni affermazione principale
    usando solo i passaggi citati. Le criticità trovate diventano warning.

    CONFIDENCE COMPOSITA: la confidence finale è il MINIMO tra il verdict
    della critic e i segnali duri — "bassa" se l'aderenza è fallita
    (``supported=false``, che innesca anche il refine) o se il massimo score
    rerankato è sotto ``retrieval_min_score``; "media" se è sotto
    ``retrieval_strong_score``. Il badge non può più dire "media" su una
    risposta scollegata dalle fonti.
    """
    if state.get("abstained"):
        # Astensione deterministica: niente affermazioni da verificare
        return {
            "steps": [_step("verify", "astensione: verifica non necessaria")]
        }
    passages = _select_passages(
        state.get("results", {}), exclude=_state_exclusions(state)
    )
    citation_map = build_citation_map(passages)
    parsed = parse_citations(state.get("answer", ""), citation_map)
    cited = {n: citation_map[n] for n in (c.n for c in parsed.citations) if n in citation_map}
    if not cited:
        # Risposta articolata senza marcatori [n] pur avendo passaggi
        # disponibili: è da considerare fragile — la reflexion la rigenera
        # con le citazioni (la criticità esplicita diventa feedback di refine)
        if citation_map:
            return {
                "warnings": [
                    "risposta priva di marcatori di citazione [n]: "
                    "rigenerata con citazioni esplicite"
                ],
                "confidence": "bassa",
                "critic_issues": [
                    "La risposta non contiene alcun marcatore di citazione [n]: "
                    "riscrivila facendo terminare OGNI affermazione fattuale con "
                    "il marcatore del passaggio che la sostiene "
                    '(es. "... entro 30 giorni [2].").'
                ],
                "steps": [
                    _step(
                        "verify",
                        "nessuna citazione: risposta fragile, rigenerazione richiesta",
                    )
                ],
            }
        return {
            "steps": [
                _step("verify", "nessuna citazione da verificare: controllo saltato")
            ]
        }

    elenco = "\n\n".join(_format_passage(n, r) for n, r in cited.items())
    user = f"RISPOSTA:\n{state['answer']}\n\nPASSAGGI CITATI:\n{elenco}"
    warnings: list[str] = []
    detail = "aderenza confermata"
    client = _get_client()
    supported = True
    claims: list[str] = []
    try:
        data = await _chat_json(client, VERIFY_PROMPT, user)
        if not data.get("supported"):
            supported = False
            claims = [str(c) for c in (data.get("unsupported_claims") or [])]
            detail = f"{len(claims)} affermazioni non supportate"
            warnings.append(
                "affermazioni non pienamente supportate dai passaggi citati: "
                + ("; ".join(claims) if claims else "vedi risposta")
            )
    except LLMNotConfiguredError:
        raise
    except Exception as exc:
        log.warning("verifica di aderenza non riuscita", error=str(exc))
        detail = "verifica non riuscita"
        warnings.append("verifica di aderenza non eseguita: controllare le fonti a mano")

    critic_confidence = ""
    verdict = ""
    critic_issues: list[str] = []
    if get_settings().critic_enabled:
        try:
            cdata = await _chat_json(client, CRITIC_PROMPT, user)
            verdict = str(cdata.get("verdict") or "")
            critic_confidence = _CONFIDENCE_DA_VERDICT.get(verdict, "")
            issues = [str(i) for i in (cdata.get("issues") or []) if str(i).strip()]
            for issue in issues:
                warnings.append(f"valutazione critica: {issue}")
            # Le criticità restano nello stato: se il verdict è "fragile" il
            # nodo ``refine`` le usa come feedback per rigenerare la risposta
            critic_issues = issues
            detail += (
                f" — critic: {verdict or 'verdict assente'}"
                + (f", {len(issues)} criticità" if issues else "")
            )
        except LLMNotConfiguredError:
            raise
        except Exception as exc:
            # La critic è un controllo aggiuntivo: un suo fallimento non
            # invalida la verifica di aderenza già fatta
            log.warning("valutazione critica non riuscita", error=str(exc))
            detail += " — critic non riuscita"
            warnings.append(
                "valutazione critica della risposta non eseguita: "
                "controllare le fonti a mano"
            )

    # CONFIDENCE COMPOSITA: minimo tra verdict della critic e segnali duri
    components = [critic_confidence]
    if not supported:
        # Aderenza fallita: risposta fragile per definizione (innesca refine)
        components.append("bassa")
        # Le affermazioni contestate diventano feedback per il refine anche
        # quando la critic è spenta o non le ha rilevate
        critic_issues = critic_issues + [
            f"affermazione non supportata dai passaggi: {claim}"
            for claim in claims
        ]
    if verdict == "parziale" and claims:
        # Critic tiepida MA aderenza con rilievi: rigenerare comunque
        components.append("bassa")
    max_score, scores_reranked = _reranked_max_score(
        state.get("results", {}), _state_exclusions(state)
    )
    if scores_reranked:
        settings = get_settings()
        if max_score < settings.retrieval_min_score:
            components.append("bassa")
        elif max_score < settings.retrieval_strong_score:
            components.append("media")
    confidence = _min_confidence(*components)
    return {
        "warnings": warnings,
        "confidence": confidence,
        "critic_issues": critic_issues,
        "steps": [_step("verify", detail)],
    }


async def refine(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Rigenera la risposta usando le criticità della critic come feedback.

    REFLEXION (``reflexion_enabled``, richiede ``critic_enabled``): il grafo
    ci arriva solo se la critic ha giudicato la risposta "fragile"
    (``confidence == "bassa"``) e non c'è già stata una rigenerazione. La
    nuova risposta è prodotta con il ``REFINE_PROMPT`` (bozza + criticità +
    passaggi) e riversata in streaming sul ``token_sink``: il consumatore SSE
    emette un evento ``reset`` alla comparsa dello step ``refine``, così il
    client sostituisce la bozza con la versione corretta. Il grafo torna poi
    a ``verify`` per la riverifica (una sola volta: ``refined`` evita il
    loop).
    """
    cfg = _configurable(config)
    sink = cfg.get("token_sink")
    passages = _select_passages(
        state.get("results", {}), exclude=_state_exclusions(state)
    )
    question = _effective_question(state)
    citation_map = build_citation_map(passages)
    elenco = "\n\n".join(_format_passage(n, r) for n, r in citation_map.items())
    issues = state.get("critic_issues", [])
    user = (
        f"QUESITO: {question}\n\n"
        f"RISPOSTA BOZZA (giudicata fragile):\n{state.get('answer', '')}\n\n"
        "CRITICITÀ DA CORREGGERE:\n"
        + ("\n".join(f"- {issue}" for issue in issues) or "- (non specificate)")
        + f"\n\nPASSAGGI:\n{elenco}"
    )
    messages = [
        {"role": "system", "content": REFINE_PROMPT},
        {"role": "user", "content": user},
    ]
    parts: list[str] = []
    client = _get_client()
    async for token in client.chat_stream(messages):
        parts.append(token)
        await _emit(sink, token)
    answer = "".join(parts)

    parsed = parse_citations(answer, citation_map)
    warnings = strip_or_report_uncited(answer, citation_map)
    if parsed.invalid_markers:
        warnings.append(
            "marcatori di citazione non risolvibili nella risposta corretta: "
            + ", ".join(f"[{n}]" for n in parsed.invalid_markers)
        )
    return {
        "answer": answer,
        "citations": [c.model_dump() for c in parsed.citations],
        "warnings": warnings,
        "refined": True,
        "steps": [
            _step(
                "refine",
                "risposta rigenerata dopo valutazione critica fragile "
                f"({len(issues)} criticità affrontate)",
            )
        ],
    }


async def research_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Research agent ReAct (``agent_mode="react"``): sostituisce il loop
    ``retrieve``/``evaluate`` con un ciclo di tool-calling guidato dal modello.

    Plan resta a monte (i sotto-quesiti guidano le ricerche del loop). Se il
    tool-calling non è utilizzabile (:class:`research.ReActUnavailable`), il
    nodo imposta ``react_ok=False`` e il grafo ripiega sul percorso classico
    (``retrieve`` + ``evaluate``), con warning tracciato.
    """
    cfg = _configurable(config)
    try:
        outcome = await research.run_research(
            _effective_question(state),
            sub_questions=[sq["query"] for sq in state.get("sub_questions", [])],
            kg_context=state.get("kg_context", ""),
            client=_get_client(),
            session=cfg.get("session"),
        )
    except LLMNotConfiguredError:
        raise
    except research.ReActUnavailable as exc:
        log.warning("research agent ReAct non riuscito: fallback al grafo", error=str(exc))
        return {
            "react_ok": False,
            "warnings": [
                "research agent ReAct non riuscito: ripiegato sul ciclo "
                "classico di ricerca e valutazione"
            ],
            "steps": [
                _step("research", f"fallback al grafo classico: {exc}"),
            ],
        }
    return {
        "react_ok": True,
        "results": outcome["results"],
        "queries_run": outcome["queries_run"],
        "steps": outcome["steps"],
    }


# --- costruzione del grafo ----------------------------------------------------


def _route_after_classify(state: AgentState) -> str:
    return "plan" if state["path"] == "agentic" else "retrieve"


def _route_after_kg(state: AgentState) -> str:
    """Sul percorso agentic: research agent ReAct (default) o retrieve classico."""
    if get_settings().agent_mode == "react":
        return "research"
    return "retrieve"


def _needs_grading(state: AgentState) -> bool:
    """True se i passaggi meritano il grading di rilevanza prima della sintesi.

    Con score del reranker sotto ``retrieval_strong_score`` l'evidenza non è
    "forte": il nodo ``evaluate`` (grading CRAG) filtra gli irrilevanti su
    TUTTI i percorsi, fast e ReAct inclusi. Sopra soglia (o con score RRF
    non confrontabili) si va diretti: latenza invariata sui quesiti facili.
    """
    max_score, scores_reranked = _reranked_max_score(
        state.get("results", {}), _state_exclusions(state)
    )
    return scores_reranked and max_score < get_settings().retrieval_strong_score


def _route_after_research(state: AgentState) -> str:
    """ReAct riuscito → sintesi (previo grading se l'evidenza è debole);
    fallito → ciclo classico retrieve/evaluate."""
    if not state.get("react_ok"):
        return "retrieve"
    if _needs_grading(state):
        return "evaluate"
    return "synthesize"


def _route_after_retrieve(state: AgentState) -> str:
    """Agentic → evaluate (come sempre); fast → evaluate SOLO se l'evidenza
    rerankata è sotto ``retrieval_strong_score`` (grading condizionale)."""
    if state["path"] == "agentic":
        return "evaluate"
    if _needs_grading(state):
        return "evaluate"
    return "synthesize"


def _route_after_evaluate(state: AgentState) -> str:
    """Itera finché ci sono riformulazioni e non si è raggiunto il massimo."""
    if (
        state.get("pending_queries")
        and state.get("iterations", 0) < get_settings().max_agent_iterations
    ):
        return "retrieve"
    return "synthesize"


def _route_after_verify(state: AgentState) -> str:
    """Reflexion: risposta "fragile" → rigenerazione una tantum (refine).

    La confidence "bassa" può ora arrivare anche dall'aderenza fallita
    (``supported=false``) o dalle soglie di pertinenza, non solo dalla
    critic: il refine scatta in tutti i casi (una sola volta), purché la
    reflexion sia attiva. Le astensioni non si rigenerano mai.
    """
    settings = get_settings()
    if (
        settings.reflexion_enabled
        and state.get("confidence") == "bassa"
        and not state.get("refined")
        and not state.get("abstained")
    ):
        return "refine"
    return END


def build_graph() -> Any:
    """Costruisce e compila il grafo di stati dell'agente.

    Con ``agent_mode="react"`` (default) il percorso agentic passa per il
    nodo ``research`` (loop ReAct con tool-calling) al posto del ciclo
    ``retrieve``/``evaluate``, che resta come fallback automatico.
    """
    graph = StateGraph(AgentState)
    graph.add_node("classify", classify)
    graph.add_node("plan", plan)
    graph.add_node("kg_lookup", kg_lookup)
    graph.add_node("research", research_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("evaluate", evaluate)
    graph.add_node("synthesize", synthesize)
    graph.add_node("verify", verify)
    graph.add_node("refine", refine)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify", _route_after_classify, {"plan": "plan", "retrieve": "retrieve"}
    )
    graph.add_edge("plan", "kg_lookup")
    graph.add_conditional_edges(
        "kg_lookup",
        _route_after_kg,
        {"research": "research", "retrieve": "retrieve"},
    )
    graph.add_conditional_edges(
        "research",
        _route_after_research,
        {"synthesize": "synthesize", "retrieve": "retrieve", "evaluate": "evaluate"},
    )
    graph.add_conditional_edges(
        "retrieve",
        _route_after_retrieve,
        {"evaluate": "evaluate", "synthesize": "synthesize"},
    )
    graph.add_conditional_edges(
        "evaluate",
        _route_after_evaluate,
        {"retrieve": "retrieve", "synthesize": "synthesize"},
    )
    graph.add_edge("synthesize", "verify")
    graph.add_conditional_edges(
        "verify",
        _route_after_verify,
        {"refine": "refine", END: END},
    )
    graph.add_edge("refine", "verify")
    return graph.compile()


# --- entry point --------------------------------------------------------------


async def run_agent(
    question: str,
    *,
    session: AsyncSession | None = None,
    session_id: int | None = None,
    user_id: int | None = None,
    history: list[dict[str, str]] | None = None,
    token_sink: Any = None,
    step_sink: Any = None,
) -> AgentResult:
    """Esegue il grafo agentico su un quesito e ne restituisce l'esito.

    ``history`` (opzionale) è la coda recente della sessione come lista di
    ``{"role": "user"|"assistant", "content": str}``: ``classify`` la usa per
    riformulare il quesito in forma autonoma e ``synthesize`` per la
    continuità del dialogo (mai come fonte di fatti). ``token_sink`` (Queue o
    callable sync/async) riceve i token della sintesi in streaming;
    ``step_sink`` (opzionale, stessa meccanica) riceve ogni step appena
    tracciato da un nodo (esecuzione con ``stream_mode="values"``).
    ``LLMNotConfiguredError`` non si propaga mai: produce una risposta di
    cortesia con warning.
    """
    started = time.perf_counter()
    initial: AgentState = {
        "question": question,
        "standalone_question": question,
        "history": history or [],
        "session_id": session_id,
        "user_id": user_id,
        "path": "",
        "sub_questions": [],
        "pending_queries": [],
        "kg_context": "",
        "kg_document_ids": [],
        "react_ok": False,
        "excluded_passages": [],
        "abstained": False,
        "scores_reranked": False,
        "confidence": "",
        "critic_issues": [],
        "refined": False,
        "queries_run": [],
        "results": {},
        "iterations": 0,
        "answer": "",
        "citations": [],
        "warnings": [],
        "steps": [],
    }
    config: RunnableConfig = {
        "configurable": {"session": session, "token_sink": token_sink}
    }
    graph = build_graph()
    try:
        if step_sink is None:
            final = await graph.ainvoke(initial, config=config)
        else:
            # Stato completo dopo ogni nodo: gli step nuovi (in coda a
            # ``steps``, lista solo accresciuta) sono emessi appena appaiono.
            final = {}
            emitted = 0
            async for state in graph.astream(
                initial, config=config, stream_mode="values"
            ):
                steps = state.get("steps", [])
                for step in steps[emitted:]:
                    await _emit(step_sink, step)
                emitted = len(steps)
                final = state
    except LLMNotConfiguredError:
        answer = (
            "Il servizio di generazione non è configurato: impostare "
            "MOONSHOT_API_KEY nell'ambiente o nel file .env e riprovare."
        )
        await _emit(token_sink, answer)
        log.warning("agente eseguito senza LLM configurato")
        return AgentResult(
            answer=answer,
            path="",
            warnings=["LLM non configurato: risposta di cortesia senza ricerca"],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    return AgentResult(
        answer=final.get("answer", ""),
        path=final.get("path", ""),
        standalone_question=final.get("standalone_question", ""),
        citations=[Citation.model_validate(c) for c in final.get("citations", [])],
        warnings=final.get("warnings", []),
        steps=final.get("steps", []),
        queries_run=final.get("queries_run", []),
        retrieved_chunk_ids=sorted(final.get("results", {}).keys()),
        confidence=final.get("confidence", ""),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
