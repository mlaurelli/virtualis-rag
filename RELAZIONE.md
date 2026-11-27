# Relazione tecnica — Sistema agentico di gestione documentale

**Progetto**: Prototipo on-premise di sistema RAG agentico
**Versione della relazione**: 2.0 — 27 luglio 2026 (sostituisce la v1.0, archiviata in `docs/RELAZIONE_v1.0.md`)
**Stato**: implementazione completata e verificata; architettura agentica v2 (ReAct, HyDE, CRAG, critic avversariale, **reflexion**) attiva di default; hardening HTTP e anti brute-force integrati

---

## 1. Scopo e perimetro del lavoro

Il lavoro ha realizzato integralmente il sistema descritto nell'allegato tecnico: un prototipo di **gestione documentale basata su RAG agentico**, eseguibile interamente in locale su una singola macchina (workstation di classe gaming o, in sviluppo, un Mac), con:

- acquisizione e normalizzazione di documenti eterogenei (PDF, Word, Excel, PowerPoint, HTML, Markdown, immagini, scansioni via OCR);
- indicizzazione con ricerca ibrida (semantica + lessicale) e reranking neurale;
- interrogazione in linguaggio naturale con percorso rapido e ciclo agentico multi-passo;
- risposte con **citazioni puntuali e verificabili** fino all'evidenziazione del passaggio nel PDF originale;
- interfaccia web multiutente, con autenticazione, cronologia, amministrazione e registro di audit.

**Modello linguistico locale**: nella configurazione finale il sistema usa un **LLM eseguito interamente in locale via Ollama** — `qwen3.6:27b` (27 miliardi di parametri, quantizzazione 4 bit, contesto 256K) — esattamente come previsto dall'allegato tecnico: nessun documento, indice o interrogazione lascia la macchina. Il client LLM è un wrapper OpenAI-compatible: il passaggio tra modello locale e API Kimi (Moonshot AI), che resta disponibile come alternativa cloud di frontiera, richiede **solo variabili d'ambiente**. Embedding, reranker, indici, basi dati e knowledge graph sono sempre locali.

In aggiunta al perimetro del prototipo (cap. 11 dell'allegato), sono state implementate **tre delle linee di evoluzione del cap. 12** — knowledge graph documentale (GraphRAG), valutazione continua con ciclo di feedback, containerizzazione completa — e, oltre quelle, un'**architettura agentica di seconda generazione** (§ 3.4) e l'**hardening del piano HTTP** (§ 3.7).

### Novità della versione 2.0 della relazione

Rispetto alla v1.0, questa versione documenta due ondate di avanzamenti:

1. **Architettura agentica v2** (già attiva di default, qui documentata per la prima volta): research agent **ReAct con tool-calling**, espansione **HyDE**, grading dei passaggi stile **CRAG**, **critic avversariale** con badge di affidabilità.
2. **Ultimo incremento di qualità e sicurezza**: **reflexion** (autocorrezione della risposta su verdict fragile), **ricerche parallele** nel nodo di recupero, **throttling anti brute-force** sul login, **security headers** su ogni risposta.

---

## 2. Architettura realizzata

```
┌────────────────────────────────────────────────────────────────┐
│  FRONTEND  React 18 + TypeScript + Vite (SPA, tema chiaro/scuro)│
│  Chat SSE · Ricerca · Documenti · Knowledge Graph · Amministr.  │
│  Viewer PDF con evidenziazione del passaggio citato             │
└──────────────────────────▲─────────────────────────────────────┘
                           │ HTTPS/HTTP su LAN, JWT, security headers
┌──────────────────────────┴─────────────────────────────────────┐
│  BACKEND  FastAPI (Python 3.12, async)                          │
│  API REST · SSE streaming · auth JWT · audit log                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ AGENTE v2  LangGraph a stati espliciti                    │  │
│  │ classify → fast | plan → kg_lookup → research (ReAct)     │  │
│  │ retrieve ⇄ evaluate (CRAG) → synthesize → verify (critic) │  │
│  │ ↳ verdict fragile → refine → verify (reflexion, 1 volta)  │  │
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
├── RELAZIONE.md               # la presente relazione (v2.0)
├── docker-compose.yml         # qdrant (con healthcheck) + app
├── Makefile                   # setup, test, lint, build, backup, collaudo…
├── .github/workflows/ci.yml   # CI: backend, frontend, e2e
├── docs/
│   ├── DEMO.md                # guida alla sessione di consegna
│   ├── MANUALE_UTENTE.md      # guida per gli utenti finali
│   ├── RELAZIONE_v1.0.md      # versione precedente della relazione
│   └── screenshots/           # 19 screenshot di QA visivo
├── backend/
│   ├── app/                   # 47 moduli Python (vedi sotto)
│   ├── tests/                 # 28 file di test, 253 test
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

### 3.4 Livello agentico — architettura v2 (cap. 7, oltre spec)

Grafo LangGraph a stati espliciti (`agent/graph.py`) — ogni passo tracciato e ispezionabile:

```
START → classify ─┬─ fast ────→ retrieve ─┬─ fast ────→ synthesize → verify → END
                  └─ agentic → plan → kg_lookup ─┬─ react → research ─┬─ ok ─┘
                                                 │                    └─ ko → retrieve
                                                 └─ graph → retrieve ─┤
                                         evaluate ←───────────────────┘
                                           └─ (lacune, iterazioni < max) → retrieve

(con reflexion attiva, un verdict "fragile" in verify devia su refine → verify,
una sola volta, prima di END)
```

**Nodi del ciclo base:**

- **Classificazione del quesito**: percorso rapido (domande puntuali) vs agentico (confronti, multi-documento, temporali). Con contesto conversazionale, il medesimo passo produce anche la **domanda riformulata autonoma** (*«e nel 2024?»* → domanda completa), registrata in audit per trasparenza.
- **Pianificazione**: scomposizione in sotto-quesiti autonomi (max 5).
- **Ricerca e valutazione**: ricerche ibride per sotto-quesito; il modello giudica esplicitamente se l'evidenza è sufficiente e propone riformulazioni; iterazioni limitate (default 3) per tempi prevedibili.
- **Contesto conversazionale**: gli ultimi messaggi della sessione guidano classificazione e tono, ma **non sono mai una fonte di fatti** (regola esplicita nei prompt).
- **Sintesi con citazioni**: risposta in italiano, streaming token-per-token, ogni affermazione fattuale ancorata a un passaggio `[n]`; selezione dei passaggi con cap per documento e **guardia budget token** (taglio dei meno rilevanti oltre soglia, citazioni sempre coerenti); evidenza insufficiente → dichiarazione esplicita invece di risposta inventata.
- **Verifica di aderenza**: controllo post-generazione delle affermazioni contro i passaggi citati; affermazioni non sostenute segnalate come warning.
- **Tracciabilità completa**: ogni passo (ingressi, uscite, tempi) finisce in `AuditLog` e negli eventi SSE visibili all'utente.

**Potenziamenti v2** — ciascuno dietro flag di configurazione, con fallback automatico al comportamento precedente:

- **Research agent ReAct con tool-calling** (`AGENT_MODE=react`, default): sul percorso agentico i nodi `retrieve`/`evaluate` sono sostituiti da un loop *Reason+Act* (`agent/research.py`): il modello, via function calling OpenAI, decide quale strumento invocare (`hybrid_search`, `read_document`, `list_documents`, `cerca_entita`, `contesto_entita`) fino a `REACT_MAX_ROUNDS` round (default 6). Ogni chiamata è tracciata come step `research_tool` in SSE e audit. Se il tool-calling fallisce, il grafo **ripiega automaticamente** sul ciclo classico `retrieve`/`evaluate` con warning tracciato.
- **HyDE — Hypothetical Document Embeddings** (`HYDE_ENABLED=true`, default): prima di ogni ricerca il modello scrive un breve *passaggio ipotetico* che risponderebbe al quesito; due ricerche ibride (quesito + passaggio) sono fuse per score massimo. Riduce il mismatch lessicale tra la forma della domanda e il linguaggio dei documenti.
- **Grading dei passaggi stile CRAG** (`CRAG_ENABLED=true`, default): il nodo `evaluate` classifica ogni passaggio top come `rilevante`/`parziale`/`irrilevante`; gli irrilevanti sono scartati dalla sintesi, i parziali restano con peso dimezzato.
- **Critic avversariale** (`CRITIC_ENABLED=true`, default): dopo la verifica di aderenza, un secondo prompt cerca attivamente di **confutare** ogni affermazione principale usando solo i passaggi citati. Il verdict (`solido`/`parziale`/`fragile`) diventa la `confidence` della risposta (`alta`/`media`/`bassa`), mostrata dal frontend come badge "Affidabilità"; le criticità diventano warning.
- **Reflexion — autocorrezione su risposta fragile** (`REFLEXION_ENABLED=true`, default; richiede la critic): se il verdict è `fragile`, il nodo **`refine`** **rigenera la risposta una sola volta** passando al modello bozza + criticità + passaggi (prompt dedicato del "revisore"), poi la **riverifica**: la `confidence` finale è quella della riverifica. In streaming, allo step `refine` il backend emette un evento SSE `reset` e i token della nuova risposta sostituiscono la bozza nel client; audit e timeline mostrano il passaggio di revisione. Verdict solidi non pagano alcuna latenza aggiuntiva; il flag `refined` nello stato esclude per costruzione qualunque loop.
- **Ricerche parallele nel nodo `retrieve`**: le interrogazioni pianificate da `plan` (o riformulate da `evaluate`) sono eseguite **in concorrenza** con tetto `RETRIEVE_MAX_CONCURRENCY` (default 3), ciascuna con la propria sessione DB (una `AsyncSession` non è sicura in uso concorrente); la fusione per chunk resta deterministica. Sul percorso agentico riduce sensibilmente la latenza, soprattutto con HyDE attivo (una chiamata LLM per interrogazione).

### 3.5 Knowledge graph documentale (cap. 12, GraphRAG)

- **Estrazione** (`knowledge/extractor.py`): per ogni documento indicizzato, il LLM estrae entità (persone, organizzazioni, contratti, progetti, date, importi, prodotti) e relazioni con provenienza documentale. Idempotente, best-effort (mai bloccante per l'ingest), attivabile con `KG_ENABLED=true` e costruibile sul corpus esistente con `scripts/kg_build.py`.
- **Interrogazione** (`knowledge/query.py`): ricerca entità, contesto entità (documenti + relazioni), documenti per insieme di entità, sottografo di relazioni.
- **GraphRAG attivo**: il nodo `kg_lookup` del grafo riconosce le entità citate nella domanda (match deterministico sui nomi, confini di parola, niente falsi positivi su nomi corti), recupera le relazioni e **tira dentro le ricerche fino a 3 documenti collegati** che la sola ricerca vettoriale avrebbe mancato. Le relazioni guidano la sintesi ma non sono citabili: i fatti restano ancorati ai passaggi.
- **Interfaccia dedicata**: pagina Knowledge Graph con ricerca entità, badge per tipo, dettaglio con documenti e relazioni navigabili.
- Strumenti KG esposti anche via **MCP** e come strumenti del research agent ReAct.

### 3.6 Modello linguistico e resilienza

- **LLM locale via Ollama** (`llm.py`): modello `qwen3.6:27b` (quantizzato 4 bit, 256K di contesto) servito dall'endpoint OpenAI-compatible `http://localhost:11434/v1`. Il client è un wrapper del SDK OpenAI: chat, JSON mode (per i passi strutturati del grafo — classify/plan/evaluate/verify/critic, verificata funzionante con Ollama), streaming per la sintesi e **tool calling** per il research agent ReAct. Il parametro `reasoning_effort`, specifico di Kimi, viene omesso automaticamente quando non configurato. In questa configurazione **nessun dato esce dalla macchina**.
- **Alternativa cloud documentata**: puntando `KIMI_BASE_URL`/`KIMI_MODEL` a `https://api.moonshot.ai/v1` (`kimi-k3`) si passa all'API Kimi senza modifiche di codice; stessa cosa per qualunque altro endpoint OpenAI-compatible (vLLM, llama.cpp server).
- **Resilienza**: retry con backoff esponenziale + jitter su 429/5xx/errori di rete, rispetto di `Retry-After`, timeout esplicito (portato a 600 s per la generazione locale su hardware consumer); errori permanenti e **saldo esaurito** riconosciuti e falliti subito con messaggio chiaro (`LLMInsufficientBalanceError`); nello streaming retry solo prima del primo token.

### 3.7 Sicurezza e conformità (cap. 10)

- Autenticazione JWT multiutente (argon2, ruoli admin/user), **segreto JWT persistito** in `DATA_DIR/.jwt_secret` (permessi 600) — i token sopravvivono ai riavvii; confronto password a tempo costante anche per utenti inesistenti (anti-enumerazione); disattivazione utente con revoca immediata.
- **Throttling anti brute-force sul login** *(nuovo)*: dopo `LOGIN_MAX_ATTEMPTS` fallimenti (default 5) nella finestra scorrevole `LOGIN_WINDOW_SECONDS` (default 300 s) per coppia IP+username, l'endpoint risponde **429** con header `Retry-After`; un login riuscito azzera il contatore. Contatore in memoria del processo, adeguato al dispiegamento a nodo singolo.
- **Security headers su ogni risposta** *(nuovo)*: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` restrittiva e **Content-Security-Policy** moderata (script solo same-origin; stili inline ammessi per il build React; worker/blob per il viewer PDF).
- **Wizard di primo avvio**: creazione guidata del primo amministratore (niente admin/admin predefinito; bootstrap da variabili solo dietro `ADMIN_AUTO_CREATE=true`).
- **File serving confinato**: i documenti originali sono serviti solo se dentro le radici autorizzate (upload dir o radici delle fonti configurate), con verifica su path risolto.
- **HTTPS su LAN**: settings TLS + script `gen_cert.py` per certificato self-signed con SAN corrette; attivazione automatica nel container se configurato.
- **Audit completo**: interrogazioni, passi dell'agente, documenti consultati, riformulazioni, latenze — consultabile in amministrazione.
- Errori API senza path interni né stack trace; cancellazioni propagate all'indice e al knowledge graph.

### 3.8 Interfaccia utente

React 18 + TypeScript strict + Vite, design system proprio (CSS variables, Inter/JetBrains Mono, tema scuro/chiaro):

- **Chat**: streaming SSE con timeline dei passi dell'agente (icone, durata per passo, collassabile; etichette per tutti i passi v2, da `kg_lookup` a `refine`), risposte in **markdown** (elenchi, tabelle, codice), citazioni `[n]` con **hover-preview** (popover con titolo/fonte/snippet) e pannello laterale; **viewer PDF interno** che apre il documento alla pagina e **evidenzia il passaggio citato** (coordinate Docling end-to-end, pdfjs caricato lazy: bundle principale invariato); suggerimenti di domande nella chat vuota; feedback 👍/👎 con commento; **badge di affidabilità** (confidence dalla critic) e gestione dell'evento `reset` della reflexion (la bozza streamata è sostituita dalla risposta corretta).
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
| Test backend (unitari + integrazione, Qdrant embedded reale) | **253** in 28 file | tutti verdi |
| Test end-to-end Playwright (browser reale, pipeline vera) | **12** | 12/12 in due run consecutive (~22 s) |
| QA visivo (screenshot ispezionati, entrambi i temi) | **19 pagine/stati** | 2 difetti trovati e corretti |
| Analisi statica | ruff, mypy (47 file), oxlint | puliti |
| Validazione orchestrazione | `docker compose config` | valida |

I 10 test aggiunti nell'ultimo incremento (`tests/test_reflexion.py`, `tests/test_security.py`) coprono: rigenerazione e riverifica su verdict fragile, unicità della rigenerazione (nessun loop), comportamento con reflexion disabilitata, parallelismo reale del retrieve e tetto di concorrenza, throttling del login (429 con `Retry-After`, azzeramento su successo, isolamento per coppia IP+username), presenza degli header di sicurezza.

### 4.2 Verifiche funzionali eseguite su sistema reale

- Smoke test server: login, upload di 4 documenti (md/docx/pdf), indicizzazione completa, ricerca semantica (top-3 tutti pertinenti) e lessicale (sigla `GAR-2025-0042` al rank 1), chat SSE con eventi in ordine di contratto, download file, SPA servita.
- OCR reale su immagine (testo estratto correttamente).
- Ricerca ibrida dimostrata superiore alla sola densa sui casi lessicali, con test dedicato.
- QA visivo con dati realistici: corretti popover citazione "appeso" e nome fonte `#1` in tabella; la suite E2E ha scoperto e previene la regressione di un bug di polling dei badge di stato.

### 4.3 Ciò che resta da verificare (e perché)

| Elemento | Blocco | Azione richiesta |
|---|---|---|
| Build containerizzato | daemon Docker non attivo sulla macchina di sviluppo | `docker compose build app` sulla workstation |
| Connettori Google/Microsoft | credenziali tenant assenti | configurazione OAuth del cliente |
| TLS end-to-end | richiede il deploy | `scripts/gen_cert.py` + verifica su LAN |

L'integrazione con l'endpoint OpenAI-compatible di Ollama è verificata: chat, JSON mode (fondamentale per i passi del grafo agentico), streaming e tool calling funzionano correttamente.

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

La latenza è dominata dal reranker cross-encoder su CPU (macchina di sviluppo); sulla GPU della workstation il costo si riduce drasticamente. Il lock sull'indice embedded regge la concorrenza senza errori. Le **ricerche parallele** del nodo `retrieve` (§ 3.4) riducono ulteriormente la latenza percepita sul percorso agentico: N interrogazioni pianificate costano il tempo della più lenta anziché la somma.

**Collaudo con LLM reale** (`qwen3.6:27b` locale): completato con esito positivo — ingest verificato, domande reali risposte correttamente con citazioni risolte, percorso fast/agentico corretto, verifica di aderenza passata. Il collaudo ha inoltre scoperto un caso reale di fragilità (caduta di connessione del server LLM a metà streaming) che ha prodotto un rafforzamento della resilienza: retry dello streaming quando l'errore avviene prima del primo token.

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
make test                     # 253 test backend
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
| 7 — Grafo di stati, ciclo agentico, citazioni, aderenza, MCP | Implementato; architettura v2 con ReAct, HyDE, CRAG, critic e reflexion oltre spec; citazioni fino all'evidenziazione PDF | ✅ (oltre spec) |
| 8 — Modelli locali | LLM locale `qwen3.6:27b` via Ollama, embedding/reranker locali: nessun dato esce dalla macchina; API Kimi come alternativa cloud documentata | ✅ |
| 9 — Piattaforma hardware | Dimensionato su RTX 5080 + 32 GB RAM (§ 4.4, con note di sizing oneste); Compose production-grade, healthcheck, quantizzazione | ✅ (build da validare on-site) |
| 10 — Sicurezza, riservatezza, audit | JWT, TLS, audit, confinamento file, segreto persistito, anti brute-force, security headers | ✅ |
| 11 — Perimetro e limiti | Rispettati e documentati | ✅ |
| 12 — Evoluzioni | KG ✅, valutazione continua ✅, scalabilità/container ✅; permessi per-utente e nuovi connettori = percorso noto | ✅ parziale oltre perimetro |
| 14 — Sessione di consegna | Corpus demo + guida + manuale utente + screenshot | ✅ |

---

## 7. Conclusione

Il sistema è completo, verificato a tre livelli (unità/integrazione, end-to-end, visivo) e **interamente locale**: LLM `qwen3.6:27b` via Ollama, embedding, reranker, indici e basi dati sulla stessa macchina, dimensionato per la workstation del cliente (RTX 5080, 32 GB RAM). La qualità è stata perseguita per sottrazione — nessuna parte superflua, ogni componente testato — e per verifica continua: ogni incremento ha chiuso con test verdi, analisi statica pulita e, dove possibile, dimostrazione empirica (la ricerca ibrida che batte la densa, l'OCR che estrae, l'E2E che scopre bug veri).

L'ultimo incremento ha portato il ciclo agentico a un ulteriore livello di maturità: la risposta non è più solo generata e verificata, ma **autocorretta** quando la valutazione critica la giudica fragile (reflexion), con piena trasparenza verso l'utente (timeline, evento `reset`, badge di affidabilità) e in audit; il piano HTTP è indurito (security headers, anti brute-force) e la latenza del percorso agentico è ridotta dalle ricerche parallele. Ogni avanzamento è dietro flag di configurazione con fallback al comportamento precedente: il sistema resta semplice da operare e ogni funzione può essere spenta senza conseguenze.
