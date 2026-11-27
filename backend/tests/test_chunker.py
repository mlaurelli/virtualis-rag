"""Test del chunker gerarchico su NormalizedDoc sintetici.

Il conteggio token usa ``method="approx"`` (chars/4) per essere deterministico
e indipendente da tiktoken/rete.
"""

from itertools import pairwise

from app.ingestion.chunker import chunk_document, count_tokens
from app.ingestion.converter import BBox, Block, NormalizedDoc

# Con approx: 1 token = 4 caratteri
TARGET = 60  # 240 caratteri
OVERLAP = 15  # 60 caratteri


def _para(text: str, section: list[str], page: int | None = None) -> Block:
    return Block(kind="paragraph", text=text, section_path=section, page=page)


def _sentence(n: int) -> str:
    """Frase di ~40 caratteri (= 10 token approx), terminata da punto."""
    return f"Frase numero {n:02d} con contenuto di prova."


def _doc_long_section() -> NormalizedDoc:
    """Una sezione unica con tante frasi: forza chunk multipli + overlap."""
    blocks = [
        Block(kind="heading", text="Unica", level=1, section_path=[]),
        _para(" ".join(_sentence(i) for i in range(20)), ["Unica"], page=1),
    ]
    return NormalizedDoc(blocks=blocks, title="Doc lungo")


def test_rispetto_confini_sezione() -> None:
    """Nessun chunk mescola testo di due sezioni diverse."""
    blocks = [
        Block(kind="heading", text="Alfa", level=1, section_path=[]),
        _para("Testo della sezione Alfa. " * 20, ["Alfa"], page=1),
        Block(kind="heading", text="Beta", level=1, section_path=[]),
        _para("Testo della sezione Beta. " * 20, ["Beta"], page=2),
    ]
    doc = NormalizedDoc(blocks=blocks, title="T")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) >= 2
    for chunk in chunks:
        # ogni chunk appartiene a una sola sezione
        assert len(set(chunk.section_path)) <= 1 or chunk.section_path in (
            ["Alfa"], ["Beta"],
        )
        if "Alfa" in chunk.text:
            assert "Beta" not in chunk.text.replace("[Sezione:", "")
    alfa = [c for c in chunks if c.section_path == ["Alfa"]]
    beta = [c for c in chunks if c.section_path == ["Beta"]]
    assert alfa and beta
    for chunk in alfa:
        assert "Beta." not in chunk.text


def test_limite_token() -> None:
    """I chunk testuali non superano il target (le tabelle sono l'eccezione)."""
    doc = _doc_long_section()
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) > 1  # il testo è molto più lungo del target
    for chunk in chunks:
        assert chunk.token_count <= TARGET


def test_overlap_tra_chunk_consecutivi() -> None:
    """Chunk consecutivi della stessa sezione condividono frasi (overlap)."""
    doc = _doc_long_section()
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) > 2
    for prev, curr in pairwise(chunks):
        # l'ultima frase del chunk precedente ricompare all'inizio del successivo
        # (i pezzi del corpo sono separati da "\n\n", non da ". ")
        prev_pieces = [p for p in prev.text.split("\n\n") if p.strip()]
        last = prev_pieces[-1].strip().rstrip(".")
        assert last and last in curr.text
        # nessun taglio a metà parola: il chunk inizia a inizio frase
        body = curr.text.split("]\n\n", 1)[-1]
        assert body[0].isupper() or body[0].isdigit()


def test_tabelle_chunk_dedicati() -> None:
    """Ogni tabella è un chunk a sé con prefisso descrittivo e righe preservate."""
    table_md = "| Nome | Valore |\n| --- | --- |\n| a | 1 |\n| b | 2 |"
    blocks = [
        Block(kind="heading", text="Dati", level=1, section_path=[]),
        _para("Introduzione ai dati.", ["Dati"], page=1),
        Block(kind="table", text=table_md, section_path=["Dati"], page=1),
        _para("Nota finale.", ["Dati"], page=1),
    ]
    doc = NormalizedDoc(blocks=blocks, title="Report vendite")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    table = tables[0]
    # prefisso in linguaggio naturale per la raggiungibilità
    assert "Tabella nel documento «Report vendite»" in table.text
    assert "sezione «Dati»" in table.text
    # struttura righe/colonne preservata
    assert "| Nome | Valore |" in table.text
    assert "| b | 2 |" in table.text
    # gli altri chunk non contengono la tabella
    others = [c for c in chunks if c.kind != "table"]
    assert all("| b | 2 |" not in c.text for c in others)


def test_heading_anteposti_e_marcati() -> None:
    """Il percorso di sezione è anteposto al chunk come riga marcata."""
    blocks = [
        Block(kind="heading", text="Capitolo", level=1, section_path=[]),
        Block(kind="heading", text="Sottosezione", level=2, section_path=["Capitolo"]),
        _para("Contenuto.", ["Capitolo", "Sottosezione"], page=1),
    ]
    doc = NormalizedDoc(blocks=blocks, title="T")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) == 1
    assert chunks[0].text.startswith("[Sezione: Capitolo > Sottosezione]")
    assert "Contenuto." in chunks[0].text


def test_context_text_finestra_estesa() -> None:
    """context_text include il chunk ± 1 adiacente, entro ~2x target."""
    doc = _doc_long_section()
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) > 2
    middle = chunks[len(chunks) // 2]
    i = middle.ord
    # contiene il testo proprio e la coda del chunk precedente
    assert middle.text in middle.context_text
    prev_tail = chunks[i - 1].text[-30:]
    assert any(
        prev_tail[k:] in middle.context_text for k in range(0, 25)
    )
    # limite ~2x target (con margine per il completamento delle frasi)
    assert count_tokens(middle.context_text, method="approx") <= 2 * TARGET + 30
    # primo e ultimo chunk hanno un solo vicino
    assert chunks[0].text in chunks[0].context_text


def test_pagine_corrette() -> None:
    """page_start/page_end riflettono le pagine dei blocchi inclusi."""
    blocks = [
        Block(kind="heading", text="S", level=1, section_path=[]),
        _para("Pagina uno. " * 30, ["S"], page=1),
        _para("Pagina due. " * 30, ["S"], page=2),
    ]
    doc = NormalizedDoc(blocks=blocks, title="T")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.page_start is not None and chunk.page_end is not None
        assert chunk.page_start <= chunk.page_end
        assert 1 <= chunk.page_start <= 2
    # complessivamente le pagine coperte arrivano a 2
    assert max(c.page_end for c in chunks) == 2


def test_frasi_piu_lunghe_del_target_spezzate_per_parole() -> None:
    """Una frase oltre il target è spezzata per parole, mai a metà parola."""
    long_sentence = " ".join(f"parola{i:03d}" for i in range(80)) + "."
    blocks = [
        Block(kind="heading", text="S", level=1, section_path=[]),
        _para(long_sentence, ["S"], page=1),
    ]
    doc = NormalizedDoc(blocks=blocks, title="T")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert len(chunks) >= 2
    joined = " ".join(c.text for c in chunks)
    assert "parola000" in joined and "parola079" in joined


def test_bboxes_unione_multi_rettangolo_deduplicata() -> None:
    """I chunk aggregano i bbox dei block: unione deduplicata, non un rettangolo unico."""
    bb1: BBox = {"page": 1, "l": 10.0, "t": 700.0, "r": 300.0, "b": 680.0}
    bb2: BBox = {"page": 1, "l": 10.0, "t": 670.0, "r": 300.0, "b": 650.0}
    bb_tab: BBox = {"page": 2, "l": 50.0, "t": 500.0, "r": 400.0, "b": 300.0}
    blocks = [
        Block(kind="heading", text="S", level=1, section_path=[]),
        # due frasi dallo stesso block: il bbox conta una volta sola
        Block(kind="paragraph", text="Prima frase. Seconda frase.",
              section_path=["S"], page=1, bboxes=[bb1]),
        Block(kind="paragraph", text="Terza frase.",
              section_path=["S"], page=1, bboxes=[bb2]),
        Block(kind="table", text="| a | b |\n| --- | --- |\n| 1 | 2 |",
              section_path=["S"], page=2, bboxes=[bb_tab]),
    ]
    doc = NormalizedDoc(blocks=blocks, title="T")
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=0, token_method="approx"
    )
    text_chunks = [c for c in chunks if c.kind == "text"]
    table_chunks = [c for c in chunks if c.kind == "table"]
    assert len(text_chunks) == 1
    assert text_chunks[0].bboxes == [bb1, bb2]
    assert len(table_chunks) == 1
    assert table_chunks[0].bboxes == [bb_tab]


def test_bboxes_vuote_per_blocchi_senza_layout() -> None:
    """Blocchi senza bbox (formati non-PDF) → chunk senza coordinate."""
    doc = _doc_long_section()
    chunks = chunk_document(
        doc, target_tokens=TARGET, overlap_tokens=OVERLAP, token_method="approx"
    )
    assert chunks
    assert all(chunk.bboxes == [] for chunk in chunks)
