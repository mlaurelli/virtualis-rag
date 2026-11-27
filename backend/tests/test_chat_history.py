"""Test del contesto conversazionale dell'agente (history + standalone_question).

Il KimiClient è sempre MOCKATO (nessuna rete): un fake risponde in base al
marcatore presente nel system prompt (come in test_agent) e registra i
messaggi utente ricevuti, così i test verificano quali prompt vedono i nodi.
La ricerca ibrida è sostituita da un fake che annota le interrogazioni.
"""

from collections import deque
from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import run_agent
from app.agent import tools as agent_tools

# Marcatori distintivi dei system prompt (vedi app/agent/prompts.py)
_CLASSIFY, _PLAN, _EVALUATE, _VERIFY = (
    "classificatore",
    "pianificatore",
    "valutatore",
    "verificatore",
)

# Storia di esempio: un primo scambio sul contratto di manutenzione 2023
_HISTORY = [
    {
        "role": "user",
        "content": "quali sono le condizioni di garanzia del contratto di manutenzione 2023?",
    },
    {
        "role": "assistant",
        "content": "La garanzia del contratto 2023 copre 12 mesi [1].",
    },
]


class _FakeKimi:
    """KimiClient finto: risposte JSON in coda per tipo di prompt + token di
    sintesi prefissati. Registra i messaggi utente per le asserzioni."""

    def __init__(self, responses: dict[str, list[str]], stream_tokens: list[str]) -> None:
        self._responses = {k: deque(v) for k, v in responses.items()}
        self._stream_tokens = stream_tokens
        self.user_messages: list[tuple[str, str]] = []  # (marcatore, contenuto)
        self.stream_messages: list[dict[str, Any]] = []  # messaggi della sintesi

    async def chat(self, messages, *, json_mode=False, **kwargs) -> str:
        system = messages[0]["content"]
        for marker, queue in self._responses.items():
            if marker in system:
                self.user_messages.append((marker, messages[1]["content"]))
                if len(queue) > 1:
                    return queue.popleft()
                return queue[0]  # l'ultima risposta si ripete
        raise AssertionError(f"prompt di sistema non riconosciuto: {system[:60]!r}")

    async def chat_stream(self, messages, **kwargs):
        self.stream_messages = list(messages)
        for token in self._stream_tokens:
            yield token

    def prompt_of(self, marker: str) -> str:
        """Ultimo messaggio utente inviato al prompt con il marcatore dato."""
        return next(m for tag, m in reversed(self.user_messages) if tag == marker)


class _FakeSearch:
    """Sostituto di tools.hybrid_search: stesse hit per ogni query, tiene
    traccia delle interrogazioni eseguite."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def __call__(self, query, filters=None, limit=8, *, session=None):
        self.queries.append(query)
        return [dict(h) for h in self._hits]


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
def fake_env(monkeypatch):
    """Monta KimiClient e hybrid_search finti nel grafo (come test_agent)."""

    def install(
        fake_kimi: _FakeKimi, hits: list[dict[str, Any]]
    ) -> tuple[_FakeKimi, _FakeSearch]:
        fake_search = _FakeSearch(hits)
        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)
        monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)
        return fake_kimi, fake_search

    return install


async def test_classify_con_storia_riformula_il_quesito(fake_env) -> None:
    """Follow-up "e nel 2024?" con storia: classify riceve la conversazione e
    produce la standalone_question risolta, usata per la ricerca e tracciata
    nello step (trasparenza/audit)."""
    standalone = (
        "quali sono le condizioni di garanzia del contratto di manutenzione nel 2024?"
    )
    fake = _FakeKimi(
        responses={
            _CLASSIFY: [
                '{"path": "fast", "reason": "quesito puntuale", '
                f'"standalone_question": "{standalone}"}}'
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["La garanzia 2024 copre 24 mesi [1]."],
    )
    _, search = fake_env(fake, [_hit(10, 7, 0.9)])

    result = await run_agent("e nel 2024?", session=None, history=_HISTORY)

    # lo state conserva entrambe le forme del quesito
    assert result.standalone_question == standalone
    # la ricerca (e queries_run) usa la forma autonoma, non "e nel 2024?"
    assert search.queries == [standalone]
    assert result.queries_run == [standalone]
    # classify ha ricevuto la conversazione e il quesito attuale
    prompt = fake.prompt_of(_CLASSIFY)
    assert "CONVERSAZIONE PRECEDENTE" in prompt
    assert "contratto di manutenzione 2023" in prompt
    assert "QUESITO ATTUALE: e nel 2024?" in prompt
    # la riformulazione è registrata nello step classify (→ audit)
    classify_step = result.steps[0]
    assert classify_step["step"] == "classify"
    assert f"quesito riformulato: «{standalone}»" in classify_step["detail"]
    assert result.answer == "La garanzia 2024 copre 24 mesi [1]."


async def test_classify_quesito_gia_autonomo_resta_invariato(fake_env) -> None:
    """Se il quesito è già autonomo, standalone_question coincide con
    l'originale e lo step non segnala alcuna riformulazione."""
    question = "qual è la scadenza del contratto di assistenza?"
    fake = _FakeKimi(
        responses={
            _CLASSIFY: [
                '{"path": "fast", "reason": "puntuale", '
                f'"standalone_question": "{question}"}}'
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Scade a dicembre [1]."],
    )
    _, search = fake_env(fake, [_hit(10, 7, 0.9)])

    result = await run_agent(question, session=None, history=_HISTORY)

    assert result.standalone_question == question
    assert search.queries == [question]
    assert "riformulato" not in result.steps[0]["detail"]


async def test_senza_storia_comportamento_identico(fake_env) -> None:
    """Senza history il prompt di classify non contiene la conversazione, la
    standalone_question ripiega sul quesito originale e la ricerca è invariata
    (il fake risponde con il vecchio JSON senza il nuovo campo)."""
    fake = _FakeKimi(
        responses={
            _CLASSIFY: ['{"path": "fast", "reason": "quesito puntuale"}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Le spese sono rimborsate [1]."],
    )
    _, search = fake_env(fake, [_hit(10, 7, 0.9)])
    question = "come sono rimborsate le spese di trasferta?"

    result = await run_agent(question, session=None)

    assert result.standalone_question == question
    assert search.queries == [question]
    assert "CONVERSAZIONE" not in fake.prompt_of(_CLASSIFY)
    assert "riformulato" not in result.steps[0]["detail"]
    # anche la sintesi non riceve alcun blocco di storia
    assert "CONVERSAZIONE PRECEDENTE" not in fake.stream_messages[1]["content"]


async def test_sintesi_riceve_la_storia_solo_per_continuita(fake_env) -> None:
    """Con history la sintesi vede le ultime battute (marcate come NON fonte)
    e il quesito nella forma autonoma; i passaggi restano l'unica base."""
    standalone = "condizioni di garanzia del contratto di manutenzione nel 2024"
    fake = _FakeKimi(
        responses={
            _CLASSIFY: [
                '{"path": "fast", "reason": "puntuale", '
                f'"standalone_question": "{standalone}"}}'
            ],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Nel 2024 la garanzia copre 24 mesi [1]."],
    )
    fake_env(fake, [_hit(10, 7, 0.9)])

    await run_agent("e nel 2024?", session=None, history=_HISTORY)

    user = fake.stream_messages[1]["content"]
    assert f"QUESITO: {standalone}" in user
    assert "PASSAGGI:" in user
    assert "CONVERSAZIONE PRECEDENTE" in user
    assert "NON una fonte di fatti" in user
    assert "utente: quali sono le condizioni di garanzia" in user
    assert "assistente: La garanzia del contratto 2023" in user


async def test_percorso_agentic_usa_il_quesito_autonomo(fake_env) -> None:
    """Nel percorso agentic plan ed evaluate ricevono la standalone_question
    (non il follow-up con riferimenti)."""
    standalone = "confronto delle garanzie del contratto di manutenzione 2023 e 2024"
    fake = _FakeKimi(
        responses={
            _CLASSIFY: [
                '{"path": "agentic", "reason": "raffronto temporale", '
                f'"standalone_question": "{standalone}"}}'
            ],
            _PLAN: [
                '{"sub_questions": ['
                '{"query": "garanzia contratto manutenzione 2023", "purpose": "2023"}, '
                '{"query": "garanzia contratto manutenzione 2024", "purpose": "2024"}'
                "]}"
            ],
            _EVALUATE: ['{"sufficient": true, "missing": "", "refined_queries": []}'],
            _VERIFY: ['{"supported": true, "unsupported_claims": []}'],
        },
        stream_tokens=["Confronto [1]."],
    )
    _, search = fake_env(fake, [_hit(20, 7, 0.9)])

    result = await run_agent("e confrontala con il 2024", session=None, history=_HISTORY)

    assert result.path == "agentic"
    assert result.standalone_question == standalone
    assert fake.prompt_of(_PLAN) == standalone
    assert f"QUESITO: {standalone}" in fake.prompt_of(_EVALUATE)
    assert search.queries == [
        "garanzia contratto manutenzione 2023",
        "garanzia contratto manutenzione 2024",
    ]
