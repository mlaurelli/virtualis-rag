"""Test del generatore del corpus demo: file creati e apribili, senza ingest."""

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from scripts.demo_seed import EXAMPLE_QUESTIONS, generate_corpus  # noqa: E402


def test_generate_corpus_crea_tutti_i_file(tmp_path) -> None:
    """Genera 10 documenti nei formati previsti, senza errori."""
    created = generate_corpus(tmp_path / "corpus")
    assert len(created) == 10
    suffixes = {p.suffix for p in created}
    assert suffixes == {".docx", ".xlsx", ".pdf", ".md"}
    for path in created:
        assert path.stat().st_size > 0


def test_docx_apribili_e_con_contenuto(tmp_path) -> None:
    """I DOCX si aprono con python-docx e contengono le clausole attese."""
    from docx import Document

    dest = tmp_path / "corpus"
    generate_corpus(dest)

    for name in ("Contratto_Manutenzione_2023.docx", "Contratto_Manutenzione_2025.docx"):
        doc = Document(dest / name)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "garanzia" in text.lower()

    def _read(name: str) -> str:
        return "\n".join(p.text for p in Document(dest / name).paragraphs)

    c2023 = _read("Contratto_Manutenzione_2023.docx")
    c2025 = _read("Contratto_Manutenzione_2025.docx")
    # Le clausole di garanzia devono essere DIVERSE (serve al confronto agentico)
    assert "12 mesi" in c2023 and "24 mesi" in c2025

    verbale = "\n".join(p.text for p in Document(dest / "Verbale_CDA_2024.docx").paragraphs)
    assert "2024-017" in verbale


def test_xlsx_apribili_e_con_tabella(tmp_path) -> None:
    """Gli XLSX si aprono con openpyxl e il listino contiene il sensore SP-220."""
    from openpyxl import load_workbook

    dest = tmp_path / "corpus"
    generate_corpus(dest)

    ws = load_workbook(dest / "Listino_2025.xlsx").active
    rows = [[cell.value for cell in row] for row in ws.iter_rows()]
    assert rows[0][0] == "Codice"
    codici = {row[0] for row in rows[1:]}
    assert "SP-220" in codici and "FLT-100" in codici


def test_domande_di_esempio_presenti() -> None:
    """5 domande pronte: 2 fast e 3 agentiche."""
    assert len(EXAMPLE_QUESTIONS["fast"]) == 2
    assert len(EXAMPLE_QUESTIONS["agentiche"]) == 3
