# Architettura tecnica — Sistema agentico di gestione documentale

Documento as-built dell'architettura implementata. Per la visione d'insieme e la
conformità all'offerta: `RELAZIONE.md`. Per l'uso: `README.md` e `docs/MANUALE_UTENTE.md`.

## 1. Principi architetturali

1. **Tutto in locale**: LLM (Ollama), embedding, reranker, indice vettoriale, DB e knowledge graph risiedono sulla stessa macchina. Nessun dato esce dal perimetro.
2. **Componenti sostituibili**: ogni livello comunica tramite contratti stabili (API REST, interfacce Python, MCP). Il modello LLM è una variabile d'ambiente.
3. **Ispezionabilità**: l'agente è un grafo di stati espliciti; ogni passo è tracciato con ingressi, uscite e tempi, e persistito in audit.
4. **Degradazione elegante**: ogni componente opzionale (reranker, KG, OCR, critic) si disattiva senza rompere il flusso principale.
5. **Qualità verificata**: ogni incremento chiude con test verdi (unità, integrazione, E2E) e analisi statica pulita.

## 2. Vista a livelli

```
Utenti (browser su LAN, HTTPS opzionale)
   │
   ▼
Frontend SPA (React 18/TS) ── servita dal backend come statici
   │ REST + SSE (JWT Bearer)
   ▼
API FastAPI ── auth (JWT/argon2) · chat SSE · search · documents · ingest · kg · admin · feedback · setup · health
   │
   ├── Agente (LangGraph) ────────────┐
   │    classify → fast │ agentic     │  strumenti: hybrid_search, read_document,
   │    plan → kg_lookup → retrieve ⇄ evaluate │  list_documents, cerca_entita, contesto_entita
   │    synthesize (stream) → verify  │  (esposti anche via MCP server)
   │                                  ▼
   ├── Retrieval ibrido ── Embedder (e5-large + BM25) ── Qdrant (denso+sparso, RRF, quantizzazione int8)
   │    └── Reranker (bge-reranker-v2-m3, cross-encoder)
   ├── Knowledge graph (SQLite: Entity, EntityMention, Relation; estrattore LLM)
   ├── Pipeline ingest (coda asyncio) ── Docling/OCR → chunker gerarchico → embed → indicizza
   │    └── Connettori: local_fs, upload, gdrive*, m365*   (* dietro credenziali tenant)
   └── Persistenza: SQLite (utenti, documenti, chunk, sessioni, audit, feedback, KG) + file su disco
```

## 3. Flusso di indicizzazione

1. Un connettore (o l'upload) registra il documento: `Document(status=pending)` + hash SHA-256 del contenuto (dedup: stesso hash già indicizzato → skip).
2. Il worker della coda lo prende in carico: `processing`.
3. **Conversione Docling** → `NormalizedDoc` (blocchi tipizzati: heading/paragrafo/tabella/elenco, con `section_path`, pagina e bbox). Se `OCR_ENABLED` e il testo estratto è sotto soglia → riconversione con Tesseract (ita+eng).
4. **Chunking gerarchico**: confini di sezione, limite token verificato sul testo completo, overlap di frasi intere, tabelle come chunk dedicati con descrizione testuale, `context_text` (finestra estesa ±1 chunk).
5. **Embedding**: denso e5-large (prefisso `passage:`) + sparso BM25; testi oltre il limite del modello troncati su parola intera con log.
6. **Upsert Qdrant**: point ID = UUID5(document_id, ord) (idempotente), payload completo (metadati + bbox).
7. Commit DB chunk → `indexed`. Se `KG_ENABLED`: estrazione entità/relazioni via LLM (best-effort).
8. Cancellazioni e rinomine propagate dai connettori (index + DB + KG).

## 4. Flusso di interrogazione (agente)

Stato condiviso (`AgentState`): question, standalone_question, history, path, sub_questions,
queries_run, results, kg_context, iterations, answer, citations, warnings, steps.

- **classify**: JSON `{path, reason, standalone_question}` — con la storia della sessione, risolve i riferimenti ("e nel 2024?" → domanda autonoma). Fallback `agentic` su JSON irrecuperabile.
- **plan** (solo agentic): max 5 sotto-quesiti autonomi.
- **kg_lookup** (solo agentic, se `KG_ENABLED`): match deterministico dei nomi entità (confini di parola, ≥4 caratteri) → `entity_subgraph` + `documents_for_entities` → `kg_context` (guida non citabile) e fino a 3 documenti extra in retrieval.
- **retrieve**: ricerca ibrida (prefetch denso+sparso, RRF nativa Qdrant, filtri metadati) → reranker → merge per chunk_id con score migliore.
- **evaluate** (solo agentic): il modello giudica la copertura e propone riformulazioni; loop limitato da `max_agent_iterations`.
- **synthesize**: max 12 passaggi (cap 3/documento, guardia budget token), prompt con regole ferree (solo fatti dai passaggi, citazioni [n], dichiarare evidenza insufficiente), **streaming** token via `configurable` del grafo.
- **verify**: aderenza delle affermazioni ai passaggi citati → warning.

Ogni nodo appende uno `step` (nome, dettaglio, timestamp): esposto in SSE, persistito in `AuditLog`.

## 5. Evoluzione agentica (v2, in rilascio)

Flag in `config.py` (tutti con fallback al comportamento v1):

- `AGENT_MODE=react` — research agent con **tool-calling** (function calling OpenAI): il modello sceglie autonomamente gli strumenti in un ciclo ragiona-agisci-osserva (max `REACT_MAX_ROUNDS`), con fallback al grafo fisso.
- `HYDE_ENABLED` — ricerca anche su un passaggio ipotetico generato dal modello, fuso con la ricerca standard.
- `CRAG_ENABLED` — grading per passaggio (rilevante/parziale/irrilevante) nel nodo evaluate; irrilevanti scartati.
- `CRITIC_ENABLED` — passo avversariale post-sintesi: tenta di confutare le affermazioni, produce `confidence` (alta/media/bassa) e issues.

Evoluzione successiva (v3, pianificata): RAG-Fusion (query multiple + RRF), contextual retrieval (contesto LLM per chunk all'indicizzazione), memoria agentica cross-sessione, feedback-aware boosting.

## 6. Contratti chiave

- **LLM**: `KimiClient` (wrapper SDK OpenAI): `chat`, `chat_stream`, `extract_json`. Endpoint configurabile (`KIMI_BASE_URL`): Ollama, Kimi, vLLM, llama.cpp. Resilienza: retry backoff+jitter su transitori, Retry-After, distinzione permanente/saldo, retry stream solo pre-token.
- **Connettore** (`connectors/base.py`): enumerate/changes/fetch + propagazione cancellazioni.
- **Retrieval**: `retrieve(query, filters, limit, rerank, session)` → `list[SearchResult]` (chunk, score, pagina, sezione, bbox, metadati).
- **API REST/SSE**: `backend/API.md` (contratto consumato dal frontend; eventi: step/token/citations/answer/done/error).
- **MCP**: server `virtualis-docs` — ricerca_documenti, leggi_documento, elenca_documenti, kg_*.

## 7. Persistenza

- **SQLite** (WAL via aiosqlite): users, sources, documents, chunks, chat_sessions, chat_messages, audit_log, feedback, entities, mentions, relations.
- **Qdrant**: embedded (`data/qdrant`) in dev/single-node, server Docker in produzione (`QDRANT_URL`).
- **File**: originali in `upload_dir` o nelle radici local_fs (servizio confinato alle radici, verifica `resolve()`).
- **Segreti**: JWT secret persistito in `data/.jwt_secret` (600); nessun segreto nel codice.
- **Backup**: `scripts/backup.py` — tar.gz con manifest (sqlite consistente via API backup, upload, qdrant embedded), restore senza cancellazioni.

## 8. Sicurezza

JWT HS256 con revoca immediata alla disattivazione · argon2 con confronto a tempo costante · wizard primo avvio (nessun default) · CORS ristretto · errori API sanitizzati · file serving confinato · TLS opzionale con certificato self-signed generabile · audit completo di interrogazioni, passi agente, documenti consultati e feedback.

## 9. Prestazioni (misurate, macchina di sviluppo M3 Max)

- Ricerca ibrida + rerank: p50 5,1 s, p95 9,5 s, 0 errori su 200 richieste concorrenti (8 utenti).
- Risposta percorso fast con LLM locale 27B: ~4-5 min (thinking incluso); latenze target su GPU RTX decisamente inferiori.
- Indicizzazione: decine di pagine in secondi (docx/md), PDF con layout ~1 min al primo caricamento pipeline.

## 10. Decisioni architetturali rilevanti (e perché)

| Decisione | Alternativa scartata | Motivo |
|---|---|---|
| Grafo a stati espliciti (LangGraph) | function-calling libero | ispezionabilità e audit ogni passo; il ReAct (v2) è aggiunto come percorso con fallback |
| RRF nativa Qdrant | RRF in Python | meno codice, fusione nel motore; verificata in embedded |
| e5-large + BM25 via fastembed | sentence-transformers puro | ONNX leggero, denso+sparso stessa libreria |
| Match deterministico entità (kg_lookup) | estrazione entità via LLM dalla domanda | zero chiamate extra, niente deriva di naming, testabile |
| KimiClient OpenAI-compatible | SDK specifico provider | sostituibilità totale del modello (Ollama/Kimi/vLLM) |
| Knowledge graph su SQLite | Neo4j | zero dipendenze, stesso file di backup, sufficiente per la scala del prototipo |
| Viewer pdfjs lazy | viewer nativo browser | evidenziazione del passaggio via bbox Docling; bundle principale invariato |
