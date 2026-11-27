"""Test della guardia sul budget di token del contesto di sintesi.

Oltre ``synthesize_max_context_tokens`` i passaggi meno rilevanti sono
tagliati prima della chiamata LLM: il payload resta sotto budget e i
passaggi migliori restano. Il conteggio token è sostituito da una stima
deterministica (len/4) per non dipendere da tiktoken.
"""

from typing import Any

import pytest

from app.agent import graph as agent_graph
from app.agent import run_agent
from app.agent import tools as agent_tools
from app.agent.citations import build_citation_map
from app.config import get_settings
from app.retrieval.hybrid import SearchResult


def _result(chunk_id: int, score: float, text: str) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=chunk_id,
        title=f"doc {chunk_id}",
        snippet=text[:50],
        context_text=text,
        score=score,
        page=None,
    )


def _approx_tokens(text: str) -> int:
    """Stima deterministica dei token per i test (come il fallback del chunker)."""
    return max(1, len(text) // 4)


@pytest.fixture
def approx_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_graph, "count_tokens", _approx_tokens)


def test_fit_taglia_i_meno_rilevanti_e_resta_sotto_budget(approx_tokens: None) -> None:
    """8 passaggi grandi, budget per ~3: i migliori per score restano."""
    cmap = build_citation_map(
        [
            _result(i, score=1.0 - i * 0.1, text=f"passaggio{i} " + "x" * 4000)
            for i in range(1, 9)
        ]
    )
    budget = 3500
    kept = agent_graph._fit_citation_map_to_budget(
        cmap, question="quesito?", extra="", budget=budget
    )
    assert 1 <= len(kept) < len(cmap)
    # i passaggi conservati sono i migliori per score, numerati da 1
    assert [kept[n].chunk_id for n in sorted(kept)] == list(range(1, len(kept) + 1))
    elenco = "\n\n".join(agent_graph._format_passage(n, r) for n, r in kept.items())
    assert _approx_tokens(elenco) <= budget


def test_fit_conserva_sempre_almeno_un_passaggio(approx_tokens: None) -> None:
    """Anche un singolo passaggio oltre budget non è mai eliminato del tutto:
    meglio un contesto troppo grande che una risposta senza fonti."""
    cmap = build_citation_map([_result(1, 0.9, "z" * 100000)])
    kept = agent_graph._fit_citation_map_to_budget(
        cmap, question="q", extra="", budget=10
    )
    assert list(kept) == [1]


def test_fit_sotto_budget_non_tocca_nulla(approx_tokens: None) -> None:
    """Contesto già sotto budget: nessun taglio."""
    cmap = build_citation_map([_result(1, 0.9, "breve"), _result(2, 0.5, "corto")])
    kept = agent_graph._fit_citation_map_to_budget(
        cmap, question="q", extra="", budget=100000
    )
    assert list(kept) == [1, 2]


class _CapturingKimi:
    """KimiClient finto che registra i messaggi inviati in streaming."""

    def __init__(self) -> None:
        self.stream_messages: list[dict[str, Any]] | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> str:
        system = messages[0]["content"]
        if "classificatore" in system:
            return '{"path": "fast", "reason": "quesito puntuale"}'
        if "verificatore" in system:
            return '{"supported": true, "unsupported_claims": []}'
        raise AssertionError(f"prompt non riconosciuto: {system[:50]!r}")

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.stream_messages = messages
        yield "Risposta sintetica [1]."


def _hit_grande(chunk_id: int, score: float) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": chunk_id,
        "title": f"Documento {chunk_id}",
        "snippet": f"snippet {chunk_id}",
        "context_text": f"CONTENUTO-{chunk_id} " + "y" * 4000,
        "score": score,
        "page": None,
        "section_path": [],
        "source_name": "Archivio",
        "doc_type": "pdf",
        "modified_at": None,
    }


async def test_grafo_sintesi_sotto_budget_con_molti_passaggi_grandi(
    approx_tokens: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end sul grafo: con molti passaggi grandi il payload della
    sintesi resta sotto budget e i passaggi migliori restano nel prompt."""
    monkeypatch.setenv("SYNTHESIZE_MAX_CONTEXT_TOKENS", "3500")
    get_settings.cache_clear()
    try:
        fake = _CapturingKimi()
        hits = [_hit_grande(i, score=1.0 - i * 0.1) for i in range(1, 9)]

        async def fake_search(
            query: str, filters: Any = None, limit: int = 8, *, session: Any = None
        ) -> list[dict[str, Any]]:
            return [dict(h) for h in hits]

        monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake)
        monkeypatch.setattr(agent_tools, "hybrid_search", fake_search)

        result = await run_agent("quesito di prova?", session=None)

        assert fake.stream_messages is not None
        user = fake.stream_messages[1]["content"]
        assert _approx_tokens(user) <= 3500
        # i passaggi migliori restano, i peggiori sono tagliati
        assert "CONTENUTO-1" in user
        assert "CONTENUTO-8" not in user
        assert result.answer == "Risposta sintetica [1]."
        assert len(result.citations) == 1
    finally:
        get_settings.cache_clear()
