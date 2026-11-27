"""Configurazione centralizzata del backend Virtualis.

Le impostazioni sono lette dalle variabili d'ambiente e, se presente,
dal file `.env` nella radice del repository.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = structlog.get_logger(__name__)

# Radice del repository: backend/app/config.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Impostazioni dell'applicazione, con override via variabili d'ambiente."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (Kimi / Moonshot, API OpenAI-compatible) ---
    moonshot_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_model: str = "kimi-k3"
    kimi_reasoning_effort: str = "high"
    # Resilienza: timeout per singola richiesta HTTP e retry con backoff
    # esponenziale + jitter su errori transitori (429, 5xx, timeout, rete)
    llm_request_timeout: float = 120.0
    llm_max_retries: int = 4
    llm_retry_base_seconds: float = 1.0
    # Richieste simultanee massime verso il modello (semaforo globale):
    # Ollama serve le richieste in serie, accodarle qui evita che un job
    # lungo (es. estrazione KG) saturi il provider e affami la chat
    llm_max_concurrency: int = 2

    # --- Dati e database ---
    data_dir: Path = Path("./data")
    jwt_secret: str = ""
    jwt_expire_minutes: int = 720

    # --- Qdrant / retrieval (fasi successive) ---
    qdrant_url: str = ""  # vuoto = Qdrant embedded in data/qdrant
    qdrant_collection: str = "chunks"
    dense_model: str = "intfloat/multilingual-e5-large"
    sparse_model: str = "Qdrant/bm25"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_enabled: bool = True
    # Soglie di pertinenza sugli score del reranker (cross-encoder, sigmoid
    # in [0,1]). Si applicano SOLO a score effettivamente rerankati (gli
    # score RRF grezzi vivono su un'altra scala, ~0.01-0.03):
    # - sotto ``retrieval_min_score`` l'agente SI ASTIENE invece di generare
    #   (barriera anti-allucinazione: fonti troppo deboli);
    # - sotto ``retrieval_strong_score`` la selezione passa dal grading di
    #   rilevanza (nodo evaluate) anche sui percorsi fast/ReAct
    # Default calibrato sul corpus reale (26/08): i documenti pertinenti
    # stanno a ~0.2-0.5, quelli fuori tema sotto 0.1 (caso reale: 0.02);
    # 0.30 provocava over-astensione su domande brevi
    retrieval_min_score: float = 0.15
    retrieval_strong_score: float = 0.60
    # Preriscaldamento dei modelli di retrieval all'avvio (in background, non
    # blocca il boot): la prima query utente non paga il caricamento da
    # secondi/minuti. Spegnere su macchine molto deboli
    warmup_on_start: bool = True

    # --- Knowledge graph (GraphRAG) ---
    # Default OFF: l'estrazione di entità/relazioni usa il LLM (una chiamata
    # per documento); si attiva esplicitamente e si popola anche dopo con
    # scripts/kg_build.py
    kg_enabled: bool = False
    kg_max_chars_per_doc: int = 12000  # budget di testo per documento all'estrattore
    kg_max_entities_per_doc: int = 40
    # Uso ATTIVO del grafo nel ciclo agentico (nodo kg_lookup, percorso
    # agentic): profondità dell'espansione delle relazioni e tetto ai
    # documenti extra tirati dentro le ricerche via grafo
    kg_agent_depth: int = 1
    kg_agent_max_extra_docs: int = 3

    # --- Agente ---
    max_agent_iterations: int = 3
    # Budget di token del contesto passato alla sintesi (stima cl100k): oltre
    # questa soglia i passaggi meno rilevanti sono tagliati prima della
    # chiamata. Default dimensionato sui modelli locali (Ollama ~16k di
    # contesto: prompt + storia + KG + margine di generazione)
    synthesize_max_context_tokens: int = 10000
    # Messaggi recenti della sessione passati all'agente come contesto
    # conversazionale (risoluzione dei riferimenti e continuità del dialogo)
    chat_history_messages: int = 6
    # Reranker nel ciclo agentico (caricamento lazy, ~1-2 GB al primo uso):
    # spegnere su macchine deboli; il retrieval resta funzionante (ordine RRF)
    agent_rerank_enabled: bool = True

    # --- Architettura agentica v2 (multi-agente) ---
    # "react" (default): sul percorso agentic un research agent ReAct con
    # tool-calling (function calling OpenAI) sostituisce retrieve/evaluate;
    # "graph": grafo a stati fissi classico, immutato
    agent_mode: str = "react"
    # Round massimi del loop ReAct (una chiamata LLM + gli strumenti invocati)
    react_max_rounds: int = 6
    # HyDE: prima di ogni ricerca il LLM scrive un passaggio ipotetico di
    # risposta e si fondono (max-score) i risultati delle due ricerche —
    # riduce il mismatch lessicale tra domanda e documenti
    hyde_enabled: bool = True
    # CRAG: il nodo evaluate classifica ogni passaggio top come
    # rilevante/parziale/irrilevante: gli irrilevanti sono scartati, i
    # parziali tenuti con peso minore
    crag_enabled: bool = True
    # Critic avversariale: dopo la verifica di aderenza, un secondo prompt
    # tenta di confutare la risposta; il verdict diventa `confidence`
    # (alta/media/bassa) esposta nell'evento SSE `answer`
    critic_enabled: bool = True
    # Reflexion: se la critic giudica la risposta "fragile" (confidence
    # bassa), il grafo la RIGENERA una sola volta passando le criticità
    # trovate come feedback esplicito, poi la riverifica. Richiede la critic
    # attiva; flag spento → comportamento precedente
    reflexion_enabled: bool = True
    # Concorrenza massima delle ricerche del nodo retrieve (interrogazioni
    # pianificate eseguite in parallelo, ciascuna con la propria sessione DB)
    retrieve_max_concurrency: int = 3

    # --- Frontend (produzione) ---
    # Path del build React servito come SPA (relativo alla cartella backend/)
    frontend_dist: str = "../frontend/dist"

    # --- Ingestion (fasi successive) ---
    upload_dir: Path = Path("./data/uploads")
    upload_max_mb: int = 50  # dimensione massima di un file caricato via API
    chunk_target_tokens: int = 512
    chunk_overlap_tokens: int = 64
    ocr_enabled: bool = False
    # Conversione in sottoprocesso isolato: la conversione Docling gira in un
    # processo figlio persistente con limite di memoria proprio (RLIMIT_AS),
    # così un documento patologico (planimetria raster, PDF enorme) fallisce
    # da solo invece di far uccidere l'intera app dall'OOM killer del container
    convert_in_subprocess: bool = True
    convert_max_memory_mb: int = 6144
    # Riciclo del worker di conversione ogni N documenti: il figlio accumula
    # RSS (torch/Docling non restituiscono la memoria) e sotto carico spinge
    # il container contro il mem_limit (malloc nativi falliti). Col riciclo
    # periodico la memoria del figlio torna al sistema. 0 = mai (persistente)
    convert_worker_max_tasks: int = 8

    # --- HTTP ---
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    # Anti brute-force sul login: dopo `login_max_attempts` fallimenti nella
    # finestra `login_window_seconds` (per coppia IP+username) → 429.
    # Contatore in memoria del processo: adeguato al dispiegamento a nodo
    # singolo del prototipo
    login_max_attempts: int = 5
    login_window_seconds: int = 300

    # --- TLS (HTTPS su LAN) ---
    # Entrambi valorizzati = HTTPS (vedi scripts/gen_cert.py); vuoti = HTTP
    tls_cert_file: str = ""
    tls_key_file: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Permette di definire CORS_ORIGINS come stringa separata da virgole."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("agent_mode", mode="before")
    @classmethod
    def _normalize_agent_mode(cls, value: object) -> object:
        """Normalizza AGENT_MODE: valori ignoti → "graph" (percorso classico)."""
        mode = str(value or "").strip().lower()
        if mode not in ("react", "graph"):
            log.warning("agent_mode non riconosciuto: ripiego su 'graph'", valore=value)
            return "graph"
        return mode

    @model_validator(mode="after")
    def _default_jwt_secret(self) -> "Settings":
        """Risolvi il segreto JWT se non configurato.

        Se ``jwt_secret`` è vuoto, il segreto è caricato da
        ``data_dir/.jwt_secret`` o, al primo avvio, generato e persistito lì
        (permessi 600): i token emessi sopravvivono ai riavvii. Un
        ``JWT_SECRET`` esplicito da env/.env ha sempre precedenza e non
        tocca il file. Se la persistenza fallisce (permessi), si ripiega su
        un segreto effimero con avviso.
        """
        if self.jwt_secret:
            return self
        import secrets

        secret_file = self.data_dir / ".jwt_secret"
        try:
            if secret_file.is_file():
                persisted = secret_file.read_text(encoding="utf-8").strip()
                if persisted:
                    self.jwt_secret = persisted
                    return self
            self.jwt_secret = secrets.token_urlsafe(48)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.jwt_secret + "\n")
            log.warning(
                "jwt_secret non configurato: generato e persistito in "
                f"{secret_file}. Impostare JWT_SECRET in .env per gestirlo "
                "esplicitamente."
            )
        except OSError as exc:
            if not self.jwt_secret:
                self.jwt_secret = secrets.token_urlsafe(48)
            log.warning(
                "jwt_secret non configurato e persistenza impossibile: segreto "
                "effimero, i token non sopravviveranno al riavvio. Impostare "
                "JWT_SECRET in .env.",
                error=str(exc),
            )
        return self

    @property
    def frontend_dist_path(self) -> Path:
        """Path assoluto del build frontend (relativo alla cartella backend/)."""
        path = Path(self.frontend_dist)
        if not path.is_absolute():
            path = (REPO_ROOT / "backend" / path).resolve()
        return path

    @property
    def database_url(self) -> str:
        """URL SQLAlchemy async per SQLite (file dentro data_dir)."""
        return f"sqlite+aiosqlite:///{(self.data_dir / 'virtualis.db').resolve()}"

    @property
    def llm_configured(self) -> bool:
        """True se la chiave API Moonshot è impostata."""
        return bool(self.moonshot_api_key)


@lru_cache
def get_settings() -> Settings:
    """Restituisce il singleton delle impostazioni (cacheato)."""
    return Settings()
