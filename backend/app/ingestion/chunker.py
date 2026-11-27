"""Chunking gerarchico struttura-aware dei documenti normalizzati.

Regole principali:

- i chunk rispettano i confini di sezione (mai a metà tra due sezioni);
- un chunk non supera ``target_tokens`` (conteggio tiktoken cl100k_base,
  oppure approssimazione ``chars/4`` selezionabile con ``token_method``);
- tra chunk consecutivi della stessa sezione c'è un overlap di circa
  ``overlap_tokens``, ottenuto riutilizzando le ultime frasi intere
  (nessun taglio a metà parola);
- ogni tabella diventa un chunk dedicato, con un prefisso descrittivo in
  linguaggio naturale per renderla raggiungibile dalle query;
- il percorso di sezione è anteposto al testo del chunk come riga marcata
  (contesto per il retrieval);
- ``context_text`` è la finestra estesa (chunk ± 1 adiacente, max ~2x target)
  usata in fase di retrieval per dare più contesto al LLM.

Funzioni pure, senza I/O: facilmente testabili.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from .converter import BBox, Block, NormalizedDoc

log = structlog.get_logger(__name__)

# Separatore di frasi: dopo punteggiatura forte seguita da spazio/newline
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|(?<=\n)\s*")

_tokenizer = None


def _get_tokenizer() -> Any:
    """Encoding tiktoken cl100k_base, caricato lazy al primo uso."""
    global _tokenizer
    if _tokenizer is None:
        import tiktoken

        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def count_tokens(text: str, *, method: str = "tiktoken") -> int:
    """Conta i token di ``text``.

    ``method="tiktoken"`` usa cl100k_base; ``method="approx"`` stima
    ``len(text) // 4``. In caso di errore di tiktoken si ripiega sulla stima.
    """
    if not text:
        return 0
    if method == "approx":
        return max(1, len(text) // 4)
    try:
        return len(_get_tokenizer().encode(text))
    except Exception:
        log.warning("tiktoken non disponibile, uso stima chars/4")
        return max(1, len(text) // 4)


@dataclass
class ChunkData:
    """Chunk pronto per embedding/indicizzazione (indipendente dall'ORM)."""

    ord: int
    text: str
    context_text: str
    section_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0
    kind: str = "text"  # "text" | "table"
    # Rettangoli di layout dei block che compongono il chunk (multi-rettangolo)
    bboxes: list[BBox] = field(default_factory=list)


def _section_prefix(section_path: list[str]) -> str:
    """Riga marcata con il percorso di sezione, anteposta al chunk."""
    if not section_path:
        return ""
    return "[Sezione: " + " > ".join(section_path) + "]\n\n"


def _split_sentences(text: str) -> list[str]:
    """Divide il testo in frasi (mai a metà parola)."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    return parts or [text]


def _split_words(text: str) -> list[str]:
    """Divide il testo in parole (fallback per frasi più lunghe del target)."""
    return text.split()


def _tail_within(text: str, budget_tokens: int, *, method: str) -> str:
    """Coda del testo (frasi intere) entro il budget di token."""
    if budget_tokens <= 0:
        return ""
    sentences = _split_sentences(text)
    kept: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        cost = count_tokens(sentence, method=method)
        if kept and total + cost > budget_tokens:
            break
        kept.append(sentence)
        total += cost
    kept.reverse()
    return " ".join(kept)


def _head_within(text: str, budget_tokens: int, *, method: str) -> str:
    """Testa del testo (frasi intere) entro il budget di token."""
    if budget_tokens <= 0:
        return ""
    sentences = _split_sentences(text)
    kept: list[str] = []
    total = 0
    for sentence in sentences:
        cost = count_tokens(sentence, method=method)
        if kept and total + cost > budget_tokens:
            break
        kept.append(sentence)
        total += cost
    return " ".join(kept)


@dataclass
class _Piece:
    """Unità atomica di accumulo: una frase con pagina e bbox del suo block."""

    text: str
    page: int | None
    bboxes: list[BBox] = field(default_factory=list)


def _blocks_to_pieces(blocks: list[Block]) -> list[_Piece]:
    """Appiattisce i blocchi testuali in frasi con pagina e bbox associati."""
    pieces: list[_Piece] = []
    for block in blocks:
        for sentence in _split_sentences(block.text):
            pieces.append(_Piece(text=sentence, page=block.page, bboxes=block.bboxes))
    return pieces


def _merge_bboxes(pieces: list[_Piece]) -> list[BBox]:
    """Unione dei bbox dei pezzi, deduplicata (evidenziazione multi-rettangolo)."""
    merged: list[BBox] = []
    seen: set[tuple[float, ...]] = set()
    for piece in pieces:
        for bbox in piece.bboxes:
            key = (bbox["page"], bbox["l"], bbox["t"], bbox["r"], bbox["b"])
            if key not in seen:
                seen.add(key)
                merged.append(bbox)
    return merged


def _emit_text_chunk(
    body_pieces: list[_Piece],
    *,
    section_path: list[str],
    ord_: int,
    method: str,
) -> ChunkData:
    """Costruisce un ChunkData testuale da un insieme di frasi."""
    body = "\n\n".join(piece.text for piece in body_pieces)
    text = _section_prefix(section_path) + body
    pages = [piece.page for piece in body_pieces if piece.page is not None]
    return ChunkData(
        ord=ord_,
        text=text,
        context_text="",  # calcolato in una seconda passata
        section_path=list(section_path),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        token_count=count_tokens(text, method=method),
        kind="text",
        bboxes=_merge_bboxes(body_pieces),
    )


def _chunk_text_segment(
    blocks: list[Block],
    *,
    section_path: list[str],
    target_tokens: int,
    overlap_tokens: int,
    method: str,
    ord_start: int,
) -> list[ChunkData]:
    """Segmenta i blocchi di una sezione in chunk con overlap di frasi.

    Il limite è verificato sul testo completo (prefisso + corpo), non sulla
    somma dei conteggi dei pezzi: gli arrotondamenti non possono far
    sforare il target.
    """
    prefix = _section_prefix(section_path)
    prefix_tokens = count_tokens(prefix, method=method)
    # Budget per il corpo nel caso di frasi oltre il target: mai sotto metà
    body_budget = max(target_tokens - prefix_tokens, target_tokens // 2)

    def full_count(pieces: list[_Piece]) -> int:
        body = "\n\n".join(piece.text for piece in pieces)
        return count_tokens(prefix + body, method=method)

    chunks: list[ChunkData] = []
    current: list[_Piece] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        chunks.append(
            _emit_text_chunk(
                current, section_path=section_path, ord_=ord_start + len(chunks),
                method=method,
            )
        )
        # Overlap: riusa le ultime frasi del chunk appena chiuso
        overlap: list[_Piece] = []
        total = 0
        for piece in reversed(current):
            cost = count_tokens(piece.text, method=method)
            if overlap and total + cost > overlap_tokens:
                break
            overlap.append(piece)
            total += cost
        overlap.reverse()
        current = overlap

    for piece in _blocks_to_pieces(blocks):
        if count_tokens(piece.text, method=method) > body_budget:
            # Frase singola oltre il budget: chiudi il chunk corrente e
            # spezza la frase per parole (mai a metà parola)
            if current:
                chunks.append(
                    _emit_text_chunk(
                        current, section_path=section_path,
                        ord_=ord_start + len(chunks), method=method,
                    )
                )
                current = []
            buf: list[str] = []
            for word in _split_words(piece.text):
                if buf and count_tokens(
                    prefix + " ".join([*buf, word]), method=method
                ) > target_tokens:
                    chunks.append(
                        _emit_text_chunk(
                            [_Piece(text=" ".join(buf), page=piece.page,
                                    bboxes=piece.bboxes)],
                            section_path=section_path,
                            ord_=ord_start + len(chunks), method=method,
                        )
                    )
                    buf = []
                buf.append(word)
            if buf:
                current = [_Piece(text=" ".join(buf), page=piece.page,
                                  bboxes=piece.bboxes)]
            continue
        if current and full_count([*current, piece]) > target_tokens:
            flush()
            if current and full_count([*current, piece]) > target_tokens:
                current = []  # overlap troppo lungo: si riparte da zero
        current.append(piece)
    if current:
        chunks.append(
            _emit_text_chunk(
                current, section_path=section_path, ord_=ord_start + len(chunks),
                method=method,
            )
        )
    return chunks


def _table_chunk(
    block: Block,
    *,
    doc_title: str,
    target_tokens: int,
    method: str,
    ord_: int,
) -> ChunkData:
    """Chunk dedicato a una tabella, con prefisso descrittivo in linguaggio naturale.

    La tabella non viene mai spezzata: perderebbe la struttura righe/colonne.
    Può quindi superare il target (segnalato nel log).
    """
    location = f"Tabella nel documento «{doc_title}»"
    if block.section_path:
        location += ", sezione «" + " > ".join(block.section_path) + "»"
    text = f"{location}:\n{block.text}"
    token_count = count_tokens(text, method=method)
    if token_count > target_tokens:
        log.info(
            "tabella oltre il target token, mantenuta intera",
            tokens=token_count, target=target_tokens,
        )
    return ChunkData(
        ord=ord_,
        text=text,
        context_text="",
        section_path=list(block.section_path),
        page_start=block.page,
        page_end=block.page,
        token_count=token_count,
        kind="table",
        bboxes=list(block.bboxes),
    )


def _fill_context(chunks: list[ChunkData], *, target_tokens: int, method: str) -> None:
    """Calcola ``context_text``: chunk ± 1 adiacente, entro ~2x target."""
    for i, chunk in enumerate(chunks):
        budget = max(2 * target_tokens - chunk.token_count, 0)
        prev_part = (
            _tail_within(chunks[i - 1].text, budget // 2, method=method) if i > 0 else ""
        )
        next_part = (
            _head_within(chunks[i + 1].text, budget - budget // 2, method=method)
            if i < len(chunks) - 1
            else ""
        )
        chunk.context_text = "\n\n".join(
            part for part in (prev_part, chunk.text, next_part) if part
        )


def chunk_document(
    doc: NormalizedDoc,
    *,
    target_tokens: int,
    overlap_tokens: int,
    token_method: str = "tiktoken",
) -> list[ChunkData]:
    """Segmenta un :class:`NormalizedDoc` in chunk gerarchici.

    I blocchi sono raggruppati per sezione; le tabelle formano sempre chunk
    dedicati. L'overlap vale solo tra chunk della stessa sezione.
    """
    chunks: list[ChunkData] = []
    segment: list[Block] = []
    segment_path: list[str] = []

    def flush_segment() -> None:
        nonlocal segment, segment_path
        if segment:
            chunks.extend(
                _chunk_text_segment(
                    segment,
                    section_path=segment_path,
                    target_tokens=target_tokens,
                    overlap_tokens=overlap_tokens,
                    method=token_method,
                    ord_start=len(chunks),
                )
            )
            segment = []
            segment_path = []

    for block in doc.blocks:
        if block.kind == "table":
            flush_segment()
            chunks.append(
                _table_chunk(
                    block,
                    doc_title=doc.title or "senza titolo",
                    target_tokens=target_tokens,
                    method=token_method,
                    ord_=len(chunks),
                )
            )
        elif block.kind == "heading":
            # Un heading apre una nuova sezione: chiudi il segmento corrente
            flush_segment()
        else:
            if segment and block.section_path != segment_path:
                flush_segment()
            if not segment:
                segment_path = list(block.section_path)
            segment.append(block)
    flush_segment()

    _fill_context(chunks, target_tokens=target_tokens, method=token_method)
    # Rinumera gli ordinali (difensivo: già sequenziali per costruzione)
    for i, chunk in enumerate(chunks):
        chunk.ord = i
    return chunks
