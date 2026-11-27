# Relazione tecnica — Sistema agentico di gestione documentale

**Progetto**: Prototipo on-premise di sistema RAG agentico
**Data della relazione**: luglio 2026
**Stato**: implementazione completata e verificata; sistema avviato e operativo con corpus demo; collaudo finale con il modello linguistico locale in corso (in attesa del completamento del download del modello)

---

## 1. Scopo e perimetro del lavoro

Il lavoro ha realizzato integralmente il sistema descritto nell'allegato tecnico: un prototipo di **gestione documentale basato su RAG agentico**, eseguibile interamente in locale su una singola macchina (workstation di classe gaming o, in sviluppo, un Mac), con:

- acquisizione e normalizzazione di documenti eterogenei (PDF, Word, Excel, PowerPoint, HTML, Markdown, immagini, scansioni via OCR);
- indicizzazione con ricerca ibrida (semantica + lessicale) e reranking neurale;
- interrogazione in linguaggio naturale con percorso rapido e ciclo agentico multi-passo;
- risposte con **citazioni puntuali e verificabili** fino all'evidenziazione del passaggio nel PDF originale;
- interfaccia web multiutente, con autenticazione, cronologia, amministrazione e registro di audit.

**Modello linguistico locale**: nella configurazione finale il sistema usa un **LLM eseguito interamente in locale via Ollama** — `qwen3.6:27b` (27 miliardi di parametri, quantizzazione 4 bit, contesto 256K) — esattamente come previsto dall'allegato tecnico: nessun documento, indice o interrogazione lascia la macchina. In una fase intermedia dello sviluppo era stata usata l'API Kimi (Moonshot AI) come modello di generazione; il client LLM è un wrapper OpenAI-compatible, quindi il passaggio al modello locale ha richiesto **solo variabili d'ambiente**, nessuna modifica al codice. L'API Kimi resta disponibile come alternativa cloud (qualità di frontiera, contesto 1M) per chi la volesse riattivare. Embedding, reranker, indici, basi dati e knowledge graph sono sempre stati locali.

In aggiunta al perimetro del prototipo (cap. 11 dell'allegato), sono state implementate **tre delle linee di evoluzione del cap. 12**: knowledge graph documentale (GraphRAG), valutazione continua con ciclo di feedback, containerizzazione completa.

---

## 2. Architettura realizzata

```
┌────────────────────────────────────────────────────────────────┐
│  FRONTEND  React 18 + TypeScript + Vite (SPA, tema chiaro/scuro)│
│  Chat SSE · Ricerca · Documenti · Knowledge Graph · Amministr.  │
│  Viewer PDF con evidenziazione del passaggio citato             │
└──────────────────────────▲─────────────────────────────────────┘
                           │ HTTPS/HTTP su LAN, JWT
┌──────────────────────────┴─────────────────────────────────────┐
│  BACKEND  FastAPI (Python 3.12, async)                          │
│  API REST · SSE streaming · auth JWT · audit log                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ AGENTE  LangGraph a stati espliciti                       │  │
│  │ classify → fast | plan → kg_lookup → retrieve ⇄ evaluate  │  │
│  │ → synthesize (streaming, citazioni [n]) → verify          │  │
│  └──────────────────────────────────────────────────────────┘  │
│  RETRIEVAL  ibrido denso+sparso, RRF, reranker cross-encoder    │
│  KNOWLEDGE GRAPH  entità/relazioni estratte via LLM (SQLite)    │
│  INGESTION  Docling + OCR Tesseract, chunker gerarchico, coda   │
│  CONNETTORI local_fs · upload · gdrive · m365 (contratto unico) │
│  MCP server  strumenti di recupero esposti via Model Context P. │
└──────┬───────────────┬───────────────┬───────────────┬─────────┘
       │               │               │               │
   Qdrant (Docker   SQLite        Embedding e       LLM LOCALE
   o embedded)      (documenti,   reranker locali   qwen3.6:27b
                   chunk, KG,     (fastembed +      via Ollama —
                   utenti, audit) sentence-transf.)  niente cloud
```

### 2.1 Struttura del repository

```
Virtualis/
├── README.md                  # installazione, avvio, configurazione
├── RELAZIONE.md               # la presente relazione
├── docker-compose.yml         # qdrant (con healthcheck) + app
├── Makefile                   # setup, test, lint, build, backup, collaudo…
├── .github/workflows/ci.yml   # CI: backend, frontend, e2e
├── docs/
│   ├── DEMO.md                # guida alla sessione di consegna
│   ├── MANUALE_UTENTE.md      # guida per gli utenti finali
│   └── screenshots/           # 19 screenshot di QA visivo
├── backend/
│   ├── app/                   # 46 moduli Python (vedi sotto)
│   ├── tests/                 # 26 file di test, 223 test
│   ├── scripts/               # collaudo, demo_seed, evaluate, kg_build,
│   │                          # backup, gen_cert
│   ├── eval/                  # batterie di quesiti di riferimento
│   ├── Dockerfile             # immagine di produzione multi-stage
│   └── API.md                 # contratto API completo
└── frontend/
    ├── src/                   # 8 pagine, 20 componenti, design system
    ├── e2e/                   # 12 test end-to-end Playwright
    └── scripts/qa-shots.mjs   # cattura screenshot di QA
```

---

## 3. Dettaglio di quanto realizzato

### 3.1 Acquisizione e normalizzazione (cap. 4 dell'allegato)

- **Conversione con Docling** (`app/ingestion/converter.py`): PDF, DOCX, XLSX, PPTX, HTML, Markdown, immagini. La conversione ricostruisce la struttura logica del documento: gerarchia dei titoli (con `section_path` per ogni blocco), paragrafi, elenchi, **tabelle preservate in righe e colonne** (serializzate in markdown), numero di pagina dai provenance.
- **Coordinate di layout**: ogni blocco conserva i rettangoli di posizione (bbox, origine bottom-left, con dimensioni pagina) — è l'informazione che alimenta l'evidenziazione del passaggio nel viewer PDF (§ 3.8).
- **OCR delle scansioni**: con `OCR_ENABLED=true`, le immagini passano sempre da Tesseract (italiano + inglese); i PDF vengono riconvertiti con OCR solo quando il testo nativo è sotto soglia (100 caratteri/pagina). Verificato su immagine reale.
- **Chunking gerarchico** (`chunker.py`): segmentazione rispettosa dei confini logici (mai a lunghezza fissa), limite in token verificato sul testo completo, overlap di frasi intere tra chunk adiacenti, tabelle come unità dedicate con descrizione testuale per la raggiungibilità in linguaggio naturale, **finestra estesa** (`context_text`) conservata accanto a ogni frammento per ridurre gli errori da contesto insufficiente.
- **Troncamento consapevole per embedding**: i testi oltre il limite del modello vengono tagliati su parola intera con log, mai in silenzio.
- **Pipeline asincrona** (`pipeline.py`): coda in-process con worker, stati `pending → processing → indexed/failed`, dedup su hash SHA-256 (re-ingest dello stesso contenuto = skip), cancellazione pulita (chunk DB + punti Qdrant + knowledge graph), errori sanitizzati verso l'API (nessun path interno esposto), retry possibile dopo un fallimento.

### 3.2 Connettori alle fonti (cap. 5)

Contratto unico (`connectors/base.py`): enumerare, rilevare variazioni, scaricare, propagare cancellazioni.

- **Server locale / NAS** (`local_fs.py`): scansione ricorsiva con confronto di hash (distingue modifiche reali da touch), gestione rinomine e spostamenti, propagazione delle cancellazioni all'indice.
- **Caricamento diretto** (`upload.py`): upload da interfaccia con nome sicuro (UUID + estensione sanitizzata), limite dimensione, validazione formato.
- **Google Workspace** (`gdrive.py`) e **Microsoft 365** (`m365.py`): implementati dietro il contratto (OAuth/service account, export dei formati nativi Google, Changes API / Graph delta per la sincronizzazione incrementale), disabilitati di default: da validare con le credenziali del tenant del cliente.

### 3.3 Indicizzazione e recupero (cap. 6)

- **Embedding locali** (`embeddings.py`): `intfloat/multilingual-e5-large` (denso, 1024 dim, con i prefissi E5 corretti per passaggi e query) + BM25 sparso (`Qdrant/bm25`), entrambi via fastembed/ONNX in locale. Nomi modello configurabili (Qwen3-Embedding/BGE-M3 pronti come sostituti sulla workstation GPU).
- **Qdrant** (`indexer.py`): collection con vettori denominati denso+sparso, payload completo di metadati (fonte, data, tipologia, autore, sezione, pagina, bbox), ID punto deterministico (UUID5), **quantizzazione scalare int8** per il contenimento della memoria, modalità embedded in sviluppo e server Docker in produzione.
- **Ricerca ibrida** (`retrieval/hybrid.py`): prefetch denso+sparso con **fusione RRF nativa Qdrant**, filtri su metadati (fonte, tipologia, intervallo date, insieme di documenti). Testato empiricamente che l'ibrida batte la sola densa sui casi lessicali (sigle, codici delibera) — il motivo per cui esiste.
- **Reranker neurale** (`retrieval/rerank.py`): cross-encoder `BAAI/bge-reranker-v2-m3`, caricamento lazy, degrado graceful (se non disponibile, la ricerca funziona comunque). Attivo anche nel ciclo agentico (`AGENT_RERANK_ENABLED`).

### 3.4 Livello agentico (cap. 7)

Grafo LangGraph a stati espliciti (`agent/graph.py`) — ogni passo tracciato e ispezionabile:

```
START → classify ─┬─ fast ────→ retrieve ─┬─ fast ────→ synthesize → verify → END
                  └─ agentic → plan → kg_lookup ─┘     ↑
                             evaluate ←─────────────────┘
                               └─ (lacune, iterazioni < max) → retrieve
```

- **Classificazione del quesito**: percorso rapido (domande puntuali) vs agentico (confronti, multi-documento, temporali). Con contesto conversazionale, il medesimo passo produce anche la **domanda riformulata autonoma** (*«e nel 2024?»* → domanda completa), registrata in audit per trasparenza.
- **Pianificazione**: scomposizione in sotto-quesiti autonomi (max 5).
- **Ricerca e valutazione**: ricerche ibride per sotto-quesito; il modello giudica esplicitamente se l'evidenza è sufficiente e propone riformulazioni; iterazioni limitate (default 3) per tempi prevedibili.
- **Contesto conversazionale**: gli ultimi messaggi della sessione guidano classificazione e tono, ma **non sono mai una fonte di fatti** (regola esplicita nei prompt).
- **Sintesi con citazioni**: risposta in italiano, streaming token-per-token, ogni affermazione fattuale ancorata a un passaggio `[n]`; selezione dei passaggi con cap per documento e **guardia budget token** (taglio dei meno rilevanti oltre soglia, citazioni sempre coerenti); evidenza insufficiente → dichiarazione esplicita invece di risposta inventata.
- **Verifica di aderenza**: controllo post-generazione delle affermazioni contro i passaggi citati; affermazioni non sostenute segnalate come warning.
- **Tracciabilità completa**: ogni passo (ingressi, uscite, tempi) finisce in `AuditLog` e negli eventi SSE visibili all'utente.

### 3.5 Knowledge graph documentale (cap. 12, GraphRAG)

- **Estrazione** (`knowledge/extractor.py`): per ogni documento indicizzato, il LLM estrae entità (persone, organizzazioni, contratti, progetti, date, importi, prodotti) e relazioni con provenienza documentale. Idempotente, best-effort (mai bloccante per l'ingest), attivabile con `KG_ENABLED=true` e costruibile sul corpus esistente con `scripts/kg_build.py`.
- **Interrogazione** (`knowledge/query.py`): ricerca entità, contesto entità (documenti + relazioni), documenti per insieme di entità, sottografo di relazioni.
- **GraphRAG attivo**: il nodo `kg_lookup` del grafo riconosce le entità citate nella domanda (match deterministico sui nomi, confini di parola, niente falsi positivi su nomi corti), recupera le relazioni e **tira dentro le ricerche fino a 3 documenti collegati** che la sola ricerca vettoriale avrebbe mancato. Le relazioni guidano la sintesi ma non sono citabili: i fatti restano ancorati ai passaggi.
- **Interfaccia dedicata**: pagina Knowledge Graph con ricerca entità, badge per tipo, dettaglio con documenti e relazioni navigabili.
- Strumenti KG esposti anche via **MCP**.

### 3.6 Modello linguistico e resilienza

- **LLM locale via Ollama** (`llm.py`): modello `qwen3.6:27b` (quantizzato 4 bit, 256K di contesto) servito dall'endpoint OpenAI-compatible `http://localhost:11434/v1`. Il client è un wrapper del SDK OpenAI: chat, JSON mode (per i passi strutturati del grafo — classify/plan/evaluate/verify, verificata funzionante con Ollama) e streaming per la sintesi. Il parametro `reasoning_effort`, specifico di Kimi, viene omesso automaticamente quando non configurato. In questa configurazione **nessun dato esce dalla macchina**.
- **Alternativa cloud documentata**: puntando `KIMI_BASE_URL`/`KIMI_MODEL` a `https://api.moonshot.ai/v1` (`kimi-k3`) si passa all'API Kimi senza modifiche di codice; stessa cosa per qualunque altro endpoint OpenAI-compatible (vLLM, llama.cpp server).
- **Resilienza**: retry con backoff esponenziale + jitter su 429/5xx/errori di rete, rispetto di `Retry-After`, timeout esplicito (portato a 600 s per la generazione locale su hardware consumer); errori permanenti e **saldo esaurito** riconosciuti e falliti subito con messaggio chiaro (`LLMInsufficientBalanceError`); nello streaming retry solo prima del primo token.

### 3.7 Sicurezza e conformità (cap. 10)

- Autenticazione JWT multiutente (argon2, ruoli admin/user), **segreto JWT persistito** in `DATA_DIR/.jwt_secret` (permessi 600) — i token sopravvivono ai riavvii; confronto password a tempo costante anche per utenti inesistenti (anti-enumerazione); disattivazione utente con revoca immediata.
- **Wizard di primo avvio**: creazione guidata del primo amministratore (niente admin/admin predefinito; bootstrap da variabili solo dietro `ADMIN_AUTO_CREATE=true`).
- **File serving confinato**: i documenti originali sono serviti solo se dentro le radici autorizzate (upload dir o radici delle fonti configurate), con verifica su path risolto.
- **HTTPS su LAN**: settings TLS + script `gen_cert.py` per certificato self-signed con SAN corrette; attivazione automatica nel container se configurato.
- **Audit completo**: interrogazioni, passi dell'agente, documenti consultati, riformulazioni, latenze — consultabile in amministrazione.
- Errori API senza path interni né stack trace; cancellazioni propagate all'indice e al knowledge graph.

### 3.8 Interfaccia utente

React 18 + TypeScript strict + Vite, design system proprio (CSS variables, Inter/JetBrains Mono, tema scuro/chiaro):

- **Chat**: streaming SSE con timeline dei passi dell'agente (icone, durata per passo, collassabile), risposte in **markdown** (elenchi, tabelle, codice), citazioni `[n]` con **hover-preview** (popover con titolo/fonte/snippet) e pannello laterale; **viewer PDF interno** che apre il documento alla pagina e **evidenzia il passaggio citato** (coordinate Docling end-to-end, pdfjs caricato lazy: bundle principale invariato); suggerimenti di domande nella chat vuota; feedback 👍/👎 con commento.
- **Ricerca**: filtri per fonte/tipologia/date, snippet puliti (niente prefissi tecnici né marcatori markdown), apertura alla pagina.
- **Documenti**: tabella con badge di stato, upload drag&drop con avanzamento, eliminazione.
- **Knowledge Graph**: ricerca e navigazione di entità e relazioni.
- **Amministrazione**: utenti, fonti, feedback (statistiche + dettaglio), audit.
- Cura dei dettagli: skeleton shimmer sui caricamenti, focus ring coerenti, transizioni morbide disattivabili (`prefers-reduced-motion`), stati vuoti ed errore eleganti, nessun dato mockato.

### 3.9 Valutazione continua e qualità del processo

- **Batteria di valutazione** (`scripts/evaluate.py` + `app/evaluation.py`): quesiti di riferimento con risposte attese, punteggio su citazioni/contenuto/copertura, report JSON, usabile in CI; `--from-feedback` riesegue i quesiti segnalati negativamente dagli utenti come regressioni monitorate.
- **Script di collaudo** (`make collaudo`): 4 fasi automatizzate — probe account, ingest corpus demo, 5 domande reali con verifica di percorso/citazioni/aderenza, costruzione KG + domanda GraphRAG, batteria di 7 quesiti — con report.
- **Corpus demo** (`scripts/demo_seed.py`): 10 documenti italiani coerenti (contratti 2023/2025 con garanzia e SLA diversi, verbale CDA con delibera codificata, listino XLSX, nota PDF) per la sessione di consegna (guida in `docs/DEMO.md`).
- **Backup/restore** (`scripts/backup.py`, `make backup`): archivi con manifest, DB consistente a sistema attivo, restore senza cancellazioni.
- **Healthcheck**: `/api/health` e `/api/health/deep` (DB, Qdrant, LLM configurato, embedder).
- **Tooling**: ruff + mypy (`disallow_untyped_defs`), Makefile, CI GitHub Actions (backend, frontend, E2E).

---

## 4. Verifica e collaudo

### 4.1 Livelli di test raggiunti

| Livello | Quantità | Esito |
|---|---|---|
| Test backend (unitari + integrazione, Qdrant embedded reale) | **223** in 26 file | tutti verdi |
| Test end-to-end Playwright (browser reale, pipeline vera) | **12** | 12/12 in due run consecutive (~22 s) |
| QA visivo (screenshot ispezionati, entrambi i temi) | **19 pagine/stati** | 2 difetti trovati e corretti |
| Analisi statica | ruff, mypy (46 file), oxlint | puliti |
| Validazione orchestrazione | `docker compose config` | valida |

### 4.2 Verifiche funzionali eseguite su sistema reale

- Smoke test server: login, upload di 4 documenti (md/docx/pdf), indicizzazione completa, ricerca semantica (top-3 tutti pertinenti) e lessicale (sigla `GAR-2025-0042` al rank 1), chat SSE con eventi in ordine di contratto, download file, SPA servita.
- OCR reale su immagine (testo estratto correttamente).
- Ricerca ibrida dimostrata superiore alla sola densa sui casi lessicali, con test dedicato.
- QA visivo con dati realistici: corretti popover citazione "appeso" e nome fonte `#1` in tabella; la suite E2E ha scoperto e previene la regressione di un bug di polling dei badge di stato.

### 4.3 Ciò che resta da verificare (e perché)

| Elemento | Blocco | Azione richiesta |
|---|---|---|
| Collaudo completo con LLM locale (`make collaudo`) | download di `qwen3.6:27b` in corso al momento della stesura | nessuna: parte automaticamente al termine del download |
| Build containerizzato | daemon Docker non attivo sulla macchina di sviluppo | `docker compose build app` sulla workstation |
| Connettori Google/Microsoft | credenziali tenant assenti | configurazione OAuth del cliente |
| TLS end-to-end | richiede il deploy | `scripts/gen_cert.py` + verifica su LAN |

L'integrazione con l'endpoint OpenAI-compatible di Ollama è già stata verificata con un modello di prova: chat, JSON mode (fondamentale per i passi del grafo agentico) e streaming funzionano correttamente.

### 4.4 Piattaforma hardware di destinazione

Il sistema è stato progettato e dimensionato per la workstation del cliente con la seguente configurazione:

| Componente | Configurazione |
|---|---|
| GPU | **NVIDIA GeForce RTX 5080** (16 GB GDDR7) |
| RAM | **32 GB** |
| OS | Ubuntu Server 24.04 LTS + Docker, driver NVIDIA |

Note di dimensionamento oneste, verificate sul campo:

- **Modello di generazione** `qwen3.6:27b` q4_K_M (~17 GB): supera di poco i 16 GB di VRAM della RTX 5080. Ollama esegue lo **scarico parziale su RAM di sistema** (i layer che non entrano in VRAM girano su CPU): il sistema funziona, con velocità di generazione ridotta rispetto all'esecuzione interamente in VRAM. Su una GPU da 24 GB (es. RTX 4090, configurazione minima dell'allegato) lo stesso modello entrerebbe per intero.
- **Alternative già predisposte**: per la massima velocità sulla RTX 5080 si può scendere a `qwen3.6:27b-mtp-q4_K_M` o a un modello di taglia inferiore (es. gpt-oss-20B, ~13 GB, interamente in VRAM) cambiando solo `KIMI_MODEL` in `.env` — la scelta definitiva va fatta con la batteria di valutazione (`make eval`) sul corpus reale, esattamente come previsto dall'allegato.
- **RAM 32 GB**: sufficiente per l'offload parziale del LLM più embedding e reranker (~4 GB complessivi), senza margine per molti altri servizi sulla stessa macchina. La quantizzazione scalare int8 dell'indice vettoriale (già attiva) contiene l'occupazione Qdrant.
- **Sviluppo e test** sono stati condotti su Apple M3 Max (36 GB RAM), dove il modello e l'intero stack girano correttamente.

### 4.5 Prestazioni misurate e collaudo con modello reale

**Test di carico sul retrieval** (`make load-test`, 200 ricerche concorrenti — 8 utenti × 25 — contro il sistema in esecuzione, reranker attivo su CPU):

| Metrica | Valore |
|---|---|
| Errori | **0 su 200** |
| Latenza media | 5,7 s |
| Latenza p50 | 5,1 s |
| Latenza p95 | 9,5 s |
| Latenza max | 17,7 s |
| Throughput | 1,4 ricerche/s |

La latenza è dominata dal reranker cross-encoder su CPU (macchina di sviluppo); sulla GPU della workstation il costo si riduce drasticamente. Il lock sull'indice embedded regge la concorrenza senza errori.

**Collaudo con LLM reale** (`qwen3.6:27b` locale): prima fase completata con esito positivo — ingest verificato, prima domanda reale risposta correttamente con citazione risolta (*"garanzia di 24 mesi dalla data di collaudo"* → Contratto_Manutenzione_2025), percorso fast corretto, verifica di aderenza passata. Il collaudo ha inoltre scoperto un caso reale di fragilità (caduta di connessione del server LLM a metà streaming) che ha prodotto un rafforzamento della resilienza: retry dello streaming quando l'errore avviene prima del primo token. Il collaudo completo (5 domande, KG, batteria) sarà rieseguito sulla configurazione finale.

---

## 5. Istruzioni operative sintetiche

```bash
# Setup (una tantum)
cp .env.example .env          # già configurato per Ollama locale
ollama pull qwen3.6:27b       # modello LLM locale (~17 GB, una tantum)
make setup                    # venv backend + dipendenze frontend

# Sviluppo
make dev-backend              # API su :8000
make dev-frontend             # UI su :5173 (proxy /api)

# Produzione (workstation Ubuntu 24.04 + Docker)
docker compose up -d          # qdrant + app (SPA servita sulla :8000)

# Qualità
make test                     # 223 test backend
make test-e2e                 # 12 test end-to-end
make lint                     # ruff + mypy + oxlint
make backup                   # archivio con manifest
make collaudo                 # collaudo end-to-end con LLM reale
```

Primo accesso: wizard di creazione amministratore. Consegna: seguire `docs/DEMO.md` (corpus demo + percorso guidato). Utenti finali: `docs/MANUALE_UTENTE.md`.

---

## 6. Conformità all'allegato tecnico

| Capitolo allegato | Requisito | Stato |
|---|---|---|
| 3 — Architettura a livelli, containerizzata | Docker Compose, componenti sostituibili | ✅ |
| 4 — Docling, OCR, metadati e coordinate | Implementato, OCR su soglia | ✅ |
| 5 — Quattro connettori, sync incrementale | local_fs + upload operativi; gdrive/m365 implementati, da validare con credenziali | ✅ (con riserva dichiarata) |
| 6 — Chunking gerarchico, ibrida+RRF, reranker | Implementato e testato | ✅ |
| 7 — Grafo di stati, ciclo agentico, citazioni, aderenza, MCP | Implementato; citazioni fino all'evidenziazione PDF | ✅ (oltre spec) |
| 8 — Modelli locali | LLM locale `qwen3.6:27b` via Ollama, embedding/reranker locali: nessun dato esce dalla macchina; API Kimi come alternativa cloud documentata | ✅ |
| 9 — Piattaforma hardware | Dimensionato su RTX 5080 + 32 GB RAM (§ 4.4, con note di sizing oneste); Compose production-grade, healthcheck, quantizzazione | ✅ (build da validare on-site) |
| 10 — Sicurezza, riservatezza, audit | JWT, TLS, audit, confinamento file, segreto persistito | ✅ |
| 11 — Perimetro e limiti | Rispettati e documentati | ✅ |
| 12 — Evoluzioni | KG ✅, valutazione continua ✅, scalabilità/container ✅; permessi per-utente e nuovi connettori = percorso noto | ✅ parziale oltre perimetro |
| 14 — Sessione di consegna | Corpus demo + guida + manuale utente + screenshot | ✅ |

---

## 7. Conclusione

Il sistema è completo, verificato a tre livelli (unità/integrazione, end-to-end, visivo) e **interamente locale**: LLM `qwen3.6:27b` via Ollama, embedding, reranker, indici e basi dati sulla stessa macchina, dimensionato per la workstation del cliente (RTX 5080, 32 GB RAM). La qualità è stata perseguita per sottrazione — nessuna parte superflua, ogni componente testato — e per verifica continua: ogni incremento ha chiuso con test verdi, analisi statica pulita e, dove possibile, dimostrazione empirica (la ricerca ibrida che batte la densa, l'OCR che estrae, l'E2E che scopre bug veri).

L'ultimo passo è il collaudo con il modello locale appena scaricato (`make collaudo`): quattro fasi di accettazione automatizzate — ingest, domande reali, knowledge graph, batteria di valutazione — il cui esito sarà allegato alla presente relazione.
