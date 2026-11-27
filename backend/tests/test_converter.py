"""Test del convertitore: DOCX (via Docling) e Markdown (lettura diretta)."""

import shutil
import subprocess
from pathlib import Path

import pytest
from docx import Document as DocxDocument

from app.config import get_settings
from app.ingestion import converter as converter_module
from app.ingestion.converter import (
    Block,
    ConversionError,
    NormalizedDoc,
    convert_document,
)


@pytest.fixture
def sample_docx(tmp_path) -> Path:
    """DOCX con heading annidati, paragrafi e una tabella 2x2."""
    path = tmp_path / "esempio.docx"
    doc = DocxDocument()
    doc.add_heading("Introduzione", level=1)
    doc.add_paragraph("Questo è il primo paragrafo dell'introduzione.")
    doc.add_heading("Dettagli tecnici", level=2)
    doc.add_paragraph("Un paragrafo con dettagli tecnici rilevanti.")
    table = doc.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.cell(0, 0).text = "Nome"
    table.cell(0, 1).text = "Valore"
    table.cell(1, 0).text = "alpha"
    table.cell(1, 1).text = "42"
    doc.add_paragraph("Conclusione dopo la tabella.")
    doc.save(path)
    return path


def test_convert_docx_blocks(sample_docx: Path) -> None:
    """Il DOCX produce blocchi dei tipi attesi, in ordine di lettura."""
    doc = convert_document(sample_docx, doc_meta={})
    kinds = [block.kind for block in doc.blocks]
    assert "heading" in kinds
    assert "paragraph" in kinds
    assert "table" in kinds
    # la tabella compare dopo i paragrafi che la precedono nel documento
    assert kinds.index("table") > kinds.index("paragraph")
    texts = [block.text for block in doc.blocks]
    assert any("Introduzione" in text for text in texts)
    assert any("primo paragrafo" in text for text in texts)


def test_convert_docx_section_path(sample_docx: Path) -> None:
    """Il percorso di sezione riflette la gerarchia dei titoli."""
    doc = convert_document(sample_docx, doc_meta={})
    details = next(
        block for block in doc.blocks if "dettagli tecnici rilevanti" in block.text
    )
    assert details.section_path == ["Introduzione", "Dettagli tecnici"]
    intro_para = next(
        block for block in doc.blocks if "primo paragrafo" in block.text
    )
    assert intro_para.section_path == ["Introduzione"]


def test_convert_docx_table_preserves_cells(sample_docx: Path) -> None:
    """La tabella è serializzata preservando righe e colonne."""
    doc = convert_document(sample_docx, doc_meta={})
    tables = [block for block in doc.blocks if block.kind == "table"]
    assert len(tables) == 1
    text = tables[0].text
    assert "Nome" in text and "Valore" in text
    assert "alpha" in text and "42" in text
    # almeno due righe (header + una riga dati)
    assert len([line for line in text.splitlines() if line.strip()]) >= 2
    # la tabella eredita il percorso di sezione corrente
    assert tables[0].section_path == ["Introduzione", "Dettagli tecnici"]


def test_convert_docx_metadata(sample_docx: Path) -> None:
    """Metadati: titolo dal doc_meta, doc_type, modified_at dal file."""
    doc = convert_document(sample_docx, doc_meta={"title": "Titolo custom"})
    assert doc.title == "Titolo custom"
    assert doc.doc_type == "docx"
    assert doc.modified_at is not None


def test_convert_markdown(tmp_path) -> None:
    """Il Markdown è letto direttamente con gerarchia di heading."""
    path = tmp_path / "nota.md"
    path.write_text(
        "# Manuale\n\nPrima parte del manuale.\n\n"
        "## Installazione\n\nPassi di installazione.\n\n"
        "### Requisiti\n\nElenco dei requisiti minimi.\n",
        encoding="utf-8",
    )
    doc = convert_document(path, doc_meta={})
    assert doc.title == "Manuale"  # dal primo heading H1
    headings = [b for b in doc.blocks if b.kind == "heading"]
    assert [h.level for h in headings] == [1, 2, 3]
    paragraphs = [b for b in doc.blocks if b.kind == "paragraph"]
    assert paragraphs[0].section_path == ["Manuale"]
    assert paragraphs[1].section_path == ["Manuale", "Installazione"]
    assert paragraphs[2].section_path == ["Manuale", "Installazione", "Requisiti"]


def test_convert_txt(tmp_path) -> None:
    """I file .txt diventano paragrafi semplici."""
    path = tmp_path / "note.txt"
    path.write_text("Prima riga di testo.\n\nSecondo paragrafo.\n", encoding="utf-8")
    doc = convert_document(path, doc_meta={})
    assert [b.kind for b in doc.blocks] == ["paragraph", "paragraph"]
    assert doc.blocks[0].text == "Prima riga di testo."


def test_convert_missing_file(tmp_path) -> None:
    """File inesistente → ConversionError con messaggio utile."""
    with pytest.raises(ConversionError, match="non trovato"):
        convert_document(tmp_path / "assente.pdf", doc_meta={})


# --- PDF reale: bbox dei provenance -------------------------------------------


def _write_minimal_pdf(path: Path, lines: list[str]) -> None:
    """PDF minimale a una pagina (Helvetica, MediaBox A4), stile demo_seed."""
    stream_lines = ["BT", "/F1 11 Tf", "14 TL", "50 800 Td"]
    stream_lines.append(f"({lines[0]}) Tj")
    for line in lines[1:]:
        stream_lines.append("T*")
        stream_lines.append(f"({line}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1")
    objects = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>"),
        (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"),
        (4, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"),
        (5, b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    size = max(offsets) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for n in range(1, size):
        out += f"{offsets[n]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def test_convert_pdf_reale_bboxes(tmp_path) -> None:
    """Un PDF reale produce block con bbox in punti, origine bottom-left.

    Conversione Docling reale (~secondi): verifica le coordinate contro le
    dimensioni di pagina esposte nei bbox stessi.
    """
    path = tmp_path / "prova.pdf"
    _write_minimal_pdf(path, [
        "Prima riga del documento di prova.",
        "Seconda riga con altre parole di test.",
    ])
    doc = convert_document(path, doc_meta={})
    assert doc.page_count == 1
    assert doc.blocks
    for block in doc.blocks:
        assert block.page == 1
        assert block.bboxes, f"block senza bbox: {block.text[:40]}"
        for bbox in block.bboxes:
            assert bbox["page"] == 1
            width = bbox["page_width"]
            height = bbox["page_height"]
            assert 0 <= bbox["l"] < bbox["r"] <= width
            assert 0 <= bbox["b"] < bbox["t"] <= height


def test_convert_markdown_senza_bboxes(tmp_path) -> None:
    """Formati senza layout (Markdown): bboxes sempre vuota."""
    path = tmp_path / "nota.md"
    path.write_text("# Titolo\n\nTesto semplice.\n", encoding="utf-8")
    doc = convert_document(path, doc_meta={})
    assert doc.blocks
    assert all(block.bboxes == [] for block in doc.blocks)


# --- OCR (settings.ocr_enabled) -------------------------------------------------


def _fake_docling_convert(calls: list[bool], native_text: str, ocr_text: str):
    """Conversione Docling finta: registra il flag ``ocr`` e rende testi noti."""

    def fake(path, *, ocr):
        calls.append(ocr)
        text = ocr_text if ocr else native_text
        return NormalizedDoc(
            blocks=[Block(kind="paragraph", text=text)], page_count=2
        )

    return fake


def test_ocr_disabilitato_mai_riconverte(tmp_settings, tmp_path, monkeypatch) -> None:
    """ocr_enabled=False (default): comportamento originale, mai OCR."""
    path = tmp_path / "scansione.pdf"
    path.write_bytes(b"%PDF finto")
    calls: list[bool] = []
    monkeypatch.setattr(
        converter_module,
        "_docling_to_normalized",
        _fake_docling_convert(calls, native_text="poco", ocr_text="tanto"),
    )
    doc = convert_document(path, doc_meta={})
    assert calls == [False]
    assert doc.blocks[0].text == "poco"


def test_pdf_sotto_soglia_riconverte_con_ocr(tmp_settings, tmp_path, monkeypatch) -> None:
    """PDF con testo nativo sotto soglia → seconda conversione con OCR."""
    path = tmp_path / "scansione.pdf"
    path.write_bytes(b"%PDF finto")
    calls: list[bool] = []
    monkeypatch.setattr(
        converter_module,
        "_docling_to_normalized",
        _fake_docling_convert(calls, native_text="poco", ocr_text="testo OCR " * 30),
    )
    monkeypatch.setenv("OCR_ENABLED", "true")
    get_settings.cache_clear()
    doc = convert_document(path, doc_meta={})
    assert calls == [False, True]
    assert "testo OCR" in doc.blocks[0].text


def test_pdf_sopra_soglia_non_riconverte(tmp_settings, tmp_path, monkeypatch) -> None:
    """PDF con testo nativo sufficiente → nessuna seconda conversione."""
    path = tmp_path / "digitale.pdf"
    path.write_bytes(b"%PDF finto")
    calls: list[bool] = []
    native = "testo digitale sufficiente " * 20  # > 100 caratteri/pagina
    monkeypatch.setattr(
        converter_module,
        "_docling_to_normalized",
        _fake_docling_convert(calls, native_text=native, ocr_text="ocr"),
    )
    monkeypatch.setenv("OCR_ENABLED", "true")
    get_settings.cache_clear()
    convert_document(path, doc_meta={})
    assert calls == [False]


def test_immagine_con_ocr_sempre_attivo(tmp_settings, tmp_path, monkeypatch) -> None:
    """Immagini: OCR direttamente attivo, senza conversione nativa preliminare."""
    path = tmp_path / "foto.png"
    path.write_bytes(b"PNG finto")
    calls: list[bool] = []
    monkeypatch.setattr(
        converter_module,
        "_docling_to_normalized",
        _fake_docling_convert(calls, native_text="", ocr_text="testo OCR"),
    )
    monkeypatch.setenv("OCR_ENABLED", "true")
    get_settings.cache_clear()
    doc = convert_document(path, doc_meta={})
    assert calls == [True]
    assert doc.blocks[0].text == "testo OCR"


def test_ocr_reale_su_immagine(tmp_settings, tmp_path, monkeypatch) -> None:
    """OCR end-to-end con Tesseract reale (skippato se binario/lingue assenti)."""
    pytest.importorskip("PIL")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract non installato")
    langs = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, check=False
    ).stdout
    if not {"ita", "eng"} <= set(langs.split()):
        pytest.skip("lingue tesseract ita/eng non installate")

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 300), "white")
    ImageDraw.Draw(img).text(
        (40, 120),
        "Rimborso spese 2024",
        fill="black",
        font=ImageFont.load_default(size=48),
    )
    path = tmp_path / "scansione.png"
    img.save(path)

    monkeypatch.setenv("OCR_ENABLED", "true")
    get_settings.cache_clear()
    doc = convert_document(path, doc_meta={})
    assert "rimborso" in " ".join(b.text for b in doc.blocks).lower()


def test_file_non_trovato_errore_senza_path_interno(tmp_path) -> None:
    """ConversionError per file mancante: il messaggio (mostrato via API nel
    campo ``error`` del documento) contiene solo il nome, non il path interno."""
    missing = tmp_path / "cartella-riservata" / "documento.md"
    with pytest.raises(ConversionError) as exc_info:
        convert_document(missing)
    assert str(tmp_path) not in str(exc_info.value)
    assert "documento.md" in str(exc_info.value)
