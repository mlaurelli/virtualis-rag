"""Test dell'architettura agentica v2: research agent ReAct, HyDE, CRAG, critic.

Il KimiClient è sempre MOCKATO (nessuna rete): un fake emette tool_call
strutturati per il loop ReAct e risposte JSON in coda per i prompt classici
(marcatori: "classificatore", "pianificatore", "valutatore", "verificatore",
"critico", "scrittore" per HyDE). I flag v2, spenti di default dal conftest,
sono riattivati qui esplicitamente.
"""

from collections import deque
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import research, run_agent
from app.agent import tools as agent_tools
from app.config import get_settings

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _PLAN, _EVALUATE, _VERIFY, _CRITIC, _HYDE = (
    "classificatore",
    "pianificatore",
    "valutatore",
    "verificatore",
    "critico",
    "scrittore",
)


def _tc(call_id: str, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Un tool_call nel formato di app.llm.ToolCall."""
    return {"id": call_id, "name": name, "arguments": arguments}


class _FakeKimi:
    """KimiClient finto v2: round di tool-calling scriptati per il loop ReAct
    + risposte JSON in coda per i prompt classici (dispatch per marcatore).

    ``tool_rounds`` è la sceneggiatura del ReAct: ogni elemento è una tupla
    ``(content, tool_calls)`` consumata da una chiamata ``chat_with_tools``,
    oppure un'eccezione da sollevare. Esaurita la sceneggiatura il modello
    risponde senza tool_call (fine loop).
    """

    def __init__(
        self,
        responses: dict[str, list[str]] | None = None,
        stream_tokens: list[str] | None = None,
        tool_rounds: list[Any] | None = None,
    ) -> None:
        self._responses = {k: deque(v) for k, v in (responses or {}).items()}
        self._stream_tokens = stream_tokens or []
        self._tool_rounds = deque(tool_rounds or [])
        self.calls: list[str] = []
        self.round_messages: list[list[dict[str, Any]]] = []
        self.tool_schemas: Any = None

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                self.calls.append(marker)
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]  # l'ultima risposta si ripete
        raise AssertionError(f"prompt di sistema non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        for token in self._stream_tokens:
            yield token

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.tool_schemas = tools
        self.round_messages.append([dict(m) for m in messages])
        self.calls.append("react_round")
        if not self._tool_rounds:
            return ("Ho raccolto i passaggi necessari.", [])
        scripted = self._tool_rounds.popleft()
        if isinstance(scripted, Exception):
            raise scripted
        return scripted


class _FakeSearch:
    """Sostituto di tools.hybrid_search: stesse hit per ogni query, tiene
    traccia delle interrogazioni eseguite."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.queries.append(query)
        return [dict(h) for h in self._hits]


class _FakeSearchPerQuery:
    """Ricerca finta con hit diverse per interrogazione (test fusione HyDE)."""

    def __init__(self, mapping: dict[str, list[dict[str, Any]]]) -> None:
        self._mapping = mapping
        self.queries: list[str] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.queries.append(query)
        return [dict(h) for h in self._mapping.get(query, [])]


def _hit(chunk_id: int, document_id: int, score: float) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": f"Documento {document_id}",
        "snippet": f"snippet {chunk_id}",
        "context_text": f"contenuto esteso del passaggio {chunk_id}",
        "score": score,
        "page": 3,
        "section_path": ["Capitolo 1"],
        "source_name": "Archivio locale",
        "doc_type": "pdf",
        "modified_at": "2024-03-01T12:00:00",
    }


@pytest.fixture
def v2_env(monkeypatch):
    """Monta il fake LLM e la ricerca finta con i flag v2 configurati;
    restituisce una factory per configurarli test per test."""

    def install(
        fake_kimi: _FakeKimi,
        hits: list[dict[str, Any]],
        *,
        agent_mode: str = "react",
        hyde: bool = False,
        crag: bool = False,
        critic: bool = False,
        max_rounds: int | None = None,
    ) -> tuple[_FakeKimi, _FakeSearch]:
        monkeypatch.setenv("AGENT_MODE", agent_mode)
        monkeypatch.setenv("HYDE_ENABLED", "true" if hyde else "false")
        monkeypatch.setenv("CRAG_ENABLED", "true" if crag else "false")
        monkeypatch.setenv("CRITIC_ENABLED", "true" if critic else "false")
        if max_rounds is not None:
            monkeypatch.setenv("REACT_MAX_ROUNDS", str(max_rounds))
        get_settings.cache_clear()
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)
        search = _FakeSearch(hits)
        monkeypatch.setattr(agent_tools, "hybrid_search", search)
        return fake_kimi, search

    return install


# --- research agent ReAct ------------------------------------------------------


async def test_react_loop_completo_con_tool_call(v2_env) -> None:
    """Loop ReAct: il modello invoca hybrid_search due volte, gli esiti
    compatti rientrano nei messaggi, poi risponde senza tool_call → stop.
    I passaggi raccolti alimentano synthesize/verify invariati."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "confronto"}'],
            _PLAN: [
                '{"sub_questions": ['
                '{"query": "rimborso trasferte 2023", "purpose": "versione 2023"}'
                "]}"
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Confronto ", "completo [1]."],
        tool_rounds=[
            ("", [_tc("c1", "hybrid_search", {"query": "rimborso trasferte 2023"})]),
            ("", [_tc("c2", "hybrid_search", {"query": "rimborso trasferte 2024", "limit": 5})]),
        ],
    )
    _, search = v2_env(fake, [_hit(10, 7, 0.9), _hit(11, 8, 0.6)])
    result = await run_agent("confronta i rimborsi 2023 e 2024", session=None)

    assert result.path == "agentic"
    assert result.answer == "Confronto completo [1]."
    # le interrogazioni decise dal modello sono tracciate
    assert result.queries_run == ["rimborso trasferte 2023", "rimborso trasferte 2024"]
    assert search.queries == result.queries_run
    # passaggi accumulati nello stesso formato del nodo retrieve
    assert result.retrieved_chunk_ids == [10, 11]
    assert len(result.citations) == 1
    # sequenza dei nodi: research sostituisce retrieve/evaluate
    assert [s["step"] for s in result.steps] == [
        "classify",
        "plan",
        "research",
        "research_tool",
        "research_tool",
        "research",
        "synthesize",
        "verify",
    ]
    tool_steps = [s for s in result.steps if s["step"] == "research_tool"]
    assert "hybrid_search" in tool_steps[0]["detail"]
    assert "2 risultati" in tool_steps[0]["detail"]
    # il terzo round (senza tool_call) chiude il loop
    assert fake.calls.count("react_round") == 3
    # gli esiti compatti degli strumenti sono rientrati nei messaggi
    ultimo_round = fake.round_messages[-1]
    tool_msgs = [m for m in ultimo_round if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert "«Documento 7»" in tool_msgs[0]["content"]
    # gli schemi degli strumenti sono stati passati al modello
    nomi = {spec["function"]["name"] for spec in fake.tool_schemas}
    assert nomi == {
        "hybrid_search",
        "read_document",
        "list_documents",
        "cerca_entita",
        "contesto_entita",
    }


async def test_react_max_rounds_rispettato(v2_env) -> None:
    """Il modello chiede sempre strumenti: il loop si chiude a
    react_max_rounds e si procede con quanto raccolto."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composto"}'],
            _PLAN: ['{"sub_questions": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Parziale [1]."],
        tool_rounds=[
            ("", [_tc("c1", "hybrid_search", {"query": "prima"})]),
            ("", [_tc("c2", "hybrid_search", {"query": "seconda"})]),
        ],
    )
    v2_env(fake, [_hit(10, 7, 0.9)], max_rounds=2)
    result = await run_agent("quesito complesso", session=None)

    assert fake.calls.count("react_round") == 2  # mai un terzo round
    assert any(
        "massimo di 2 round raggiunto" in s["detail"] for s in result.steps
    )
    assert result.answer == "Parziale [1]."
    assert result.retrieved_chunk_ids == [10]


async def test_react_fallback_su_tool_call_malformati(v2_env) -> None:
    """Due round consecutivi senza tool_call validi (strumento sconosciuto,
    argomenti None) → fallback automatico al ciclo classico
    retrieve/evaluate, con warning tracciato."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composto"}'],
            _PLAN: ['{"sub_questions": []}'],
            _EVALUATE: ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Risposta dal grafo [1]."],
        tool_rounds=[
            ("", [_tc("c1", "vola_via", {"query": "x"})]),
            ("", [_tc("c2", "hybrid_search", None)]),  # argomenti malformati
        ],
    )
    _, search = v2_env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("quesito complesso", session=None)

    # fallback al grafo classico: retrieve + evaluate sono passati
    nomi_step = [s["step"] for s in result.steps]
    assert "retrieve" in nomi_step
    assert "evaluate" in nomi_step
    assert any(
        "fallback al grafo classico" in s["detail"]
        for s in result.steps
        if s["step"] == "research"
    )
    assert any("ripiegato sul ciclo classico" in w for w in result.warnings)
    # il percorso classico ha comunque risposto con i passaggi trovati
    assert result.answer == "Risposta dal grafo [1]."
    assert search.queries  # la ricerca classica è stata eseguita


async def test_react_fallback_su_eccezione_llm(v2_env) -> None:
    """Eccezione nella chiamata con tools → stesso fallback al grafo."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composto"}'],
            _PLAN: ['{"sub_questions": []}'],
            _EVALUATE: ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Risposta di riserva [1]."],
        tool_rounds=[ConnectionError("endpoint tools non disponibile")],
    )
    v2_env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("quesito complesso", session=None)

    assert any("ripiegato sul ciclo classico" in w for w in result.warnings)
    assert result.answer == "Risposta di riserva [1]."


async def test_react_errore_strumento_non_ferma_il_loop(v2_env) -> None:
    """Un tool_call verso uno strumento che fallisce (read_document senza
    sessione DB) produce un messaggio di errore e il loop prosegue."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composto"}'],
            _PLAN: ['{"sub_questions": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Ok [1]."],
        tool_rounds=[
            ("", [_tc("c1", "read_document", {"document_id": 99})]),
            ("", [_tc("c2", "hybrid_search", {"query": "rimborso"})]),
        ],
    )
    v2_env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("leggi il documento e cerca", session=None)

    assert any(
        "errore" in s["detail"]
        for s in result.steps
        if s["step"] == "research_tool"
    )
    assert result.answer == "Ok [1]."


async def test_agent_mode_graph_ignora_il_research(v2_env) -> None:
    """agent_mode=graph: il percorso agentic è quello classico, nessuna
    chiamata chat_with_tools."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composto"}'],
            _PLAN: ['{"sub_questions": []}'],
            _EVALUATE: ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Classico [1]."],
    )
    v2_env(fake, [_hit(10, 7, 0.9)], agent_mode="graph")
    result = await run_agent("quesito complesso", session=None)

    assert [s["step"] for s in result.steps] == [
        "classify",
        "plan",
        "retrieve",
        "evaluate",
        "synthesize",
        "verify",
    ]
    assert "react_round" not in fake.calls


# --- HyDE ----------------------------------------------------------------------


async def test_hyde_due_ricerche_fuse_per_score_massimo(monkeypatch) -> None:
    """hyde_search: due ricerche ibride (quesito + passaggio ipotetico) e
    fusione per score massimo per chunk."""
    monkeypatch.setenv("HYDE_ENABLED", "true")
    get_settings.cache_clear()
    fake = _FakeKimi(responses={_HYDE: ["passaggio ipotetico sui rimborsi"]})
    search = _FakeSearchPerQuery(
        {
            "come funziona il rimborso?": [_hit(1, 7, 0.5)],
            "passaggio ipotetico sui rimborsi": [_hit(2, 8, 0.9), _hit(1, 7, 0.7)],
        }
    )
    monkeypatch.setattr(agent_tools, "hybrid_search", search)

    hits, hyde_text = await research.hyde_search(
        "come funziona il rimborso?", client=fake, session=None
    )

    assert hyde_text == "passaggio ipotetico sui rimborsi"
    assert search.queries == [
        "come funziona il rimborso?",
        "passaggio ipotetico sui rimborsi",
    ]
    # fusione max-score: chunk 1 tiene lo score migliore tra le due ricerche
    assert [h["chunk_id"] for h in hits] == [2, 1]
    assert hits[1]["score"] == 0.7


async def test_hyde_disabilitato_singola_ricerca(monkeypatch) -> None:
    """Flag spento: una sola ricerca, nessuna chiamata LLM per HyDE."""
    monkeypatch.setenv("HYDE_ENABLED", "false")
    get_settings.cache_clear()
    fake = _FakeKimi(responses={_HYDE: ["non deve essere chiamato"]})
    search = _FakeSearchPerQuery({"q": [_hit(1, 7, 0.5)]})
    monkeypatch.setattr(agent_tools, "hybrid_search", search)

    hits, hyde_text = await research.hyde_search("q", client=fake, session=None)

    assert hyde_text == ""
    assert search.queries == ["q"]
    assert [h["chunk_id"] for h in hits] == [1]
    assert fake.calls == []  # il modello non è stato interpellato


async def test_hyde_nel_nodo_retrieve_del_grafo(v2_env) -> None:
    """Percorso fast con HyDE attivo: il nodo retrieve esegue le due ricerche
    e traccia l'espansione nello step."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _HYDE: ["estratto ipotetico sui rimborsi"],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Rimborso previsto [1]."],
    )
    _, search = v2_env(fake, [_hit(10, 7, 0.9)], hyde=True)
    result = await run_agent("come funziona il rimborso?", session=None)

    assert search.queries == [
        "come funziona il rimborso?",
        "estratto ipotetico sui rimborsi",
    ]
    retrieve_step = next(s for s in result.steps if s["step"] == "retrieve")
    assert "HyDE" in retrieve_step["detail"]
    assert result.answer == "Rimborso previsto [1]."


# --- CRAG (grading dei passaggi in evaluate) ------------------------------------


def _state_con_risultati() -> dict[str, Any]:
    """Stato minimo per invocare direttamente il nodo evaluate."""
    return {
        "question": "quali sono i termini del reso?",
        "standalone_question": "quali sono i termini del reso?",
        "path": "agentic",
        "results": {
            10: _hit(10, 7, 0.9),
            11: _hit(11, 8, 0.8),
            12: _hit(12, 9, 0.7),
        },
        "queries_run": ["termini reso"],
        "excluded_passages": [],
        "iterations": 0,
    }


async def test_crag_scarta_irrilevanti_e_penalizza_parziali(v2_env) -> None:
    """Grading CRAG: l'irrilevante finisce in excluded_passages, il parziale
    resta con score dimezzato (dopo il rilevante nella selezione)."""
    fake = _FakeKimi(
        responses={
            _EVALUATE: [
                '{"grades": ['
                '{"n": 1, "grade": "rilevante"}, '
                '{"n": 2, "grade": "parziale"}, '
                '{"n": 3, "grade": "irrilevante"}], '
                '"sufficient": true, "missing": "", "refined_queries": []}'
            ]
        }
    )
    v2_env(fake, [], crag=True)
    outcome = await agent_graph.evaluate(_state_con_risultati(), config={})

    # passaggi numerati per score: [1]=chunk10, [2]=chunk11, [3]=chunk12
    assert outcome["excluded_passages"] == [12]
    assert outcome["results"][11]["score"] == pytest.approx(0.4)
    assert "grading: 1 rilevanti, 1 parziali, 1 irrilevanti" in (
        outcome["steps"][0]["detail"]
    )
    # selezione per la sintesi: irrilevante scartato, parziale in coda
    merged = {**_state_con_risultati()["results"], **outcome["results"]}
    selezionati = agent_graph._select_passages(
        merged, exclude=set(outcome["excluded_passages"])
    )
    assert [r.chunk_id for r in selezionati] == [10, 11]


async def test_crag_json_senza_grades_compatibile(v2_env) -> None:
    """Risposta senza ``grades`` (modello lacunoso): comportamento identico
    al nodo evaluate classico."""
    fake = _FakeKimi(
        responses={
            _EVALUATE: [
                '{"sufficient": false, "missing": "manca la diaria", '
                '"refined_queries": ["diaria missioni estero"]}'
            ]
        }
    )
    v2_env(fake, [], crag=True)
    outcome = await agent_graph.evaluate(_state_con_risultati(), config={})

    assert "excluded_passages" not in outcome
    assert "results" not in outcome
    assert outcome["pending_queries"] == ["diaria missioni estero"]


# --- Critic avversariale (verify) ------------------------------------------------


def _state_con_risposta() -> dict[str, Any]:
    """Stato minimo per invocare direttamente il nodo verify."""
    return {
        "answer": "Il reso va richiesto entro 30 giorni [1].",
        "results": {10: _hit(10, 7, 0.9)},
        "excluded_passages": [],
    }


@pytest.mark.parametrize(
    ("verdict", "confidence"),
    [("solido", "alta"), ("parziale", "media"), ("fragile", "bassa")],
)
async def test_critic_verdict_mappa_su_confidence(
    v2_env, verdict: str, confidence: str
) -> None:
    """Il verdict della critic diventa la confidence della risposta."""
    fake = _FakeKimi(
        responses={
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: [f'{{"verdict": "{verdict}", "issues": []}}'],
        }
    )
    v2_env(fake, [], critic=True)
    outcome = await agent_graph.verify(_state_con_risposta(), config={})

    assert outcome["confidence"] == confidence
    assert f"critic: {verdict}" in outcome["steps"][0]["detail"]
    assert outcome["warnings"] == []


async def test_critic_issues_diventano_warning(v2_env) -> None:
    """Le criticità trovate dalla critic finiscono nei warning."""
    fake = _FakeKimi(
        responses={
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: [
                '{"verdict": "fragile", "issues": ["la cifra citata non '
                'compare nei passaggi"]}'
            ],
        }
    )
    v2_env(fake, [], critic=True)
    outcome = await agent_graph.verify(_state_con_risposta(), config={})

    assert outcome["confidence"] == "bassa"
    assert any("cifra citata" in w for w in outcome["warnings"])


async def test_critic_disabilitata_comportamento_attuale(v2_env) -> None:
    """Flag spento (default dei test): verify è quello classico, nessuna
    chiamata alla critic e confidence vuota."""
    fake = _FakeKimi(
        responses={
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: ["non deve essere chiamata"],
        }
    )
    v2_env(fake, [], critic=False)
    outcome = await agent_graph.verify(_state_con_risposta(), config={})

    assert outcome["confidence"] == ""
    assert _CRITIC not in fake.calls


async def test_confidence_nel_risultato_finale(v2_env) -> None:
    """La confidence arriva in AgentResult (e da lì nell'evento SSE answer)."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
            _CRITIC: ['{"verdict": "solido", "issues": []}'],
        },
        stream_tokens=["Il reso entro 30 giorni [1]."],
    )
    v2_env(fake, [_hit(10, 7, 0.9)], critic=True)
    result = await run_agent("termini per il reso?", session=None)

    assert result.confidence == "alta"
