# Virtualis — Guida tecnica al backend

Guida al codice del backend FastAPI (`backend/app/`), pensata per uno
sviluppatore che deve metterci le mani: responsabilità di ogni modulo, flussi
di controllo, modello dati, configurazione, mappa delle API, ciclo di vita
dell'applicazione e organizzazione dei test.

Tutti i riferimenti citano path e nomi esatti così come appaiono nel codice.

---

## 1. Panoramica del package e convenzioni

Il package è `app` (cartella `backend/app/`), installato come
`virtualis-backend` (vedi `backend/pyproject.toml`, Python >=3.11 <3.13).
Struttura:

```
backend/app/
├── __main__.py          # entry point `python -m app` (uvicorn, TLS opzionale)
├── config.py            # Settings pydantic-settings (env + .env)
├── db.py                # engine/session factory SQLAlchemy async (SQLite)
├── models.py            # ORM SQLAlchemy 2.0 (tutte le tabelle)
├── auth.py              # argon2, JWT, dipendenze auth, router /api/auth
├── llm.py               # KimiClient: client OpenAI-compatible con retry/semaforo
├── evaluation.py        # scoring della batteria di valutazione (funzione pura)
├── warmup.py            # preriscaldamento modelli retrieval all'avvio
├── reindex.py           # job di reindicizzazione Qdrant dai chunk in DB
├── m365_import.py       # job di import Microsoft 365 (Graph API)
├── mcp_server.py        # server MCP "virtualis-docs" (fastmcp)
├── api/                 # app FastAPI, dipendenze e router REST
├── agent/               # grafo LangGraph, research agent ReAct, tools, citazioni
├── ingestion/           # pipeline: conversione, chunking, embedding, indice
├── retrieval/           # ricerca ibrida Qdrant, reranker, servizio retrieve()
├── knowledge/           # knowledge graph documentale (GraphRAG)
└── connectors/          # sorgenti documentali: local_fs, upload, gdrive, m365
```

Convenzioni trasversali:

- **Logging**: `structlog` ovunque (`log = structlog.get_logger(__name__)`),
  eventi in italiano con campi strutturati.
- **DB async**: SQLAlchemy 2.0 con `AsyncSession` su `aiosqlite`. Niente lazy
  loading nel contesto async: le rimozioni in cascata sono esplicite
  (`delete(Chunk).where(...)`) e le relazioni ORM non vengono navigate in
  modo implicito.
- **Configurazione**: un unico oggetto `Settings` (pydantic-settings) letto
  da env e da `.env` nella radice del repo; accesso tramite il singleton
  cacheato `get_settings()` (`@lru_cache`). I test lo azzerano con
  `get_settings.cache_clear()`.
- **Docstring e commenti in italiano**, come tutto il codice del progetto.
- **Singleton lazy per le risorse pesanti**: `Embedder.get()`,
  `Reranker.get()`, `get_kimi_client()`, `pipeline._get_index()` — i modelli
  (1-2 GB) sono caricati solo al primo uso reale.
- **Resilienza degradata**: ogni arricchimento opzionale (KG, HyDE, reranker,
  warmup) fallisce in warning e mai in eccezione verso il flusso principale.
- **Funzioni `reset_*` / `reset_state`** esposte da quasi tutti i moduli con
  stato globale, pensate per i test.

---

## 2. Moduli del package

### 2.1 `config.py` — configurazione centralizzata

`class Settings(BaseSettings)` con `SettingsConfigDict(env_file=REPO_ROOT /
".env", extra="ignore")`; `REPO_ROOT` è calcolato da
`Path(__file__).resolve().parents[2]`.

Elementi rilevanti oltre ai campi (elencati in §4):

- `_split_cors_origins` (validator): accetta `CORS_ORIGINS` come stringa
  separata da virgole.
- `_normalize_agent_mode` (validator): valori ignoti di `AGENT_MODE` →
  `"graph"` con warning.
- `_default_jwt_secret` (model validator): se `jwt_secret` è vuoto, legge o
  genera e persiste il segreto in `data_dir/.jwt_secret` (permessi 600); in
  caso di fallimento ripiega su un segreto effimero con warning.
- Proprietà: `frontend_dist_path` (path assoluto del build React),
  `database_url` (`sqlite+aiosqlite:///<data_dir>/virtualis.db`),
  `llm_configured` (bool, `moonshot_api_key` valorizzata).
- `get_settings()` — singleton `@lru_cache`.

### 2.2 `db.py` — engine e sessioni

- `_set_sqlite_pragmas` (listener sull'evento `connect`): `journal_mode=WAL`,
  `busy_timeout=5000`, `synchronous=NORMAL`. NOTA importante: `foreign_keys`
  resta volutamente OFF — il modello dati conserva per design riferimenti
  "penzolanti" (audit log e feedback sopravvivono alla cancellazione di
  sessioni/messaggi).
- `get_engine()` / `get_session_factory()` — globali lazy; la factory usa
  `expire_on_commit=False`.
- `get_db()` — dependency FastAPI (una sessione per richiesta).
- `init_db()` — `create_all` + `_apply_additive_columns`, che applica gli
  ALTER TABLE additivi (`chunks.bboxes_json`) e crea l'indice
  `ix_documents_updated_at` sui DB preesistenti (non ci sono migrazioni).
- `reset_engine()` — azzera engine e factory (usato dai test).

### 2.3 `models.py` — modello dati ORM

Vedi §3 per il dettaglio di tutte le tabelle. Definisce inoltre gli enum
stringa: `UserRole` (admin/user), `SourceType` (local_fs/upload/gdrive/m365),
`DocumentStatus` (pending/processing/indexed/failed), `MessageRole`
(user/assistant), `AgentPath` (fast/agentic), tutti basati su una
`StrEnum(str, enum.Enum)` locale.

### 2.4 `auth.py` — autenticazione e router `/api/auth`

- Hash password: `pwdlib.PasswordHash.recommended()` (argon2);
  `hash_password(password)`, `verify_password(password, hashed)`.
  `_DUMMY_PASSWORD_HASH` è verificato al login quando lo username non esiste
  (tempi uniformi, anti enumerazione).
- JWT HS256: `create_access_token(user)` (claim `sub`=user id, `role`,
  scadenza da `jwt_expire_minutes`), `decode_access_token(token)` (401 se
  invalido).
- Dipendenze FastAPI: `get_current_user` (bearer via `OAuth2PasswordBearer`
  con `auto_error=False`; ricontrolla `is_active` a ogni richiesta → revoca
  immediata) e `require_admin` (403 se non admin).
- `bootstrap_admin(db)`: crea il primo admin solo se non esiste alcun utente
  E (`ADMIN_AUTO_CREATE=true` oppure `ADMIN_USERNAME`+`ADMIN_PASSWORD`
  impostate); altrimenti il primo admin si crea dal wizard `/api/setup`.
- Anti brute-force in memoria: `_login_failures` (dict `"ip|username"` →
  `deque` di timestamp), `check_login_throttle(key)` (429 con `Retry-After`
  oltre `login_max_attempts` in `login_window_seconds`),
  `record_login_failure(key)`, `_throttle_key(request, username)`.
- Router `router = APIRouter(prefix="/api/auth")`:
  - `POST /api/auth/login` — JSON o form-encoded; restituisce `TokenOut`
    (`access_token`, `token_type`, `user`).
  - `GET /api/auth/me` — profilo dell'utente autenticato.

### 2.5 `llm.py` — client LLM (OpenAI-compatible)

`class KimiClient`: wrapper su `AsyncOpenAI` verso un endpoint
OpenAI-compatible (Kimi/Moonshot in cloud, Ollama in produzione). Retry del
SDK disattivati (`max_retries=0`): la politica è interna.

- `_get_llm_semaphore()` — semaforo globale (`llm_max_concurrency`, default
  2) attraversato da OGNI chiamata al modello, streaming incluso per l'intera
  durata dello stream. `reset_llm_semaphore()` per i test.
- Politica di retry: `_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}`,
  `_BALANCE_MARKERS = ("insufficient balance", "suspended")` (permanente
  anche con 429 → `LLMInsufficientBalanceError`), header `Retry-After`
  onorato (`_retry_after_seconds`), backoff esponenziale + jitter
  (`_backoff_seconds`), decisione in `_handle_retryable_error` (fino a
  `llm_max_retries` tentativi).
- `chat(messages, *, temperature=0.2, json_mode=False, max_tokens=None) -> str`
  — completamento non-streaming.
- `chat_with_tools(messages, tools, ...) -> tuple[str, list[ToolCall]]` —
  function calling OpenAI (`tool_choice="auto"`); gli `arguments` JSON sono
  decodificati (`None` se malformati). `ToolCall` è un TypedDict
  `{id, name, arguments}`.
- `chat_stream(messages, ...) -> AsyncIterator[str]` — yield dei delta; il
  retry è possibile solo prima del primo token.
- `_build_kwargs`: aggiunge `extra_body={"reasoning_effort": ...}` solo se
  `kimi_reasoning_effort` è valorizzato (vuoto con Ollama).
- Eccezioni: `LLMNotConfiguredError` (manca `MOONSHOT_API_KEY`; mappata a 503
  da un handler dell'app), `LLMInsufficientBalanceError`.
- `extract_json(text)` — parsing tollerante: rimuove code fence, poi
  fallback sul primo blocco `{...}` bilanciato.
- `get_kimi_client()` — singleton `@lru_cache`.

### 2.6 Package `api/`

#### 2.6.1 `api/main.py` — applicazione FastAPI

`create_app()` factory: CORS da `cors_origins`, middleware `security_headers`
(`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Permissions-Policy`, CSP moderata), exception handler
`LLMNotConfiguredError` → 503, registrazione di tutti i router, mount del
build frontend con `SPAStaticFiles` (fallback SPA su `index.html`).

- `lifespan(app)`: `init_db()` → `bootstrap_admin()` →
  `pipeline.start_worker()` → task background `_resume_ingest_safely()`
  (resume dell'ingest) e `_run_warmup()` (se `warmup_on_start`). Allo
  shutdown: `stop_worker()`, `convert_worker.shutdown()` (in thread),
  cancellazione dei task background. Entrambi i wrapper "safely" non sollevano
  mai.
- Endpoint locali: `GET /api/health` (pubblico) e `GET /api/health/deep`
  (solo admin: check DB, Qdrant, LLM — solo configurazione — ed embedder con
  `Embedder.is_loaded()`; 503 se degradato).
- `app = create_app()` a livello di modulo.

#### 2.6.2 `api/deps.py`

Re-export di `get_current_user`, `require_admin`, `get_db`.

#### 2.6.3 Router

Descritti endpoint per endpoint in §5. File: `routes_chat.py` (SSE e
sessioni), `routes_search.py`, `routes_docs.py`, `routes_ingest.py`,
`routes_admin.py`, `routes_kg.py`, `routes_m365.py`, `routes_feedback.py`,
`routes_setup.py`.

Punti architetturali da conoscere:

- **`routes_chat.py`**: il meccanismo SSE è una `asyncio.Queue` interna con
  eventi `("step"|"token"|"result"|"error", payload)`; l'agente gira in un
  task (`_run_agent_task` → `run_agent` con `token_sink`/`step_sink`) e il
  generatore `_event_stream` traduce negli eventi del contratto
  (`step`/`token`/`reset`/`citations`/`answer`/`done`/`error`). Keep-alive
  `: keep-alive` dopo 15 s di silenzio (`_HEARTBEAT_SECONDS`). Allo step
  `refine` (reflexion) emette `reset` e azzera i token accumulati. Se il
  client si disconnette a metà stream, il task è cancellato e la risposta
  parziale persistita con suffisso `_(risposta interrotta)_` tramite
  `_persist_partial_answer` (in task schermato con `asyncio.shield`).
  `_persist_result` salva messaggio assistant + riga `AuditLog` e ritorna
  l'`audit_id`. Le sessioni sono PRIVATE (404 anche per admin).
- **`routes_docs.py`**: `_allowed_roots(document, source)` limita il file
  serving a `upload_dir` (sorgente upload) o alla radice `config.path`
  (local_fs); path fuori radice → 403; gdrive/m365 → 404. La DELETE
  (admin) rimuove punti Qdrant, chunk, grafo KG (`delete_document_graph`),
  riga e — solo per upload — il file su disco.
- **`routes_admin.py`**: intero router con
  `dependencies=[Depends(require_admin)]`. Contiene anche il reset del
  materiale (a batch di `_RESET_BATCH = 200` documenti, 409 se la pipeline è
  occupata) e l'avvio del reindex (`reindex.get_job().start(...)`, 409 su
  `ReindexRunningError`). La PATCH utenti tutela l'ultimo admin attivo.
- **`routes_ingest.py`**: `_SYNC_CONNECTORS = {"local_fs": LocalFsConnector,
  "gdrive": GDriveConnector}`; le sync girano in task background referenziati
  in `_background_syncs`. L'upload controlla estensione (`SUPPORTED_EXTENSIONS`)
  e dimensione (`upload_max_mb`, 413) prima e dopo la lettura.
- **`routes_m365.py`**: tutto admin. Valida `folders` con
  `parse_folder_spec` a tempo di richiesta (422 se malformato). `status`
  sintetizza uno stato "running/ingest" dai contatori DB quando non c'è un
  job attivo ma la pipeline sta indicizzando. Le credenziali non sono MAI
  persistite.
- **`routes_kg.py`**: `_require_kg()` risponde 404 con dettaglio se
  `kg_enabled` è spento.
- **`routes_feedback.py`**: due router: quello utente (`POST /api/feedback`,
  upsert per `message_id`) e `admin_router` (`/api/admin/feedback`,
  `/stats`). I messaggi altrui rispondono 404.
- **`routes_setup.py`**: endpoint pubblici attivi solo finché non esiste
  alcun utente (`POST /api/setup` → 409 dopo).

### 2.7 Package `agent/` — ciclo agentico

#### 2.7.1 `agent/graph.py` — grafo di stati LangGraph

Stato condiviso `AgentState` (TypedDict, `total=False`) con reducer
`operator.add` per `queries_run`/`warnings`/`steps` e `operator.or_` per
`results` (chunk_id → dict serializzato). Esito `AgentResult` (pydantic):
`answer`, `path`, `standalone_question`, `citations`, `warnings`, `steps`,
`queries_run`, `retrieved_chunk_ids`, `confidence`, `latency_ms`.

Topologia (`build_graph()`):

```
START → classify ─┬─ fast ────→ retrieve ─┬─ fast ────→ synthesize → verify → END
                  └─ agentic → plan → kg_lookup ─┬─ react → research ─┬─ ok ─┘
                                                 │                    └─ ko → retrieve
                                                 └─ graph → retrieve ─┤
                                         evaluate ←───────────────────┘
                                           └─ (lacune, iter < max) → retrieve
(verdict "fragile" in verify → refine → verify, una sola volta)
```

Nodi (tutti async, firma `(state: AgentState, config: RunnableConfig)`):

- `classify` — decide `fast`/`agentic` via `CLASSIFY_PROMPT` e, con storia
  conversazionale, produce `standalone_question` (riferimenti risolti). JSON
  irrecuperabile → percorso prudente `agentic`.
- `plan` — scompone in max 5 sotto-quesiti (`PLAN_PROMPT`), con guardia
  lingua `research.ensure_italian_query`; popola `pending_queries`.
- `kg_lookup` — solo con `kg_enabled` e sessione DB: match deterministico
  `kg_query.match_entities_in_text` sul quesito, poi `entity_subgraph` e
  `documents_for_entities`; produce `kg_context` (guida, non fonte) e
  `kg_document_ids`. Best-effort totale.
- `research_node` — con `agent_mode="react"` invoca
  `research.run_research`; su `research.ReActUnavailable` imposta
  `react_ok=False` e il grafo ripiega sul ciclo classico.
- `retrieve_node` — esegue le `pending_queries` in parallelo (semaforo
  `retrieve_max_concurrency`, sessione DB propria per task), con HyDE
  (`research.hyde_search`) se attivo; fusione deterministica per chunk_id con
  score massimo; eventuale ricerca vincolata ai `kg_document_ids` (tetto
  `kg_agent_max_extra_docs`).
- `evaluate` — grading CRAG (`_apply_crag_grades`): classifica i passaggi
  rilevante/parziale/irrilevante; gli irrilevanti finiscono in
  `excluded_passages`, i parziali con score abbattuto
  (`_CRAG_PARTIAL_WEIGHT = 0.5`). Se non `sufficient`, propone fino a 3
  `refined_queries` (filtrate contro `queries_run` e per lingua) che
  alimentano un altro giro di retrieve, fino a `max_agent_iterations`.
- `synthesize` — barriera anti-allucinazione PRIMA della chiamata LLM: se il
  massimo score rerankato (`_reranked_max_score`) è sotto
  `retrieval_min_score`, o se nessun passaggio menziona i termini distintivi
  (`_distinctive_terms`/`_passages_mention_any`), risponde con l'astensione
  deterministica `_abstain_answer` (prefisso `ABSTENTION_PREFIX` da
  `evaluation.py`), confidence "alta", step `abstain`. Altrimenti: selezione
  `_select_passages` (max `_MAX_PASSAGGI_SINTESI = 12`, max
  `_MAX_PER_DOCUMENTO = 3` per documento, dedup per testo normalizzato),
  budget di token `_fit_citation_map_to_budget`
  (`synthesize_max_context_tokens`, stima cl100k via
  `chunker.count_tokens`), streaming su `token_sink`, parsing citazioni.
- `verify` — aderenza (`VERIFY_PROMPT`) + critic avversariale
  (`CRITIC_PROMPT`, se `critic_enabled`); la confidence finale è il MINIMO
  (`_min_confidence`) tra verdict della critic
  (`_CONFIDENCE_DA_VERDICT` = solido→alta, parziale→media, fragile→bassa) e i
  segnali duri (aderenza fallita → bassa; score sotto le soglie). Risposta
  senza marcatori `[n]` → confidence bassa + feedback esplicito per refine.
- `refine` — reflexion (`reflexion_enabled`, richiede la critic): rigenera la
  risposta con `REFINE_PROMPT` usando `critic_issues` come feedback, una sola
  volta (`refined`), poi torna a `verify`.

Routing condizionale: `_route_after_classify`, `_route_after_kg`,
`_route_after_research`, `_route_after_retrieve`, `_route_after_evaluate`,
`_route_after_verify`. Il **grading condizionale** (`_needs_grading`): con
score rerankato sotto `retrieval_strong_score` anche fast e ReAct passano da
`evaluate`.

Helper importanti: `_chat_json` (JSON mode con UN retry di correzione),
`_step` (voce di tracciamento), `_emit` (sink Queue/callable sync/async),
`_to_search_result`/`_select_passages`/`_dedup_key`, `_format_passage`,
`_format_history`, `_format_kg_context` (max `_MAX_KG_RELAZIONI_CONTESTO = 20`).

Entry point: `run_agent(question, *, session=None, session_id=None,
user_id=None, history=None, token_sink=None, step_sink=None) -> AgentResult`.
Con `step_sink` esegue con `stream_mode="values"` ed emette ogni step appena
appare. `LLMNotConfiguredError` non si propaga: risposta di cortesia con
warning. Sessione DB e sink viaggiano nel `configurable` del grafo (stato
serializzabile).

#### 2.7.2 `agent/research.py` — research agent ReAct

- `run_research(question, *, sub_questions=None, kg_context="", client,
  session) -> dict` — loop Reason+Act: `client.chat_with_tools(messages,
  TOOLS_SPEC)`, esecuzione strumenti via `_exec_tool`, esiti compatti nei
  messaggi (`_compact_hits`, max `_MAX_HITS_IN_MESSAGES = 5`), fino a
  `react_max_rounds` round. Restituisce `{"results", "queries_run", "steps"}`
  nello stesso formato del nodo `retrieve`. Solleva `ReActUnavailable` dopo
  `_MAX_MALFORMED_ROUNDS = 2` round consecutivi senza tool_call validi o su
  errore LLM → il grafo fa fallback. Ogni chiamata è tracciata come step
  `research_tool`.
- `TOOLS_SPEC` — schemi function calling per `hybrid_search`,
  `read_document`, `list_documents`, `cerca_entita`, `contesto_entita`.
- Guardia lingua: `looks_italian(text)` (accentate → italiano; stopword IT →
  italiano; ≥2 stopword EN e nessuna IT → non italiano) e
  `ensure_italian_query(query, fallback)` — applicata a TUTTE le
  interrogazioni generate dal modello (HyDE, plan, riformulazioni, ReAct).
- HyDE: `hyde_search(query, *, client, session, limit=8) -> (hits, hyde_text)`
  genera il passaggio ipotetico (`HYDE_PROMPT`, temperature 0.3), scarta
  testo non italiano e fonde le due ricerche con `fuse_hits` (max-score).

#### 2.7.3 `agent/tools.py` — strumenti documentali

Funzioni async pure, senza decoratori langchain: il grafo le invoca
direttamente, il ReAct le espone via function calling, il server MCP le
riusa.

- `serialize_result(result) -> dict` — serializzazione JSON-friendly di
  `SearchResult` (include `reranked`).
- `_build_filters(filters_dict) -> SearchFilters | None` — chiavi:
  `source_id`, `doc_type`, `date_from`, `date_to`, `document_ids`.
- `hybrid_search(query, filters=None, limit=8, *, session)` — `retrieve()`
  con rerank secondo `agent_rerank_enabled`.
- `read_document(document_id, *, session)` — metadati + concatenazione dei
  `context_text` in ordine; `ValueError` se non esiste.
- `list_documents(*, session, source_id=None, doc_type=None, limit=50)`.
- Strumenti KG (rispondono con `detail` esplicito se KG spento):
  `cerca_entita(query, limit=10, *, session)`,
  `contesto_entita(entity_id, *, session)`,
  `documenti_per_entita(names, mode="any", *, session)`.

#### 2.7.4 `agent/citations.py` — citazioni `[n]`

- `Citation` (pydantic): `n`, `document_id`, `title`, `page`,
  `section_path`, `snippet`, `source_name`, `score`, `bboxes`.
- `build_citation_map(results) -> dict[int, SearchResult]` — numerazione
  `[1..N]` con dedup per chunk e ordine per score.
- `parse_citations(answer_text, citation_map) -> ParsedCitations` — risolve i
  marcatori `[n]` (`_MARKER_RE`), `invalid_markers` per quelli non risolvibili.
- `strip_or_report_uncited(answer_text, citation_map) -> list[str]` —
  euristica: risposta non banale (≥80 char, ≥2 frasi) senza marcatori →
  warning.

#### 2.7.5 `agent/prompts.py`

Prompt di sistema in italiano: `CLASSIFY_PROMPT`, `PLAN_PROMPT`,
`EVALUATE_PROMPT`, `SYNTHESIZE_PROMPT`, `VERIFY_PROMPT`, `RESEARCH_PROMPT`,
`HYDE_PROMPT`, `CRITIC_PROMPT`, `REFINE_PROMPT`. Sono il cuore della qualità:
citarli (e versionarli) quando si interviene sul comportamento dell'agente.

### 2.8 Package `ingestion/` — pipeline documentale

#### 2.8.1 `ingestion/pipeline.py` — coda e worker

Stato in-process: `_PipelineState` (queue `asyncio.Queue[_Job]`, `pending`
dict, `processing`, `processing_since`, `recent` deque maxlen 50). Un solo
worker asyncio per processo.

- `content_hash(path) -> str` — SHA-256 a blocchi (1 MiB).
- `ingest_file(path, source_id, external_id, session_factory, *,
  doc_meta=None) -> int` — registra/aggiorna la riga `Document`
  (status pending) e accoda; dedup: stesso `content_hash` su documento già
  `indexed` → skip (recent con status `skipped`).
- `_process(job, session_factory)` — catena completa: conversione (nel
  sottoprocesso `convert_worker.convert` se `convert_in_subprocess`, in
  thread, timeout `_CONVERT_TIMEOUT_S = 18 min`) → rimozione vecchi chunk
  (DB + indice + KG) → chunking → righe chunk → embedding denso+sparso
  (`build_passage_text(titolo, testo)` solo per l'embedding) → upsert Qdrant
  → status `indexed` con `page_count` → estrazione KG best-effort se
  `kg_enabled`. Errori: `_public_error` (messaggi utente solo per
  `ConversionError`/`ConnectorNotConfiguredError`, altrimenti generico con il
  tipo; l'errore completo resta nel log).
- `_worker_loop` — `asyncio.wait_for(_process, _PROCESS_TIMEOUT_S = 20 min)`;
  su timeout il documento è marcato failed e il worker prosegue; rete di
  sicurezza `except Exception` per non uccidere mai il worker.
- **Watchdog** (`_watchdog_loop`/`_watchdog_tick`, ogni
  `_WATCHDOG_INTERVAL_S = 60` s): (1) rianima il worker se morto; (2) se la
  lavorazione supera 2× il timeout, cancella e ricrea il worker marcando il
  documento failed; (3) riaccoda i `pending` orfani (conteggio persistente
  per `_WATCHDOG_ORPHAN_STRIKES = 2` giri) via `resume_interrupted`.
- `start_worker(session_factory)` / `stop_worker()` — avvio (con
  `ensure_collection`) e arresto senza attesa di svuotamento coda.
- `resume_interrupted(session_factory, *, include_failed=False) -> dict` —
  ripresa dopo riavvio: riaccoda i `pending` ritrovando i file in
  `upload_dir` per hash (o per path diretto), marca failed i `processing`
  residui e i pending il cui file non esiste più; i `failed` solo con
  `include_failed=True` (route `/api/ingest/requeue`).
- `pipeline_status() -> dict` — `{pending, processing, recent}`.
- `reset_state()` per i test.

#### 2.8.2 `ingestion/converter.py` — conversione documenti

- `convert_document(path, *, doc_meta=None) -> NormalizedDoc` — dispatch:
  `.md/.markdown/.txt` → `_convert_plain` (parsing heading Markdown); tutto
  il resto → `_convert_with_docling`. `doc_meta` sovrascrive
  title/author/modified_at. Solleva `ConversionError` (messaggi pensati per
  l'utente).
- `NormalizedDoc` (dataclass): `blocks`, `title`, `author`, `modified_at`,
  `doc_type`, `page_count`. `Block`: `kind` (heading/paragraph/table/
  list_item/caption), `text`, `level`, `page`, `section_path`, `bboxes`.
  `BBox` (TypedDict): punti PDF con origine bottom-left, più
  `page_width`/`page_height` opzionali.
- `_convert_with_docling`: con `ocr_enabled`, OCR Tesseract (ita+eng, variante
  CLI) sempre per le immagini (`_IMAGE_EXTENSIONS`), e per i PDF solo se il
  testo nativo è sotto `_OCR_MIN_CHARS_PER_PAGE = 100` char/pagina
  (scansioni → seconda conversione con OCR).
- `_docling_to_normalized` — appiattisce `iterate_items()` di Docling in
  blocchi, mantenendo gerarchia di sezione, pagina e bbox dei provenance
  (convertiti a bottom-left se necessario); tabelle serializzate in Markdown
  (`_table_to_markdown` via `export_to_dataframe`).

#### 2.8.3 `ingestion/convert_worker.py` — conversione in sottoprocesso

Processo figlio persistente (context `spawn`, daemon) con `RLIMIT_AS` proprio
(`convert_max_memory_mb`): un documento patologico fallisce da solo
(`ConversionError` con `MEMORY_ERROR_MESSAGE`) invece di far uccidere l'app
dall'OOM killer. Comunicazione via `Pipe`: `("convert", path, doc_meta)` →
`("ok", NormalizedDoc)` / `("error", msg)`.

- `convert(path, *, doc_meta=None, timeout=DEFAULT_TIMEOUT_S) ->
  NormalizedDoc` — bloccante (chiamare in `asyncio.to_thread`); avvio lazy,
  ricreazione automatica dopo crash/timeout (`_WorkerCrashedError`,
  `_WorkerTimeoutError`), riciclo ogni `convert_worker_max_tasks` conversioni
  (torch/Docling non rilasciano RSS). Lock `threading` per le transizioni.
- `_child_main` lato figlio: hook di test `_VIRTUALIS_TEST_CONVERT_EXIT` /
  `_VIRTUALIS_TEST_CONVERT_SLEEP_S`.
- `shutdown()` — arresto dal lifespan.

#### 2.8.4 `ingestion/chunker.py` — chunking gerarchico

Funzioni pure, nessun I/O.

- `count_tokens(text, *, method="tiktoken")` — cl100k_base (lazy) con
  fallback `chars/4`.
- `ChunkData` (dataclass): `ord`, `text`, `context_text`, `section_path`,
  `page_start`, `page_end`, `token_count`, `kind` ("text"/"table"), `bboxes`.
- `chunk_document(doc, *, target_tokens, overlap_tokens,
  token_method="tiktoken") -> list[ChunkData]` — raggruppa per sezione (mai
  chunk a cavallo), overlap di frasi intere tra chunk della stessa sezione,
  tabelle sempre in chunk dedicati (`_table_chunk`, con prefisso descrittivo
  "Tabella nel documento «...», sezione «...»"), prefisso `[Sezione: ...]`
  anteposto al testo, frasi oltre budget spezzate per parole, `context_text`
  = finestra estesa chunk ± 1 (`_fill_context`, entro ~2× target).

#### 2.8.5 `ingestion/embeddings.py` — embedding fastembed

- `Embedder` (singleton lazy): `_load_dense` (`dense_model`, default
  `intfloat/multilingual-e5-large`) e `_load_sparse` (`sparse_model`, default
  `Qdrant/bm25`); pesi scaricati da HuggingFace al primo uso.
- API: `embed_passages(texts)` (prefisso `PASSAGE_PREFIX = "passage: "`),
  `embed_queries(texts)` (`QUERY_PREFIX = "query: "`) — i modelli E5 lo
  richiedono — `embed_sparse(texts)` (dict `{"indices", "values"}`),
  proprietà `dense_dim`, classmethod `get`/`reset`/`is_loaded`.
- `truncate_for_embedding(text, budget=EMBED_TOKEN_BUDGET)` — troncamento
  esplicito su parola (budget 552 token cl100k; vedi il ragionamento sul
  tokenizer XLM-R del modello nel commento in codice).
- `build_passage_text(title, text)` — compone "titolo\nchunk" SOLO per
  l'embedding (titolo troncato a `TITLE_TOKEN_BUDGET = 40` token); payload e
  DB conservano il testo pulito.

#### 2.8.6 `ingestion/indexer.py` — indice Qdrant

- `VectorIndex` — facade sul client Qdrant: server se `qdrant_url`,
  altrimenti embedded in `data_dir/qdrant`; `asyncio.Lock` serializza gli
  accessi (l'embedded non tollera concorrenza); client sincrono eseguito in
  thread.
- `ensure_collection()` — vettori named `DENSE_VECTOR_NAME = "dense"`
  (cosine, quantizzazione scalare int8 `always_ram`, quantile 0.99) e
  `SPARSE_VECTOR_NAME = "sparse"` (modifier IDF).
- `point_id(document_id, ord)` — UUID5 deterministico (namespace fisso):
  upsert idempotenti.
- `upsert_chunks(document_meta, chunks, *, chunk_ids, dense_vectors,
  sparse_vectors)` — payload completo (document_id, chunk_id, ord, text,
  context_text, section_path, page_start/end, bboxes, title, source_id/name,
  doc_type, author, modified_at come timestamp Unix).
- `delete_document(document_id)` / `delete_documents(document_ids)` (batch
  `MatchAny`, per il reset) / `collection_exists()`.

### 2.9 Package `retrieval/` — ricerca

- `hybrid.py`:
  - `SearchFilters` (dataclass): `source_id`, `doc_type`, `date_from`,
    `date_to`, `document_ids`; `to_qdrant_filter()` (date su `modified_at`
    timestamp Unix, `MatchAny` per document_ids).
  - `SearchResult` (dataclass): `chunk_id`, `document_id`, `title`,
    `snippet`, `context_text`, `score`, `page`, `section_path`,
    `source_name`, `doc_type`, `modified_at`, `bboxes`, `text`, `reranked`
    (True solo se lo score è del cross-encoder — le soglie di pertinenza si
    applicano solo a score rerankati).
  - `make_snippet(text, max_len=300)` — pulisce `[Sezione: ...]` e marcatori
    markdown, taglia su parola.
  - `HybridSearcher(embedder, index).search(query, *, filters=None,
    limit=10, dense_k=20, sparse_k=20)` — due `Prefetch` (denso e sparso)
    fusi da Qdrant con `FusionQuery(fusion=Fusion.RRF)` nativa; query sotto
    lock dell'indice, embedding in thread.
- `rerank.py`: `Reranker` (singleton lazy) su cross-encoder
  `sentence_transformers.CrossEncoder` (`reranker_model`, default
  `BAAI/bge-reranker-v2-m3`). `rerank(query, results, top_n=5)` — coppie
  (query, titolo + testo chunk troncato a `_RERANK_TEXT_MAX_CHARS = 2000`),
  aggiorna `score` e imposta `reranked=True`. Graceful degradation totale:
  modello non disponibile → risultati inalterati (`_available=False`, mai
  ritentare).
- `service.py`: `retrieve(query, *, filters=None, limit=10, rerank=True,
  rerank_top_n=5, session=None, searcher=None, reranker=None)` — ricerca
  ibrida → scarto dei chunk orfani rispetto al DB (se `session`) → rerank
  opzionale. `_get_searcher()` usa SEMPRE l'indice condiviso della pipeline
  (`pipeline._get_index()`): mai un secondo client Qdrant sullo stesso path.
  `reset_state()` per i test.

### 2.10 Package `knowledge/` — knowledge graph (GraphRAG)

Disabilitato di default (`kg_enabled=false`); ricostruibile con
`scripts/kg_build.py`. Tutto best-effort.

- `extractor.py`:
  - `ENTITY_TYPES` — vocabolario chiuso: persona, organizzazione, contratto,
    progetto, data, importo, prodotto, altro.
  - `normalize_name(name)` (lowercase, spazi compattati),
    `_normalize_predicate` (snake_case, max 128 char).
  - `extract_for_document(document_id, *, session, llm) -> report` — testo =
    `context_text` dei chunk troncato a `kg_max_chars_per_doc`; una chiamata
    LLM (`EXTRACT_PROMPT`, `_chat_json` con un retry); upsert entità
    (`_upsert_entity`, match su name+type); cancellazione/riscrittura
    idempotente di mention e relation del documento; pulizia entità orfane
    (`_delete_orphan_entities`); `invalidate_entity_cache()`. NON solleva
    mai: report `{"n_entities", "n_mentions", "n_relations", "error"}`.
  - `delete_document_graph(document_id, *, session)` — chiamata da ogni
    percorso di cancellazione (commit a carico del chiamante).
- `query.py` (sola lettura):
  - `search_entities(q, *, session, limit=10)` — LIKE case-insensitive +
    `document_count`.
  - `entity_context(entity_id, *, session)` — documenti e relazioni in
    entrambe le direzioni, con controparti e provenienza.
  - `documents_for_entities(names, *, session, mode="any"|"all")` — recupero
    multi-documento.
  - `match_entities_in_text(text, *, session, limit=20)` — match
    deterministico (prefiltro sottostringa + regex con confini di parola),
    minimo `_MIN_NAME_MATCH_LEN = 4` char; serve al nodo `kg_lookup`.
  - `entity_subgraph(names, *, session, depth=1)` — espansione BFS.
  - `full_graph(*, session, limit=300)` — top-N entità per document_count +
    archi interni, con `total_nodes`/`total_edges`.
  - Cache entità in memoria (`_entity_cache`, TTL 120 s, chiave = URL
    engine), invalidata a ogni scrittura (`invalidate_entity_cache`).
- `prompts.py`: `EXTRACT_PROMPT`.

### 2.11 Package `connectors/` — sorgenti documentali

- `base.py`: `Connector` (ABC) — `enumerate() -> list[RemoteItem]`,
  `fetch(item) -> Path`, `changes(state) -> (items|tombstones, new_state)`
  (default: scan completo). `RemoteItem` (external_id, title, modified_at,
  size, meta), `Tombstone`, `ConnectorNotConfiguredError`. Credenziali in
  `Source.config_json`.
- `local_fs.py`: `LocalFsConnector` (config: `path`, `glob` default `**/*`).
  `SUPPORTED_EXTENSIONS` = pdf, docx, xlsx, pptx, html, htm, md, markdown,
  txt, png, jpg, jpeg, tiff, tif, bmp, asciidoc. Titolo = percorso relativo
  senza estensione. `sync(source, session_factory)` — diff per
  `content_hash`: nuovi/modificati → ingest; stesso hash su path diverso →
  rinomina (aggiorna `external_id` senza reindicizzare); spariti → delete
  (indice + KG + DB). Guardia anti-svuotamento: scansione vuota con DB
  popolato → errore invece di cancellare tutto (mount non disponibile?).
- `upload.py`: `save_upload(data, filename, session_factory) -> document_id`
  — nome su disco `<uuid4><estensione sanitizzata>` in `upload_dir`, sorgente
  "upload" creata al primo uso (`get_or_create_upload_source`), poi
  `pipeline.ingest_file`.
- `gdrive.py`: `GDriveConnector` (config: `credentials_json`,
  `folder_id`?/`shared_drive_id`?, `page_token_state` gestito). Scope
  `drive.readonly`, `supportsAllDrives` ovunque. Export Google
  Docs/Sheets/Slides come DOCX/XLSX/PPTX (`_GOOGLE_EXPORTS`). Con
  `folder_id`: enumerazione ricorsiva BFS con percorso nel titolo e scan
  completo a ogni sync; senza: Changes API con `page_token` persistito in
  `Source.state_json` (+ tombstone). `fetch` scarica in `upload_dir/staging`.
  `sync` — dedup su `modifiedTime`, retry SEMPRE dei documenti `failed`
  (metadati freschi via `_get_item`, campo report `retried_failed`), guardia
  anti-svuotamento su scan completo. Dipendenze opzionali extra `gdrive`
  (import lazy in `_drive()`).
- `m365.py`: `M365Connector` (config: `tenant_id`, `client_id`,
  `client_secret`, `drive_id`). MSAL client-credentials, enumerazione
  ricorsiva del drive, `changes` via `/root/delta` con `deltaLink` in state.
  Extra opzionale `m365` (msal). È il connettore "da sorgente continua";
  l'import massivo one-shot è invece `m365_import.py`.

### 2.12 `m365_import.py` — job di import Microsoft 365

Job in-process (singleton `get_job()`, uno per processo), fasi:
auth → scan → download → attesa ingest. Credenziali MAI persistite.

- `GraphClient(auth, cancel_event)` — token endpoint via httpx (nessuna
  dipendenza msal): modalità `password` (ROPC, con traduzione degli errori
  Entra ID — AADSTS50076/50079 MFA, AADSTS7000218 public client),
  `client_credentials`, `device_code`. `get_json` con retry/backoff su
  429/5xx (onora `Retry-After` fino a 120 s, 8 tentativi), `get_all` per la
  paginazione, `download` con callback di progresso.
- Device code flow condiviso: `begin_device_flow(tenant, client_id)`,
  `try_complete_device_flow()`, `device_flow_status()`; `_DeviceFlow` conserva
  il `refresh_token` (riutilizzabile dal job; client di default
  `_DEFAULT_PUBLIC_CLIENT` = Azure PowerShell).
- `ImportJob.start(auth, options, session_factory)` / `cancel()` /
  `status()`. Opzioni: `include_onedrive/sharepoint/mail`, `user`, `users`,
  `folders` (formato `"upn:/percorso"`, validato da `parse_folder_spec`),
  `max_file_mb` (default 50), `scan_mode` (`enumerate`|`search`), `dry_run`.
- Scan: `_users_scope` (/me delegato, singolo utente, lista esplicita UPN, o
  `/users` con errore-guida se manca User.Read.All); `_scan_drive` (BFS con
  `_SCAN_WORKERS = 8` worker concorrenti, dedup `driveId:itemId`, supporto
  `remoteItem` per elementi condivisi, esclusione `_EXCLUDED_DIR_NAMES` —
  node_modules, .git, venv, target...); `_scan_drive_search` (search Graph
  per estensione, modalità opzionale); `_scan_folders` (scoping per cartella
  via `/users/{upn}/drive/root:/percorso:`); `_scan_mailbox` (tutte le
  cartelle, messaggi come task `mail`); SharePoint: tutti i siti (`/sites`
  search `*`) e i loro drive.
- Download: `_download_all` — controllo spazio disco (`_MIN_FREE_BYTES` =
  5 GiB), file in `upload_dir` con nome uuid, sorgente "Microsoft 365"
  (`_get_source_id`, `M365_SOURCE_NAME`), `external_id` =
  `m365://<categoria>/<id>#<nomefile>`; mail → `.md` con intestazioni
  (`_fetch_mail`, `_html_to_text`) + allegati (base64 o stream `$value`).
  Velocità media su finestra mobile 30 s (`_track_bytes`), ETA. Ogni file
  entra in `pipeline.ingest_file` (dedup per hash).
- `_wait_ingest` — polling dei contatori DB della sorgente finché non restano
  pending/processing (stabile per 3 giri).
- Stato osservabile `_JobState` con `snapshot()` (fase, conteggi scan,
  download con percent/byte/speed/eta, ingest, ultimi 200 eventi, ultimi 50
  errori file).

### 2.13 `reindex.py` — reindicizzazione vettoriale

`ReindexJob` (singleton `get_job()`, `reset_job()` per i test): ricostruisce
i punti Qdrant dai chunk già in DB (sola lettura) ri-embeddando col formato
corrente (`build_passage_text`), senza riconvertire i file. Batch di
`_EMBED_BATCH = 200` chunk, delete + upsert per documento (id
deterministici). Cede il passo alla coda di ingestion (`_wait_ingest_idle`,
ogni `_INGEST_PAUSE_S = 15` s). Riprendibile: stato in
`data_dir/reindex_state.json` (`_save_resume_id` atomico), `restart=true` per
ripartire da zero; a lavoro completato il file è rimosso.
`ReindexRunningError` se un job è già attivo. `scripts/reindex.py` è solo un
client HTTP della route admin.

### 2.14 `warmup.py` — preriscaldamento modelli

`warmup_models(settings)` — un embedding denso + uno sparso + un predict del
reranker (se `reranker_enabled`), tutto in thread, mai eccezioni. Invocata
dal lifespan in background (se `warmup_on_start`).

### 2.15 `mcp_server.py` — server MCP

`mcp = FastMCP("virtualis-docs", ...)`, avviabile in stdio con
`python -m app.mcp_server`. Espone gli strumenti di `agent.tools` come tool
MCP: `ricerca_documenti`, `leggi_documento`, `elenca_documenti`,
`kg_cerca_entita`, `kg_contesto_entita`, `kg_documenti_per_entita`. Ogni tool
apre la propria sessione DB via `get_session_factory()`. Nessun side-effect
pesante all'import.

### 2.16 `__main__.py` — entry point

`main()` — `uvicorn.run("app.api.main:app", host=HOST, port=PORT)` (default
`0.0.0.0:8000`); TLS se `TLS_CERT_FILE` + `TLS_KEY_FILE` sono entrambi
valorizzati (errore se uno solo).

---

## 3. Modello dati (`models.py`)

Base comune `Base(DeclarativeBase)`. Date `server_default=func.now()`;
`updated_at` con `onupdate=func.now()`. Le tabelle sono create da
`init_db()` (niente migrazioni; colonne additive via ALTER, vedi §2.2).

### `users` — `User`
| campo | tipo | note |
|---|---|---|
| id | int PK | |
| username | str(128) | unique, index |
| password_hash | str(256) | argon2 |
| role | str(16) | default `user` |
| is_active | bool | default True |
| created_at | datetime | |

### `sources` — `Source`
| campo | tipo | note |
|---|---|---|
| id | int PK | |
| name | str(256) | |
| type | str(16) | local_fs/upload/gdrive/m365 |
| config_json | JSON | credenziali/config del connettore |
| enabled | bool | default True |
| state_json | JSON | stato sync (page_token, delta_link, ...) |
| last_sync_at | datetime | nullable |

Relazione: `documents` (back_populates).

### `documents` — `Document`
UniqueConstraint `(source_id, external_id)` nome `uq_document_source_external`.

| campo | tipo | note |
|---|---|---|
| id | int PK | |
| source_id | FK sources.id | index |
| external_id | str(1024) | path locale o id remoto |
| title | str(512) | |
| doc_type | str(32) | estensione |
| author | str(256) | |
| language | str(16) | |
| content_hash | str(64) | sha256, dedup |
| modified_at | datetime | nullable |
| size_bytes | int | |
| status | str(16) | pending/processing/indexed/failed, index |
| error | Text | nullable, messaggio pubblico |
| page_count | int | nullable |
| created_at / updated_at | datetime | updated_at index |

Relazioni: `source`; `chunks` (cascade `all, delete-orphan`).

### `chunks` — `Chunk`
| campo | tipo | note |
|---|---|---|
| id | int PK | usato nel payload Qdrant |
| document_id | FK documents.id | index |
| ord | int | posizione ordinale |
| text | Text | testo pulito (con prefisso `[Sezione: ...]`) |
| section_path | JSON list[str] | |
| page_start / page_end | int | nullable |
| context_text | Text | finestra estesa (chunk ± 1) |
| token_count | int | |
| bboxes_json | Text | JSON list[BBox], default `[]` |

### `chat_sessions` — `ChatSession`
`id`, `user_id` (FK users.id, index), `title` (primi 60 char del primo
messaggio), `created_at`. Relazione `messages` (cascade, ordinate per id).

### `chat_messages` — `ChatMessage`
`id`, `session_id` (FK, index), `role` (user/assistant), `content` (Text),
`citations_json` (JSON list), `created_at`.

### `feedback` — `Feedback`
`id`, `message_id` (FK chat_messages.id, unique — upsert), `user_id` (FK),
`rating` (int: -1/+1), `comment` (Text, nullable), `question` (Text,
denormalizzata), `answer_excerpt` (Text, primi 500 char), `created_at`,
`updated_at`.

### `audit_logs` — `AuditLog`
`id`, `user_id` (FK, index), `session_id` (FK nullable), `question` (Text),
`path` ("fast"/"agentic"), `steps_json`, `retrieved_json` (list[int] di
chunk_id), `answer`, `warnings_json`, `latency_ms`, `created_at`.

### `entities` — `Entity`
UniqueConstraint `(name, type)` nome `uq_entity_name_type`.
`id`, `name` (str(512), normalizzato, index), `display_name`, `type`
(str(32), index), `description` (Text), `created_at`.

### `entity_mentions` — `EntityMention`
`id`, `entity_id` (FK, index), `document_id` (FK, index), `chunk_id` (FK
nullable, index).

### `relations` — `Relation`
UniqueConstraint `(subject_id, predicate, object_id, document_id)` nome
`uq_relation_triple_doc`. `id`, `subject_id` (FK entities.id, index),
`predicate` (str(128)), `object_id` (FK entities.id, index), `document_id`
(FK, index), `chunk_id` (FK nullable, index).

---

## 4. Configurazione (`config.py`)

Tutti i campi di `Settings`. La env var corrispondente è il nome del campo in
maiuscolo (pydantic-settings, senza prefisso); il file `.env` nella radice
del repo è letto automaticamente. Tra parentesi i valori effettivi in
`docker-compose.yml` quando divergenti dai default.

### LLM
| campo | env | default | significato |
|---|---|---|---|
| moonshot_api_key | `MOONSHOT_API_KEY` | `""` | chiave API; con Ollama un valore non vuoto qualsiasi (compose: `ollama`) |
| kimi_base_url | `KIMI_BASE_URL` | `https://api.moonshot.ai/v1` | base URL OpenAI-compatible (compose: `http://ollama:11434/v1`) |
| kimi_model | `KIMI_MODEL` | `kimi-k3` | modello di generazione (compose: `qwen2.5:14b-instruct`) |
| kimi_reasoning_effort | `KIMI_REASONING_EFFORT` | `high` | extra_body Kimi; vuoto con Ollama (compose: vuoto) |
| llm_request_timeout | `LLM_REQUEST_TIMEOUT` | `120.0` | timeout HTTP per richiesta (compose: `600`) |
| llm_max_retries | `LLM_MAX_RETRIES` | `4` | tentativi su errori transitori |
| llm_retry_base_seconds | `LLM_RETRY_BASE_SECONDS` | `1.0` | base del backoff |
| llm_max_concurrency | `LLM_MAX_CONCURRENCY` | `2` | semaforo globale verso il provider |

### Dati e database
| campo | env | default | significato |
|---|---|---|---|
| data_dir | `DATA_DIR` | `./data` | directory dati (compose: `/data`) |
| jwt_secret | `JWT_SECRET` | `""` | se vuoto: file `data_dir/.jwt_secret` (generato/persistito) |
| jwt_expire_minutes | `JWT_EXPIRE_MINUTES` | `720` | durata token |

### Qdrant / retrieval
| campo | env | default | significato |
|---|---|---|---|
| qdrant_url | `QDRANT_URL` | `""` | vuoto = embedded in `data_dir/qdrant` (compose: vuoto) |
| qdrant_collection | `QDRANT_COLLECTION` | `chunks` | nome collection |
| dense_model | `DENSE_MODEL` | `intfloat/multilingual-e5-large` | embedding denso |
| sparse_model | `SPARSE_MODEL` | `Qdrant/bm25` | embedding sparso |
| reranker_model | `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | cross-encoder |
| reranker_enabled | `RERANKER_ENABLED` | `true` | on/off reranker |
| retrieval_min_score | `RETRIEVAL_MIN_SCORE` | `0.15` | sotto soglia → astensione (solo score rerankati) |
| retrieval_strong_score | `RETRIEVAL_STRONG_SCORE` | `0.60` | sotto soglia → grading CRAG anche su fast/ReAct |
| warmup_on_start | `WARMUP_ON_START` | `true` | preriscaldamento modelli in background |

### Knowledge graph
| campo | env | default | significato |
|---|---|---|---|
| kg_enabled | `KG_ENABLED` | `false` | estrazione entità/relazioni (una chiamata LLM per documento) |
| kg_max_chars_per_doc | `KG_MAX_CHARS_PER_DOC` | `12000` | budget testo per documento |
| kg_max_entities_per_doc | `KG_MAX_ENTITIES_PER_DOC` | `40` | tetto entità per documento |
| kg_agent_depth | `KG_AGENT_DEPTH` | `1` | profondità espansione relazioni nel nodo kg_lookup |
| kg_agent_max_extra_docs | `KG_AGENT_MAX_EXTRA_DOCS` | `3` | documenti extra via grafo nelle ricerche |

### Agente
| campo | env | default | significato |
|---|---|---|---|
| max_agent_iterations | `MAX_AGENT_ITERATIONS` | `3` | loop retrieve/evaluate |
| synthesize_max_context_tokens | `SYNTHESIZE_MAX_CONTEXT_TOKENS` | `10000` | budget token del contesto di sintesi (compose: `10000`) |
| chat_history_messages | `CHAT_HISTORY_MESSAGES` | `6` | messaggi recenti passati come `history` |
| agent_rerank_enabled | `AGENT_RERANK_ENABLED` | `true` | reranker nel ciclo agentico |

### Architettura agentica v2
| campo | env | default | significato |
|---|---|---|---|
| agent_mode | `AGENT_MODE` | `react` | `react` (research agent) o `graph` (classico); valori ignoti → graph |
| react_max_rounds | `REACT_MAX_ROUNDS` | `6` | round massimi del loop ReAct |
| hyde_enabled | `HYDE_ENABLED` | `true` | espansione HyDE delle ricerche |
| crag_enabled | `CRAG_ENABLED` | `true` | grading rilevante/parziale/irrilevante in evaluate |
| critic_enabled | `CRITIC_ENABLED` | `true` | critic avversariale in verify |
| reflexion_enabled | `REFLEXION_ENABLED` | `true` | rigenerazione una tantum su confidence bassa (richiede la critic) |
| retrieve_max_concurrency | `RETRIEVE_MAX_CONCURRENCY` | `3` | ricerche parallele del nodo retrieve |

### Frontend
| campo | env | default | significato |
|---|---|---|---|
| frontend_dist | `FRONTEND_DIST` | `../frontend/dist` | build React servito come SPA (relativo a `backend/`) |

### Ingestion
| campo | env | default | significato |
|---|---|---|---|
| upload_dir | `UPLOAD_DIR` | `./data/uploads` | destinazione upload/staging (compose: `/data/uploads`) |
| upload_max_mb | `UPLOAD_MAX_MB` | `50` | dimensione massima file via API |
| chunk_target_tokens | `CHUNK_TARGET_TOKENS` | `512` | target token per chunk |
| chunk_overlap_tokens | `CHUNK_OVERLAP_TOKENS` | `64` | overlap tra chunk della stessa sezione |
| ocr_enabled | `OCR_ENABLED` | `false` | OCR Tesseract (ita+eng) |
| convert_in_subprocess | `CONVERT_IN_SUBPROCESS` | `true` | conversione Docling in processo figlio |
| convert_max_memory_mb | `CONVERT_MAX_MEMORY_MB` | `6144` | RLIMIT_AS del worker (compose: `24576`) |
| convert_worker_max_tasks | `CONVERT_WORKER_MAX_TASKS` | `8` | riciclo worker ogni N conversioni (0 = mai) |

### HTTP / sicurezza
| campo | env | default | significato |
|---|---|---|---|
| cors_origins | `CORS_ORIGINS` | `http://localhost:5173, http://localhost:3000` | lista o stringa separata da virgole |
| login_max_attempts | `LOGIN_MAX_ATTEMPTS` | `5` | fallimenti prima del 429 |
| login_window_seconds | `LOGIN_WINDOW_SECONDS` | `300` | finestra scorrevole anti brute-force |

### TLS
| campo | env | default | significato |
|---|---|---|---|
| tls_cert_file | `TLS_CERT_FILE` | `""` | entrambi valorizzati → HTTPS (compose: `/data/certs/virtualis.crt`) |
| tls_key_file | `TLS_KEY_FILE` | `""` | (compose: `/data/certs/virtualis.key`) |

### Variabili d'ambiente lette fuori da Settings
- `ADMIN_AUTO_CREATE`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` — bootstrap admin
  (`auth.bootstrap_admin`).
- `HOST`, `PORT` — binding uvicorn (`__main__.py`, default `0.0.0.0:8000`).
- `_VIRTUALIS_TEST_CONVERT_EXIT`, `_VIRTUALIS_TEST_CONVERT_SLEEP_S` — hook di
  test del convert worker.
- In compose, lato servizio ollama: `OLLAMA_CONTEXT_LENGTH=16384`,
  `OLLAMA_NUM_PARALLEL=1`.

---

## 5. Mappa delle API REST

Ruolo: **pub** = pubblico, **user** = autenticato, **admin** = ruolo admin.
Prefissi router tra parentesi.

### auth (`/api/auth`, `auth.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| POST | `/api/auth/login` | pub | login JSON o form; TokenOut |
| GET | `/api/auth/me` | user | profilo utente |

### setup (`/api/setup`, `routes_setup.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| GET | `/api/setup/status` | pub | `needs_setup` (nessun utente?) |
| POST | `/api/setup` | pub | crea il primo admin (409 se già esiste un utente) |

### chat (`/api/chat`, `routes_chat.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| POST | `/api/chat` | user | stream SSE (step/token/reset/citations/answer/done/error) |
| GET | `/api/chat/sessions` | user | sessioni proprie (conteggio + anteprima), `?limit=` |
| GET | `/api/chat/sessions/{id}` | user | dettaglio sessione con messaggi e citazioni (404 se altrui) |
| DELETE | `/api/chat/sessions/{id}` | user | elimina sessione (204; solo proprietario) |

### search (`/api/search`, `routes_search.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| POST | `/api/search` | user | ricerca ibrida + reranking, filtri `source_id/doc_type/date_from/date_to`, `limit` ≤ 50 |

### documents (`/api/documents`, `routes_docs.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| GET | `/api/documents` | user | elenco paginato, filtri `status`, `source_id` |
| GET | `/api/documents/{id}` | user | dettaglio + `chunk_count` |
| GET | `/api/documents/{id}/file` | user | file originale inline (radici consentite) |
| DELETE | `/api/documents/{id}` | admin | elimina documento, chunk, punti, KG, file upload (204) |

### ingest (`/api/ingest`, `routes_ingest.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| POST | `/api/ingest/upload` | user | upload file → coda (202) |
| POST | `/api/ingest/sync` | admin | sync manuale (una sorgente o tutte, 202, background) |
| GET | `/api/ingest/status` | user | coda, conteggi per sorgente, ultimi esiti |
| POST | `/api/ingest/requeue` | admin | riaccoda pending/failed col file su disco (202) |

### admin (`/api/admin`, `routes_admin.py` — intero router admin)
| metodo | path | scopo |
|---|---|---|
| GET | `/api/admin/users` | elenco utenti |
| POST | `/api/admin/users` | crea utente (201; 409 username duplicato; password ≥ 8) |
| PATCH | `/api/admin/users/{id}` | ruolo/stato/password; tutela ultimo admin |
| GET | `/api/admin/audit` | log audit paginato (`limit`, `offset`, `total`) |
| GET | `/api/admin/sources` | elenco sorgenti |
| POST | `/api/admin/sources` | crea sorgente (201; local_fs: path deve esistere) |
| PATCH | `/api/admin/sources/{id}` | nome/config/enabled |
| POST | `/api/admin/material/reset` | reset TOTALE materiale (`confirm: true`, `include_chat?`; 409 se coda occupata) |
| POST | `/api/admin/reindex` | avvia reindicizzazione (202; `confirm`, `dry_run`, `restart`; 409 se attiva) |
| GET | `/api/admin/reindex/status` | stato job reindex |

### knowledge graph (`/api/kg`, `routes_kg.py` — 404 se KG disabilitato)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| GET | `/api/kg/entities?q=&limit=` | user | ricerca entità con document_count |
| GET | `/api/kg/entities/{id}` | user | contesto entità (documenti + relazioni) |
| GET | `/api/kg/graph?limit=` | user | grafo per la UI (top-N, default 300, max 2000) |
| GET | `/api/kg/documents?entities=&mode=` | user | documenti per entità (mode all/any) |

### m365 (`/api/m365`, `routes_m365.py` — tutto admin)
| metodo | path | scopo |
|---|---|---|
| POST | `/api/m365/test` | verifica credenziali (token, nessuna chiamata Graph) |
| POST | `/api/m365/device/begin` | avvia device code flow |
| GET | `/api/m365/device/status` | stato flusso (cattura il refresh token) |
| POST | `/api/m365/start` | avvia import/scan (202; 409 se attivo) |
| GET | `/api/m365/status` | stato job (o stato sintetico dalla pipeline) |
| POST | `/api/m365/cancel` | annulla il job (202, best-effort) |

### feedback (`routes_feedback.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| POST | `/api/feedback` | user | pollice su/giù su un messaggio assistant (upsert) |
| GET | `/api/admin/feedback?rating=&limit=` | admin | elenco feedback con username |
| GET | `/api/admin/feedback/stats` | admin | totali, % positivi, ultimi negativi |

### health (`api/main.py`)
| metodo | path | ruolo | scopo |
|---|---|---|---|
| GET | `/api/health` | pub | stato servizio + `llm_configured` |
| GET | `/api/health/deep` | admin | check DB/Qdrant/LLM/embedder (503 se degradato) |

---

## 6. Ciclo di vita dell'app

Avvio (`lifespan` in `api/main.py`):

1. `init_db()` — tabelle + colonne additive.
2. `bootstrap_admin(db)` — primo admin solo se richiesto via env.
3. `pipeline.start_worker(session_factory)` — `ensure_collection()` Qdrant,
   task `_worker_loop` + task `_watchdog_loop` (60 s).
4. Task background `_resume_ingest_safely()` — `resume_interrupted`:
   riaccoda i pending, marca failed i processing residui e i pending senza
   file.
5. Task background `_run_warmup(settings)` se `warmup_on_start` — embedder
   (denso+sparso) e reranker preriscaldati in thread; mai eccezioni.
6. Log "Applicazione avviata" → l'app serve.

Shutdown:

1. `stop_worker()` — cancella watchdog e worker SENZA attendere la coda (i
   pending saranno ripresi al riavvio).
2. `convert_worker.shutdown()` in thread (la join sul figlio può richiedere
   secondi).
3. Cancellazione dei task resume/warmup con soppressione di `CancelledError`.

Note operative:

- La coda di ingestion è **in memoria**: ogni riavvio la trova vuota; la
  continuità è garantita da `resume_interrupted` + watchdog.
- Il worker di conversione vive in un processo figlio persistente riciclato
  ogni `convert_worker_max_tasks` documenti.
- I job M365 e reindex vivono in memoria del processo: un riavvio li uccide
  (il reindex riparte da `reindex_state.json`; l'import M365 va rilanciato —
  i file già indicizzati non vengono riscaricati grazie alla dedup per hash).
- Il semaforo LLM (`llm_max_concurrency`) evita che job lunghi (KG, HyDE)
  affamino la chat verso provider seriali come Ollama.

---

## 7. Testing (`backend/tests/`)

34 moduli di test, 350 test (`grep -c "def test_"` aggregato). Configurazione
in `backend/pyproject.toml`: `[tool.pytest.ini_options] asyncio_mode = "auto"`,
`testpaths = ["tests"]`. Dipendenze dev nell'extra `dev` (pytest,
pytest-asyncio, respx, python-docx, openpyxl, ruff, mypy).

### Fixture condivise (`conftest.py`)

- `_no_warmup` (autouse): `WARMUP_ON_START=false`.
- `_convert_in_process` (autouse): `CONVERT_IN_SUBPROCESS=false` e
  `get_settings.cache_clear()` prima/dopo (i test del worker in
  `test_convert_worker.py` riattivano il flag).
- `_agent_v2_off` (autouse): `AGENT_MODE=graph`, `HYDE_ENABLED=false`,
  `CRAG_ENABLED=false`, `CRITIC_ENABLED=false` — i test storici verificano il
  percorso classico; `test_react.py` riattiva i flag.
- `tmp_settings(tmp_path, monkeypatch)`: `DATA_DIR`/`UPLOAD_DIR` in tmp_path,
  `reset_engine()`, `pipeline.reset_state()` prima e dopo. L'`Embedder`
  singleton NON è azzerato (modelli caldi tra test: il caricamento ONNX costa
  secondi).

### Pattern ricorrenti

- **LLM sempre mockato**: `test_agent.py` definisce `_FakeKimi`, che risponde
  in base al marcatore presente nel system prompt ("classificatore",
  "pianificatore", "valutatore", "verificatore") e produce token di sintesi
  prefissati; iniettato con
  `monkeypatch.setattr(agent_graph, "get_kimi_client", lambda: fake_kimi)`.
  Anche `tools.hybrid_search` / `retrieval.service` sono sostituiti da fake
  nei test del grafo.
- **Qdrant embedded reale** per i test di retrieval/strumenti (client su
  tmp_path, `VectorIndex(client=..., dense_dim=...)` per evitare il
  caricamento dei modelli).
- **App ASGI reale** in `test_api.py`: fixture `client` =
  `httpx.AsyncClient(transport=ASGITransport(app=app))` con lifespan vero,
  DB/Qdrant/upload in tmp_path, `JWT_SECRET` fisso, reranker off; helper
  `_login`, `_run_chat` (parsing SSE) e attesa indicizzazione via polling.
- **respx** per il mock HTTP (Graph API in `test_m365_import.py`, `test_gdrive.py`).
- Copertura per area: `test_chunker.py`, `test_converter.py`,
  `test_convert_worker.py`, `test_embeddings.py`, `test_indexer.py`,
  `test_pipeline.py`, `test_retrieval.py`, `test_agent.py`,
  `test_agent_kg.py`, `test_react.py`, `test_reflexion.py`,
  `test_antihallucination.py`, `test_synthesize_budget.py`,
  `test_knowledge.py`, `test_connectors.py`, `test_gdrive.py`,
  `test_m365_import.py`, `test_reindex.py`, `test_auth.py`, `test_api.py`,
  `test_chat_history.py`, `test_feedback.py`, `test_setup.py`,
  `test_security.py`, `test_tls.py`, `test_health_deep.py`,
  `test_warmup.py`, `test_evaluation.py`, `test_db.py`, `test_config.py`,
  `test_llm.py`, `test_backup.py`, `test_demo_seed.py`, `test_collaudo.py`.

### Come lanciare

```bash
cd backend && uv run pytest          # suite completa (LLM mockato)
cd backend && uv run pytest -q       # via Makefile: make test
make lint                            # ruff + mypy (backend) + oxlint (frontend)
```

Alcuni test (retrieval/strumenti) caricano i modelli fastembed reali: la
prima esecuzione scarica i pesi da HuggingFace.
