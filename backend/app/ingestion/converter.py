"""Conversione dei documenti in una rappresentazione normalizzata a blocchi.

Usa Docling come convertitore universale (PDF, DOCX, XLSX, PPTX, HTML, immagini)
e produce una sequenza piatta di :class:`Block` con gerarchia di sezione
(``section_path``), numero di pagina, rettangoli di layout (``bboxes``,
solo per i formati che li espongono, tipicamente PDF) e metadati del
documento.

I file ``.md`` e ``.txt`` vengono letti direttamente (fallback leggero, senza
avviare Docling) con un parsing minimo dei heading Markdown.

Con ``settings.ocr_enabled`` attivo si applica l'OCR (Tesseract, ita+eng):
sempre per le immagini; per i PDF solo quando il testo nativo estratto è
sotto la soglia ``_OCR_MIN_CHARS_PER_PAGE`` (tipico delle scansioni).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import structlog

from ..config import get_settings

log = structlog.get_logger(__name__)

BlockKind = Literal["heading", "paragraph", "table", "list_item", "caption"]


class BBox(TypedDict):
    """Rettangolo di un provenance Docling, in punti PDF.

    Coordinate con **origine bottom-left** (formato nativo dei PDF): il
    viewer le converte in coordinate viewport top-left. ``page_width`` e
    ``page_height`` sono presenti quando Docling espone le dimensioni della
    pagina (servono per la conversione lato viewer).
    """

    page: int
    l: float  # noqa: E741 — chiave JSON del formato bbox (left)
    t: float
    r: float
    b: float
    page_width: NotRequired[float]
    page_height: NotRequired[float]


class ConversionError(Exception):
    """Errore di conversione di un documento (messaggio pensato per l'utente)."""


@dataclass
class Block:
    """Blocco testuale strutturato estratto dal documento."""

    kind: BlockKind
    text: str
    level: int = 0  # livello gerarchico (solo per kind="heading", 1 = titolo principale)
    page: int | None = None
    section_path: list[str] = field(default_factory=list)
    # Rettangoli dei provenance (solo formati con layout, tipicamente PDF)
    bboxes: list[BBox] = field(default_factory=list)


@dataclass
class NormalizedDoc:
    """Documento normalizzato: blocchi ordinati + metadati."""

    blocks: list[Block]
    title: str = ""
    author: str = ""
    modified_at: datetime | None = None
    doc_type: str = ""
    page_count: int | None = None


# Estensioni gestite con lettura diretta (senza Docling)
_PLAIN_EXTENSIONS = {".md", ".markdown", ".txt"}

# Estensioni immagine: l'OCR è l'unica via di estrazione del testo
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}

# Soglia minima di testo nativo (caratteri/pagina): sotto questa soglia un
# PDF è trattato come scansione e riconvertito con OCR
_OCR_MIN_CHARS_PER_PAGE = 100

# Heading Markdown: da 1 a 6 cancelletti seguiti da spazio
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def convert_document(path: Path, *, doc_meta: dict[str, Any] | None = None) -> NormalizedDoc:
    """Converte ``path`` in un :class:`NormalizedDoc`.

    ``doc_meta`` può contenere override di metadati (``title``, ``author``,
    ``modified_at``) provenienti dal connettore che ha scaricato il file.

    Solleva :class:`ConversionError` con un messaggio utile in caso di fallimento.
    """
    doc_meta = doc_meta or {}
    path = Path(path)
    if not path.is_file():
        # Solo il nome: il path completo non deve finire nelle risposte API
        raise ConversionError(f"File non trovato: {path.name}")

    try:
        if path.suffix.lower() in _PLAIN_EXTENSIONS:
            doc = _convert_plain(path)
        else:
            doc = _convert_with_docling(path)
    except ConversionError:
        raise
    except Exception as exc:
        log.error("conversione fallita", path=str(path), error=str(exc))
        raise ConversionError(f"Impossibile convertire {path.name}: {exc}") from exc

    # Metadati: override espliciti dal connettore, poi file, poi Docling
    stat = path.stat()
    doc.doc_type = doc.doc_type or path.suffix.lower().lstrip(".")
    doc.title = doc_meta.get("title") or doc.title or path.stem
    doc.author = doc_meta.get("author") or doc.author or ""
    doc.modified_at = (
        doc_meta.get("modified_at")
        or doc.modified_at
        or datetime.fromtimestamp(stat.st_mtime, tz=UTC).replace(tzinfo=None)
    )
    return doc


def _convert_plain(path: Path) -> NormalizedDoc:
    """Legge un file Markdown/testo semplice estraendo i heading Markdown."""
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")

    blocks: list[Block] = []
    headings: dict[int, str] = {}  # livello -> ultimo titolo visto

    def section_path(level: int) -> list[str]:
        return [headings[lvl] for lvl in sorted(headings) if lvl < level]

    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        text = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if text:
            blocks.append(
                Block(kind="paragraph", text=text, section_path=section_path(99))
            )

    for line in raw.splitlines():
        match = _MD_HEADING_RE.match(line)
        if match:
            flush_paragraph()
            level = len(match.group(1))
            title = match.group(2).strip()
            # Azzera i livelli più profondi e registra il nuovo heading
            for lvl in [lvl for lvl in headings if lvl >= level]:
                del headings[lvl]
            headings[level] = title
            blocks.append(
                Block(
                    kind="heading",
                    text=title,
                    level=level,
                    section_path=section_path(level),
                )
            )
        elif line.strip():
            paragraph_lines.append(line)
        else:
            flush_paragraph()
    flush_paragraph()

    doc = NormalizedDoc(blocks=blocks)
    # Titolo dal primo heading di livello 1, se presente
    for block in blocks:
        if block.kind == "heading" and block.level == 1:
            doc.title = block.text
            break
    return doc


def _convert_with_docling(path: Path) -> NormalizedDoc:
    """Converte con Docling, applicando la strategia OCR se ``ocr_enabled``.

    - Immagini: OCR sempre attivo (è l'unico modo di estrarne testo).
    - PDF: prima conversione nativa; se il testo medio per pagina è sotto
      ``_OCR_MIN_CHARS_PER_PAGE`` (tipico delle scansioni), seconda
      conversione con OCR Tesseract (ita+eng).
    - Altri formati: conversione standard (OCR non pertinente).

    Con ``ocr_enabled=False`` (default) il comportamento è quello originale.
    """
    settings = get_settings()
    suffix = path.suffix.lower()
    if settings.ocr_enabled and suffix in _IMAGE_EXTENSIONS:
        return _docling_to_normalized(path, ocr=True)

    doc = _docling_to_normalized(path, ocr=False)
    if settings.ocr_enabled and suffix == ".pdf":
        chars = sum(len(block.text) for block in doc.blocks)
        chars_per_page = chars / (doc.page_count or 1)
        if chars_per_page < _OCR_MIN_CHARS_PER_PAGE:
            log.info(
                "testo nativo insufficiente: riconversione con OCR",
                path=str(path),
                chars_per_page=round(chars_per_page, 1),
            )
            doc = _docling_to_normalized(path, ocr=True)
    return doc


def _make_converter(*, ocr: bool) -> Any:
    """DocumentConverter standard, oppure con pipeline OCR Tesseract (ita+eng).

    Si usa la variante CLI di Tesseract (binario ``tesseract``), che non
    richiede binding Python nativi: nel container è installato via apt.
    """
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover - dipendenza obbligatoria
        raise ConversionError(f"Docling non installato: {exc}") from exc
    if not ocr:
        return DocumentConverter()

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import ImageFormatOption, PdfFormatOption

    ocr_options = TesseractCliOcrOptions(lang=["ita", "eng"])
    pipeline_options = PdfPipelineOptions(do_ocr=True, ocr_options=ocr_options)
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
        }
    )


def _docling_to_normalized(path: Path, *, ocr: bool) -> NormalizedDoc:
    """Converte con Docling e appiattisce il documento in blocchi."""
    converter = _make_converter(ocr=ocr)
    result = converter.convert(str(path))
    dl_doc = result.document

    blocks: list[Block] = []
    headings: dict[int, str] = {}

    def section_path(level: int) -> list[str]:
        return [headings[lvl] for lvl in sorted(headings) if lvl < level]

    def page_of(item: Any) -> int | None:
        """Numero di pagina (1-based) dal primo provenance disponibile."""
        for prov in getattr(item, "prov", []) or []:
            page_no = getattr(prov, "page_no", None)
            if page_no is not None:
                return page_no
        return None

    # Pagine del documento Docling: page_no -> PageItem (dimensioni in punti)
    dl_pages: dict[int, Any] = dict(getattr(dl_doc, "pages", None) or {})

    def bboxes_of(item: Any) -> list[BBox]:
        """Rettangoli dei provenance dell'item, normalizzati bottom-left.

        Per i PDF Docling espone ``prov.bbox`` (punti PDF); altri formati
        tipicamente non hanno bbox e producono una lista vuota.
        """
        out: list[BBox] = []
        for prov in getattr(item, "prov", []) or []:
            bbox = getattr(prov, "bbox", None)
            page_no = getattr(prov, "page_no", None)
            if bbox is None or page_no is None:
                continue
            size = getattr(dl_pages.get(page_no), "size", None)
            width = getattr(size, "width", None)
            height = getattr(size, "height", None)
            origin = str(getattr(getattr(bbox, "coord_origin", None), "value", ""))
            if origin == "TOPLEFT" and height is not None:
                bbox = bbox.to_bottom_left_origin(page_height=height)
            entry: BBox = {
                "page": int(page_no),
                "l": float(bbox.l),
                "t": float(bbox.t),
                "r": float(bbox.r),
                "b": float(bbox.b),
            }
            if width is not None:
                entry["page_width"] = float(width)
            if height is not None:
                entry["page_height"] = float(height)
            out.append(entry)
        return out

    # iterate_items() percorre il documento in ordine di lettura, rispettando
    # la struttura (gruppi, elenchi, tabelle).
    for item, _level in dl_doc.iterate_items():
        label = getattr(getattr(item, "label", None), "value", str(getattr(item, "label", "")))
        page = page_of(item)
        bboxes = bboxes_of(item)

        if label in ("title", "section_header"):
            text = item.text.strip() if hasattr(item, "text") else ""
            if not text:
                continue
            level = 1 if label == "title" else getattr(item, "level", 2) or 2
            for lvl in [lvl for lvl in headings if lvl >= level]:
                del headings[lvl]
            headings[level] = text
            blocks.append(
                Block(kind="heading", text=text, level=level, page=page,
                      section_path=section_path(level), bboxes=bboxes)
            )
        elif label == "list_item":
            text = item.text.strip()
            if text:
                blocks.append(
                    Block(kind="list_item", text=text, page=page,
                          section_path=section_path(99), bboxes=bboxes)
                )
        elif label == "caption":
            text = item.text.strip()
            if text:
                blocks.append(
                    Block(kind="caption", text=text, page=page,
                          section_path=section_path(99), bboxes=bboxes)
                )
        elif label == "table":
            # Serializzazione Markdown: preserva righe e colonne della tabella
            text = _table_to_markdown(item, dl_doc)
            if text:
                blocks.append(
                    Block(kind="table", text=text, page=page,
                          section_path=section_path(99), bboxes=bboxes)
                )
        elif label in ("text", "paragraph"):
            text = item.text.strip()
            if text:
                blocks.append(
                    Block(kind="paragraph", text=text, page=page,
                          section_path=section_path(99), bboxes=bboxes)
                )
        # Altri label (page_header, page_footer, picture, formula...) ignorati

    page_count = None
    pages = getattr(dl_doc, "pages", None)
    if pages:
        page_count = len(pages)

    doc = NormalizedDoc(blocks=blocks, page_count=page_count)
    title = getattr(dl_doc, "name", "") or ""
    if title and title not in ("file", path.name):
        doc.title = title
    return doc


def _table_to_markdown(item: Any, dl_doc: Any) -> str:
    """Serializza una tabella Docling in Markdown (righe/colonne preservate)."""
    try:
        df = item.export_to_dataframe(doc=dl_doc)
    except Exception:
        return getattr(item, "text", "") or ""

    headers = [str(col) for col in df.columns]
    rows = [["" if cell is None else str(cell) for cell in row] for row in df.values]

    def fmt_row(cells: list[str]) -> str:
        escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in cells]
        return "| " + " | ".join(escaped) + " |"

    lines = [fmt_row(headers), "|" + "|".join(" --- " for _ in headers) + "|"]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)
