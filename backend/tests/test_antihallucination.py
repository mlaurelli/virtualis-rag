"""Test del pacchetto anti-allucinazione.

Coperti: gate di astensione (soglia di pertinenza sugli score rerankati e
check deterministico dei termini distintivi), salto del gate con score RRF
non rerankati, grading di rilevanza condizionale sui percorsi fast/ReAct,
verify che innesca il refine su aderenza fallita, confidence composita,
guardia sulla lingua delle query generate, dedup dei passaggi quasi
identici e scoring dei quesiti ``expects_no_answer`` della batteria.

LLM e ricerca sono sempre finti (nessuna rete), come in test_agent.
"""

from collections import deque
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import research, run_agent
from app.agent import tools as agent_tools
from app.config import get_settings
from app.evaluation import ABSTENTION_PREFIX, is_abstention, score_answer

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _EVALUATE, _VERIFY, _CRITIC = (
    "classificatore",
    "valutatore",
    "verificatore",
    "critico",
)


class _FakeKimi:
    """KimiClient finto: risposte JSON in coda per marcatore + stream fissi."""

    def __init__(
        self,
        responses: dict[str, list[str]],
        stream_rounds: list[list[str]] | None = None,
    ) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_rounds = deque(stream_rounds or [[]])
        self.calls: list[str] = []

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                self.calls.append(marker)
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]
        raise AssertionError(f"prompt di sistema non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        self.calls.append("stream")
        if len(self._stream_rounds) > 1:
            tokens = self._stream_rounds.popleft()
        else:
            tokens = self._stream_rounds[0]
        for token in tokens:
            yield token


class _FakeSearch:
    """Sostituto di tools.hybrid_search con hit fisse."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.queries.append(query)
        return [dict(h) for h in self._hits]


def _hit(
    chunk_id: int,
    document_id: int,
    score: float,
    *,
    reranked: bool = False,
    context: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "title": title or f"Documento {document_id}",
        "snippet": f"snippet {chunk_id}",
        "context_text": context
        if context is not None
        else f"contenuto esteso del passaggio {chunk_id}",
        "score": score,
        "page": 3,
        "section_path": ["Capitolo 1"],
        "source_name": "Archivio locale",
        "doc_type": "pdf",
        "modified_at": "2024-03-01T12:00:00",
        "reranked": reranked,
    }


@pytest.fixture
def env(monkeypatch):
    """Fake LLM + ricerca finta sul grafo classico (flag del conftest)."""

    def install(fake: _FakeKimi, hits: list[dict[str, Any]]) -> _FakeSearch:
        search = _FakeSearch(hits)
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake)
        monkeypatch.setattr(agent_tools, "hybrid_search", search)
        return search

    return install


_SUFFICIENT = '{"sufficient": true, "missing": "", "refined_queries": []}'
_SUPPORTED = '{"supported": true, "unsupported_claims": []}'


# --- gate di astensione -----------------------------------------------------------


async def test_astensione_sotto_soglia_di_pertinenza(env) -> None:
    """Max score rerankato sotto retrieval_min_score → astensione
    deterministica: niente chiamata LLM di sintesi, confidence alta,
    step ``abstain``, titoli dei risultati vicini nella risposta."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _EVALUATE: [_SUFFICIENT],  # grading condizionale (score deboli)
        },
    )
    env(fake, [_hit(10, 7, 0.12, reranked=True, title="Manuale Siemens")])
    tokens: list[str] = []
    result = await run_agent(
        "come si configura il ricevitore?", session=None, token_sink=tokens.append
    )

    assert result.answer.startswith(ABSTENTION_PREFIX)
    assert "Manuale Siemens" in result.answer  # "forse cercavi"
    assert result.citations == []
    assert result.confidence == "alta"  # l'astensione è una risposta certa
    assert any(s["step"] == "abstain" for s in result.steps)
    assert any("astensione" in w for w in result.warnings)
    # nessuna sintesi LLM, nessuna verifica di aderenza
    assert "stream" not in fake.calls
    assert _VERIFY not in fake.calls
    # la risposta è comunque arrivata sullo stream SSE
    assert tokens == [result.answer]
    assert is_abstention(result.answer)


async def test_gate_saltato_con_score_rrf_non_rerankati(env) -> None:
    """Score RRF grezzi (~0.01, reranked=False): la soglia non è applicabile,
    il gate è saltato e la sintesi procede normalmente."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: [_SUPPORTED],
        },
        stream_rounds=[["Risposta normale [1]."]],
    )
    env(fake, [_hit(10, 7, 0.016)])
    result = await run_agent("come funziona il rimborso?", session=None)

    assert result.answer == "Risposta normale [1]."
    assert not any(s["step"] == "abstain" for s in result.steps)
    # niente grading condizionale: gli score non sono confrontabili
    assert _EVALUATE not in fake.calls


async def test_astensione_su_termini_distintivi_assenti(env) -> None:
    """Il quesito cita un modello ("Florx2r") che NESSUN passaggio menziona:
    astensione deterministica anche con score alti (il caso NICE/Siemens)."""
    fake = _FakeKimi(
        responses={_CLASSIFY: ['{"path": "fast", "reason": "puntuale"}']},
    )
    env(
        fake,
        [
            _hit(
                10,
                7,
                0.9,
                reranked=True,
                title="Manuale Siemens S7",
                context="istruzioni del plc siemens per la configurazione",
            )
        ],
    )
    result = await run_agent(
        "Come si programma il ricevitore NICE Florx2r?", session=None
    )

    assert result.answer.startswith(ABSTENTION_PREFIX)
    assert any(s["step"] == "abstain" for s in result.steps)
    assert any("termini distintivi" in w for w in result.warnings)
    assert "stream" not in fake.calls


async def test_termini_distintivi_presenti_sintesi_normale(env) -> None:
    """Se almeno un passaggio menziona il termine distintivo, nessun gate."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: [_SUPPORTED],
        },
        stream_rounds=[["Istruzioni del Florx2r [1]."]],
    )
    env(
        fake,
        [
            _hit(
                10,
                7,
                0.9,
                reranked=True,
                context="manuale del ricevitore nice florx2r: programmazione",
            )
        ],
    )
    result = await run_agent(
        "Come si programma il ricevitore NICE Florx2r?", session=None
    )
    assert result.answer == "Istruzioni del Florx2r [1]."
    assert not any(s["step"] == "abstain" for s in result.steps)


def test_distinctive_terms_estrazione() -> None:
    """Codici/modelli, sigle, nomi propri non a inizio frase, testo quotato;
    esclusi numeri puri e parole a inizio frase."""
    terms = agent_graph._distinctive_terms(
        'Come si programma il ricevitore NICE Florx2r di "Aula Magna" nel 2024?'
    )
    assert {"nice", "florx2r", "aula magna"} <= terms
    assert "2024" not in terms  # numeri puri esclusi
    assert "come" not in terms  # maiuscola a inizio frase esclusa
    # nome proprio a metà frase; "Come" iniziale escluso; anno puro escluso
    assert agent_graph._distinctive_terms("Come opera Rossi nel 2024?") == {"rossi"}
    # nessun termine distintivo: il check non si applica
    assert agent_graph._distinctive_terms("come funziona il rimborso spese?") == set()


# --- grading condizionale sui percorsi --------------------------------------------


async def test_fast_path_con_evidenza_debole_passa_dal_grading(env) -> None:
    """Score rerankati sotto retrieval_strong_score: anche il fast path passa
    da evaluate prima della sintesi."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _EVALUATE: [_SUFFICIENT],
            _VERIFY: [_SUPPORTED],
        },
        stream_rounds=[["Risposta con fonti deboli [1]."]],
    )
    env(fake, [_hit(10, 7, 0.45, reranked=True)])
    result = await run_agent("come funziona il rimborso?", session=None)

    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "evaluate",
        "synthesize",
        "verify",
    ]
    assert _EVALUATE in fake.calls


async def test_fast_path_con_evidenza_forte_niente_grading(env) -> None:
    """Score rerankati sopra la soglia strong: fast diretto (latenza invariata)."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: [_SUPPORTED],
        },
        stream_rounds=[["Risposta solida [1]."]],
    )
    env(fake, [_hit(10, 7, 0.92, reranked=True)])
    result = await run_agent("come funziona il rimborso?", session=None)

    assert [s["step"] for s in result.steps] == [
        "classify",
        "retrieve",
        "synthesize",
        "verify",
    ]
    assert _EVALUATE not in fake.calls


async def test_react_con_evidenza_debole_passa_dal_grading(env, monkeypatch) -> None:
    """Anche il percorso ReAct passa da evaluate se l'evidenza è debole."""
    monkeypatch.setenv("AGENT_MODE", "react")
    get_settings.cache_clear()

    async def fake_research(question, **kwargs):
        return {
            "results": {10: _hit(10, 7, 0.4, reranked=True)},
            "queries_run": ["rimborso trasferte"],
            "steps": [{"step": "research", "detail": "fatto", "ts": ""}],
        }

    monkeypatch.setattr(research, "run_research", fake_research)
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "agentic", "reason": "composito"}'],
            "pianificatore": ['{"sub_questions": []}'],
            _EVALUATE: [_SUFFICIENT],
            _VERIFY: [_SUPPORTED],
        },
        stream_rounds=[["Risposta ReAct [1]."]],
    )
    env(fake, [])
    result = await run_agent("come funziona il rimborso?", session=None)

    steps = [s["step"] for s in result.steps]
    assert "research" in steps
    assert "evaluate" in steps
    assert steps.index("research") < steps.index("evaluate") < steps.index("synthesize")


async def test_grading_scarta_tutto_astensione(env, monkeypatch) -> None:
    """CRAG che boccia tutti i passaggi → selezione vuota → risposta onesta
    di assenza fonti (astensione), mai una sintesi su materiale scartato."""
    monkeypatch.setenv("CRAG_ENABLED", "true")
    get_settings.cache_clear()
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _EVALUATE: [
                '{"grades": [{"n": 1, "grade": "irrilevante"}], '
                '"sufficient": true, "missing": "", "refined_queries": []}'
            ],
        },
    )
    env(fake, [_hit(10, 7, 0.4, reranked=True)])
    result = await run_agent("come funziona il rimborso?", session=None)

    assert "Non ho trovato passaggi pertinenti" in result.answer
    assert is_abstention(result.answer)
    assert result.citations == []
    assert result.confidence == "alta"
    assert "stream" not in fake.calls


# --- verify che morde e confidence composita ---------------------------------------


async def test_aderenza_fallita_innesca_refine(env) -> None:
    """supported=false → confidence bassa → refine (anche senza critic);
    le affermazioni contestate diventano feedback della rigenerazione."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "puntuale"}'],
            _VERIFY: [
                '{"supported": false, "unsupported_claims": ["scadenza 30 giorni"]}',
                _SUPPORTED,
            ],
        },
        stream_rounds=[["Bozza infedele [1]."], ["Risposta corretta [1]."]],
    )
    env(fake, [_hit(10, 7, 0.9)])
    result = await run_agent("termini per il reso?", session=None)

    steps = [s["step"] for s in result.steps]
    assert steps == ["classify", "retrieve", "synthesize", "verify", "refine", "verify"]
    assert result.answer == "Risposta corretta [1]."
    # dopo la rigenerazione l'aderenza è confermata: confidence non più bassa
    assert result.confidence == ""


async def test_confidence_composita_score_deboli_limitano_il_verdict(
    env, monkeypatch
) -> None:
    """Critic "solido" ma max score rerankato sotto strong → confidence
    "media": il badge non può dire alta/media piena su evidenza debole."""
    monkeypatch.setenv("CRITIC_ENABLED", "true")
    get_settings.cache_clear()
    fake = _FakeKimi(
        responses={
            _VERIFY: [_SUPPORTED],
            _CRITIC: ['{"verdict": "solido", "issues": []}'],
        },
    )
    env(fake, [])
    state = {
        "answer": "Il reso va richiesto entro 30 giorni [1].",
        "results": {10: _hit(10, 7, 0.45, reranked=True)},
        "excluded_passages": [],
    }
    outcome = await agent_graph.verify(state, config={})
    assert outcome["confidence"] == "media"


async def test_confidence_composita_aderenza_fallita_e_bassa(env, monkeypatch) -> None:
    """supported=false → bassa anche con critic "solido" (minimo composito)."""
    monkeypatch.setenv("CRITIC_ENABLED", "true")
    get_settings.cache_clear()
    fake = _FakeKimi(
        responses={
            _VERIFY: ['{"supported": false, "unsupported_claims": ["cifra"]}'],
            _CRITIC: ['{"verdict": "solido", "issues": []}'],
        },
    )
    env(fake, [])
    state = {
        "answer": "Il reso costa 100 euro [1].",
        "results": {10: _hit(10, 7, 0.9, reranked=True)},
        "excluded_passages": [],
    }
    outcome = await agent_graph.verify(state, config={})
    assert outcome["confidence"] == "bassa"
    assert any("cifra" in i for i in outcome["critic_issues"])


async def test_verify_saltata_su_astensione(env) -> None:
    """Stato astenuto: verify non chiama il modello e non tocca la confidence."""
    fake = _FakeKimi(responses={_VERIFY: ["non deve essere chiamata"]})
    env(fake, [])
    state = {
        "answer": f"{ABSTENTION_PREFIX} a: «x».",
        "abstained": True,
        "results": {10: _hit(10, 7, 0.9)},
        "excluded_passages": [],
    }
    outcome = await agent_graph.verify(state, config={})
    assert _VERIFY not in fake.calls
    assert "astensione" in outcome["steps"][0]["detail"]
    assert "confidence" not in outcome


# --- guardia sulla lingua delle query ----------------------------------------------


def test_looks_italian_euristica_conservativa() -> None:
    assert research.looks_italian("quali sono i termini del reso?")
    assert research.looks_italian("rimborso trasferte 2023")  # sole keyword: tieni
    assert research.looks_italian("più criteri di verifica")  # accentate
    assert not research.looks_italian("how to configure the NICE receiver")
    assert research.looks_italian("")  # non giudicabile


def test_ensure_italian_query_sostituisce() -> None:
    fallback = "come si configura il ricevitore?"
    assert research.ensure_italian_query("ricerca contratti 2024", fallback) == (
        "ricerca contratti 2024"
    )
    assert (
        research.ensure_italian_query("what are the return terms", fallback)
        == fallback
    )


async def test_hyde_testo_inglese_scartato(monkeypatch) -> None:
    """HyDE che scivola in inglese: testo scartato, ricerca singola col quesito."""
    monkeypatch.setenv("HYDE_ENABLED", "true")
    get_settings.cache_clear()
    fake = _FakeKimi(
        responses={"scrittore": ["The refund policy is described in the manual."]},
    )
    search = _FakeSearch([_hit(10, 7, 0.9)])
    monkeypatch.setattr(agent_tools, "hybrid_search", search)
    try:
        hits, hyde_text = await research.hyde_search(
            "come funziona il rimborso?", client=fake, session=None
        )
    finally:
        get_settings.cache_clear()
    assert hyde_text == ""  # scartato: non finisce nella fusione
    assert search.queries == ["come funziona il rimborso?"]
    assert len(hits) == 1


async def test_react_query_inglese_sostituita(monkeypatch) -> None:
    """Query hybrid_search generata in inglese nel loop ReAct → sostituita
    con il quesito autonomo."""
    search = _FakeSearch([_hit(10, 7, 0.9)])
    monkeypatch.setattr(agent_tools, "hybrid_search", search)
    call = {
        "id": "c1",
        "name": "hybrid_search",
        "arguments": {"query": "what is the refund policy"},
    }
    outcome = await research._exec_tool(
        call,
        client=_FakeKimi(responses={}),
        session=None,
        question="qual è la politica di rimborso?",
    )
    assert search.queries == ["qual è la politica di rimborso?"]
    assert outcome["query"] == "qual è la politica di rimborso?"


# --- dedup dei passaggi quasi identici ---------------------------------------------


def test_select_passages_scarta_quasi_duplicati() -> None:
    """Stesso testo normalizzato in documenti diversi: resta solo la copia
    con lo score migliore."""
    same = "Il rimborso è previsto entro 30 giorni dalla richiesta."
    results = {
        10: _hit(10, 7, 0.9, context=same),
        11: _hit(11, 8, 0.8, context="  il RIMBORSO è previsto entro 30 giorni dalla richiesta. "),
        12: _hit(12, 9, 0.7, context="Testo del tutto diverso sulla diaria."),
    }
    selected = agent_graph._select_passages(results)
    assert [r.chunk_id for r in selected] == [10, 12]


# --- eval: quesiti expects_no_answer -----------------------------------------------


def test_score_expects_no_answer_pass_su_astensione() -> None:
    out = score_answer(
        f"{ABSTENTION_PREFIX} a: «ricevitore NICE Florx2r».",
        [],
        {"question": "q", "expects_no_answer": True},
    )
    assert out["passed"] is True
    assert out["score"] == 1.0


def test_score_expects_no_answer_fail_su_risposta_nel_merito() -> None:
    out = score_answer(
        "Il ricevitore si programma tenendo premuto il tasto P1 [1].",
        [{"n": 1}],
        {"question": "q", "expects_no_answer": True},
        retrieved_count=5,
    )
    assert out["passed"] is False
    assert out["score"] == 0.0
    assert out["checks"]["expects_no_answer"]["abstained"] is False


def test_is_abstention_pattern() -> None:
    assert is_abstention("Non ho trovato passaggi pertinenti nei documenti.")
    assert is_abstention("Il materiale indicizzato non contiene informazioni su X.")
    assert not is_abstention("Il rimborso è previsto entro 30 giorni [1].")


def test_hallucination_metrics() -> None:
    from scripts.evaluate import hallucination_metrics

    results = [
        {"expects_no_answer": True, "abstained": True},
        {"expects_no_answer": True, "abstained": False},  # allucinazione
        {"expects_no_answer": False, "abstained": False},
        {"expects_no_answer": False, "abstained": True},  # over-abstention
    ]
    metrics = hallucination_metrics(results)
    assert metrics["hallucination_rate"] == 0.5
    assert metrics["over_abstention_rate"] == 0.5
    assert metrics["no_answer_questions"] == 2
    assert metrics["normal_questions"] == 2
