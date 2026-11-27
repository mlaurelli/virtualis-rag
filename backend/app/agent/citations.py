"""Citazioni ``[n]`` della risposta agentica: mappa, parsing e controlli euristici.

Il flusso è: i passaggi selezionati per la sintesi sono numerati da
:func:`build_citation_map`; il modello marca le affermazioni con ``[n]``;
:func:`parse_citations` risolve i marcatori nei metadati dei passaggi per il
payload ``citations`` dell'API. Il controllo di aderenza vero e proprio è
demandato al nodo ``verify`` del grafo: qui restano solo euristiche leggere.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

from ..retrieval.hybrid import SearchResult

_MARKER_RE = re.compile(r"\[(\d{1,3})\]")

# Soglie dell'euristica "risposta non banale senza citazioni"
_MIN_LEN_NON_BANALE = 80
_MIN_FRASI_NON_BANALE = 2


class Citation(BaseModel):
    """Una citazione risolta, pronta per il payload SSE/API (Fase 5)."""

    n: int
    document_id: int
    title: str
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    snippet: str = ""
    source_name: str = ""
    # Score di pertinenza del passaggio citato (cross-encoder se rerankato,
    # RRF altrimenti); campo ADDITIVO: il frontend può ignorarlo
    score: float | None = None
    # Rettangoli del passaggio nel PDF (punti, origine bottom-left): il
    # viewer li usa per evidenziare il passaggio citato. Vuota se il
    # documento non ha coordinate (non-PDF o indicizzato prima dei bbox).
    bboxes: list[dict[str, Any]] = Field(default_factory=list)


class ParsedCitations(BaseModel):
    """Esito del parsing: citazioni risolte + marcatori non risolvibili."""

    citations: list[Citation] = Field(default_factory=list)
    invalid_markers: list[int] = Field(default_factory=list)


def build_citation_map(results: list[SearchResult]) -> dict[int, SearchResult]:
    """Assegna i numeri ``[1..N]`` ai passaggi.

    Dedup per ``chunk_id`` (tiene lo score migliore) e ordine per score
    decrescente: il passaggio più rilevante è sempre ``[1]``.
    """
    best_by_chunk: dict[int, SearchResult] = {}
    for r in results:
        current = best_by_chunk.get(r.chunk_id)
        if current is None or r.score > current.score:
            best_by_chunk[r.chunk_id] = r
    ordered = sorted(best_by_chunk.values(), key=lambda r: r.score, reverse=True)
    return {n: r for n, r in enumerate(ordered, start=1)}


def parse_citations(answer_text: str, citation_map: dict[int, SearchResult]) -> ParsedCitations:
    """Estrae i marcatori ``[n]`` dal testo e li risolve nei metadati.

    I marcatori sono restituiti in ordine di prima comparsa e deduplicati;
    quelli senza passaggio corrispondente finiscono in ``invalid_markers``.
    """
    citations: list[Citation] = []
    invalid: list[int] = []
    seen: set[int] = set()
    for match in _MARKER_RE.finditer(answer_text):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        result = citation_map.get(n)
        if result is None:
            invalid.append(n)
            continue
        citations.append(
            Citation(
                n=n,
                document_id=result.document_id,
                title=result.title,
                page=result.page,
                section_path=list(result.section_path),
                snippet=result.snippet,
                source_name=result.source_name,
                bboxes=list(result.bboxes),
                score=result.score,
            )
        )
    return ParsedCitations(citations=citations, invalid_markers=invalid)


def strip_or_report_uncited(answer_text: str, citation_map: dict[int, SearchResult]) -> list[str]:
    """Euristica anti-allucinazione: segnala risposte non banali senza marcatori.

    Restituisce una lista di warning (vuota se tutto a posto). Il controllo
    rigoroso di aderenza è svolto dal nodo ``verify``: qui si segnala solo il
    caso macroscopico di un testo articolato che non cita alcun passaggio.
    """
    if _MARKER_RE.search(answer_text):
        return []
    sentences = [s for s in re.split(r"[.!?]\s*", answer_text) if s.strip()]
    if len(answer_text) >= _MIN_LEN_NON_BANALE and len(sentences) >= _MIN_FRASI_NON_BANALE:
        return [
            "la risposta articolata non contiene alcun marcatore di citazione [n]: "
            "le affermazioni potrebbero non essere ancorate ai passaggi recuperati"
        ]
    return []
