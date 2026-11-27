"""Test del troncamento consapevole dei testi oltre il budget del modello e5.

Funzione pura (nessun caricamento del modello ONNX): un chunk fuori misura
(es. una tabella mantenuta intera oltre i 512 token dell'e5) non deve
corrompere l'indice né essere troncato in silenzio: il troncamento è
esplicito, su parole intere, e lascia traccia nel log.
"""

import numpy as np
from structlog.testing import capture_logs

from app.ingestion.chunker import count_tokens
from app.ingestion.embeddings import (
    EMBED_TOKEN_BUDGET,
    TITLE_TOKEN_BUDGET,
    Embedder,
    build_passage_text,
    truncate_for_embedding,
)


def test_testo_breve_invariato() -> None:
    """Un testo entro il budget passa inalterato."""
    text = "Un testo breve, ben sotto il budget di token del modello."
    assert truncate_for_embedding(text) == text


def test_testo_lungo_troncato_entro_budget_e_su_parole_intere() -> None:
    """Un testo oltre il budget è troncato entro il budget, senza tagliare
    una parola a metà (il taglio cade su uno spazio del testo originale)."""
    text = " ".join(f"parola{i}" for i in range(5000))
    out = truncate_for_embedding(text)
    assert count_tokens(out) <= EMBED_TOKEN_BUDGET
    assert out.startswith("parola0")
    assert len(out) < len(text)
    assert text[len(out)] == " "  # taglio su confine di parola


def test_troncamento_lascia_traccia_nel_log() -> None:
    """Il troncamento è consapevole: warning nel log con il dettaglio."""
    text = " ".join(["token"] * 3000)
    with capture_logs() as logs:
        truncate_for_embedding(text)
    assert any(
        "budget" in entry.get("event", "") and entry.get("log_level") == "warning"
        for entry in logs
    )


# --- testo indicizzato: titolo + chunk (build_passage_text) -------------------


def test_build_passage_text_antepone_il_titolo() -> None:
    """Il titolo (con il percorso) precede il chunk, separato da newline."""
    out = build_passage_text(
        "RISTOGEST OSTELLO/PLANIMETRIE/Planim mar2026", "tabella di MAC address"
    )
    assert out == "RISTOGEST OSTELLO/PLANIMETRIE/Planim mar2026\ntabella di MAC address"


def test_build_passage_text_senza_titolo_restituisce_solo_il_testo() -> None:
    """Titolo vuoto o solo spazi: il testo embeddato resta il chunk puro."""
    assert build_passage_text("", "testo del chunk") == "testo del chunk"
    assert build_passage_text("   ", "testo del chunk") == "testo del chunk"


def test_build_passage_text_titolo_normalizzato_su_una_riga() -> None:
    """Un titolo con a capo/spazi multipli è compattato su una sola riga:
    il chunk inizia sempre alla seconda riga del testo embeddato."""
    assert build_passage_text("riga1\nriga2  tab", "testo") == "riga1 riga2 tab\ntesto"


def test_build_passage_text_titolo_lungo_troncato_al_budget() -> None:
    """Un titolo oltre TITLE_TOKEN_BUDGET è troncato (senza warning nel log):
    non deve erodere il budget di token riservato al contenuto del chunk."""
    title = "/".join(f"cartella{i}" for i in range(100))
    with capture_logs() as logs:
        out = build_passage_text(title, "testo del chunk")
    first_line, _, rest = out.partition("\n")
    assert rest == "testo del chunk"
    assert count_tokens(first_line) <= TITLE_TOKEN_BUDGET
    assert not any(entry.get("log_level") == "warning" for entry in logs)


def test_budget_copre_chunk_al_target_con_titolo() -> None:
    """Il budget copre un chunk pieno (512 token, target del chunker) più il
    titolo (40): niente troncamento sistematico della coda dei chunk."""
    from app.config import get_settings

    assert get_settings().chunk_target_tokens + TITLE_TOKEN_BUDGET <= EMBED_TOKEN_BUDGET
    # Verifica funzionale: titolo al budget + chunk al target → nessun taglio
    title_words = ["parola"] * 60
    while count_tokens(" ".join(title_words)) > TITLE_TOKEN_BUDGET - 2:
        title_words.pop()
    chunk_words = ["contenuto"] * 400
    while count_tokens(" ".join(chunk_words)) > 508:
        chunk_words.pop()
    full = build_passage_text(" ".join(title_words), " ".join(chunk_words))
    assert count_tokens(full) <= EMBED_TOKEN_BUDGET
    assert truncate_for_embedding(full) == full


# --- prefissi e5 sul testo composto -------------------------------------------


class _FakeDense:
    """Modello denso finto: registra i testi ricevuti (nessun ONNX)."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def embed(self, texts, batch_size=32, **kwargs):
        self.inputs.extend(texts)
        return iter([np.zeros(4, dtype=np.float32) for _ in texts])


def test_prefisso_passage_con_titolo_incluso_nel_testo() -> None:
    """Il prefisso e5 ``passage: `` precede il titolo, che precede il chunk."""
    embedder = Embedder()
    fake = _FakeDense()
    embedder._dense = fake  # type: ignore[assignment]
    embedder.embed_passages([build_passage_text("Titolo/Doc", "testo del chunk")])
    assert fake.inputs == ["passage: Titolo/Doc\ntesto del chunk"]


def test_prefisso_query_invariato() -> None:
    """Il lato query resta puro: prefisso ``query: `` + query, senza titoli."""
    embedder = Embedder()
    fake = _FakeDense()
    embedder._dense = fake  # type: ignore[assignment]
    embedder.embed_queries(["quanti access point e in quali piani"])
    assert fake.inputs == ["query: quanti access point e in quali piani"]
