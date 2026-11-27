"""Embedding densi e sparsi basati su fastembed (esecuzione locale, ONNX).

- Denso: ``intfloat/multilingual-e5-large`` (da settings). I modelli E5
  richiedono il prefisso ``"passage: "`` per i documenti e ``"query: "``
  per le query: usare sempre :meth:`Embedder.embed_passages` e
  :meth:`Embedder.embed_queries`, mai l'embedding grezzo.
- Sparso: ``Qdrant/bm25`` (da settings). BM25 non richiede prefissi.

I pesi dei modelli sono scaricati da HuggingFace al primo uso (una tantum,
con log esplicito) e tenuti in cache da fastembed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import structlog

from ..config import Settings, get_settings

if TYPE_CHECKING:
    from fastembed import SparseTextEmbedding, TextEmbedding
    from fastembed.common.types import NumpyArray

log = structlog.get_logger(__name__)

# Prefissi richiesti dalla famiglia E5
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "

# Budget di token per l'embedding denso, espresso in token cl100k (stima).
# Limite reale del modello e5: 512 token del SUO tokenizer (XLM-R), di cui
# 2 speciali e ~3 per il prefisso "passage: " → ~507 token di contenuto.
# Rapporto misurato token-e5/token-cl100k sullo stesso testo: ~0,76 su prosa
# italiana, ~0,95 su tabelle di MAC address/numeri (caso peggiore). Con
# 552 = 512 (chunk al target del chunker) + 40 (titolo, TITLE_TOKEN_BUDGET)
# un chunk standard con titolo non viene MAI pre-troncato qui (in prosa vale
# ~420 token e5, ampio margine); nel caso peggiore tabellare il tokenizer
# del modello tronca da sé al massimo ~3% di coda. Il troncamento esplicito
# resta per i chunk fuori misura (es. tabelle mantenute intere oltre il
# target). Il vecchio valore 460 troncava la coda di quasi ogni chunk pieno.
EMBED_TOKEN_BUDGET = 552

# Token (cl100k) riservati al titolo anteposto al testo indicizzato
TITLE_TOKEN_BUDGET = 40


def truncate_for_embedding(
    text: str, budget: int = EMBED_TOKEN_BUDGET, *, warn: bool = True
) -> str:
    """Tronca ``text`` al budget di token, su confine di parola, con log.

    Un chunk fuori misura (es. una tabella mantenuta intera oltre il target)
    non deve corrompere l'indice né essere troncato in silenzio dal modello:
    il troncamento è esplicito e lascia traccia. Il conteggio usa cl100k
    (stima conservativa rispetto al tokenizer del modello, vedi il margine).
    Con ``warn=False`` il taglio è silenzioso (usato per i titoli, dove il
    troncamento è fisiologico e un warning per chunk sarebbe solo rumore).
    """
    from .chunker import count_tokens

    if count_tokens(text) <= budget:
        return text
    words = text.split()
    # Ricerca binaria sul numero di parole entro il budget
    lo, hi = 0, len(words)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(" ".join(words[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    if warn:
        log.warning(
            "testo oltre il budget di token del modello di embedding: troncato",
            budget=budget,
            parole_mantenute=lo,
            parole_totali=len(words),
        )
    return " ".join(words[:lo])


def build_passage_text(title: str, text: str) -> str:
    """Compone il testo da indicizzare (denso E sparso): titolo + chunk.

    Il titolo del documento (che per local_fs/M365 contiene il percorso
    relativo) rende recuperabili i chunk il cui testo non menziona il proprio
    contesto (es. tabelle di soli MAC address in una planimetria). Riguarda
    SOLO il testo embeddato: payload Qdrant e DB conservano il testo pulito
    per citazioni e snippet. Il titolo è normalizzato su una riga e troncato
    a :data:`TITLE_TOKEN_BUDGET` token per non erodere il budget del chunk;
    con l'e5 va DOPO il prefisso ``passage: `` (ci pensa ``embed_passages``,
    che riceve il testo già composto).
    """
    title = " ".join((title or "").split())
    if not title:
        return text
    title = truncate_for_embedding(title, TITLE_TOKEN_BUDGET, warn=False)
    return f"{title}\n{text}"


class Embedder:
    """Wrapper lazy/singleton attorno ai modelli fastembed densi e sparsi."""

    _instance: Embedder | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._dense: TextEmbedding | None = None
        self._sparse: SparseTextEmbedding | None = None
        self._dense_dim: int | None = None

    @classmethod
    def get(cls) -> Embedder:
        """Restituisce il singleton (i modelli sono caricati al primo uso)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Azzera il singleton (usato dai test con settings diverse)."""
        cls._instance = None

    @classmethod
    def is_loaded(cls) -> bool:
        """True se i modelli sono già caricati in memoria.

        NON carica nulla: serve agli healthcheck, che non devono pagare il
        caricamento dei modelli (1-2 GB, lazy al primo uso).
        """
        return cls._instance is not None and cls._instance._dense is not None

    # --- caricamento lazy ---

    def _load_dense(self) -> TextEmbedding:
        if self._dense is None:
            from fastembed import TextEmbedding

            log.info(
                "caricamento modello denso (download al primo uso, può richiedere minuti)",
                model=self._settings.dense_model,
            )
            self._dense = TextEmbedding(model_name=self._settings.dense_model)
            log.info("modello denso pronto", model=self._settings.dense_model)
        return self._dense

    def _load_sparse(self) -> SparseTextEmbedding:
        if self._sparse is None:
            from fastembed import SparseTextEmbedding

            log.info(
                "caricamento modello sparso (download al primo uso)",
                model=self._settings.sparse_model,
            )
            self._sparse = SparseTextEmbedding(model_name=self._settings.sparse_model)
            log.info("modello sparso pronto", model=self._settings.sparse_model)
        return self._sparse

    # --- API pubblica ---

    @property
    def dense_dim(self) -> int:
        """Dimensione del vettore denso (carica il modello se necessario)."""
        if self._dense_dim is None:
            probe = next(iter(self._load_dense().embed(["probe"])))
            self._dense_dim = int(np.asarray(probe).shape[0])
        return self._dense_dim

    def embed_passages(self, texts: list[str], *, batch_size: int = 32) -> NumpyArray:
        """Embedding denso dei documenti, con prefisso ``passage: `` (E5)."""
        prefixed = [PASSAGE_PREFIX + truncate_for_embedding(text) for text in texts]
        vectors = list(self._load_dense().embed(prefixed, batch_size=batch_size))
        return np.asarray(vectors, dtype=np.float32)

    def embed_queries(self, texts: list[str], *, batch_size: int = 32) -> NumpyArray:
        """Embedding denso delle query, con prefisso ``query: `` (E5)."""
        prefixed = [QUERY_PREFIX + truncate_for_embedding(text) for text in texts]
        vectors = list(self._load_dense().embed(prefixed, batch_size=batch_size))
        return np.asarray(vectors, dtype=np.float32)

    def embed_sparse(
        self, texts: list[str], *, batch_size: int = 32
    ) -> list[dict[str, list]]:
        """Embedding sparsi BM25 (nessun prefisso richiesto).

        Ritorna una lista di ``{"indices": [...], "values": [...]}`` pronta
        per i vettori sparsi di Qdrant.
        """
        results = list(self._load_sparse().embed(texts, batch_size=batch_size))
        return [
            {
                "indices": [int(i) for i in sparse.indices],
                "values": [float(v) for v in sparse.values],
            }
            for sparse in results
        ]
