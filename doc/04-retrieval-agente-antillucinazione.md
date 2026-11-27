# 04 — Retrieval, agente e difese anti-allucinazione

Deep-dive tecnico su come Virtualis cerca nei documenti, costruisce la
risposta e si difende dall'allucinazione. Ogni affermazione è verificata nel
codice: sono citati path e nomi esatti di moduli, funzioni e costanti.

Moduli principali:

- Retrieval: `backend/app/retrieval/hybrid.py`, `backend/app/retrieval/rerank.py`,
  `backend/app/retrieval/service.py`
- Agente: `backend/app/agent/graph.py`, `backend/app/agent/tools.py`,
  `backend/app/agent/research.py`, `backend/app/agent/citations.py`,
  `backend/app/agent/prompts.py`
- Knowledge graph: `backend/app/knowledge/extractor.py`,
  `backend/app/knowledge/query.py`, `backend/app/knowledge/prompts.py`
- LLM client: `backend/app/llm.py`
- Valutazione: `backend/app/evaluation.py`, `backend/scripts/evaluate.py`,
  `backend/eval/`
- Configurazione: `backend/app/config.py` (tutti i default citati)

---

## 1. Retrieval

### 1.1 Ricerca ibrida denso + sparso (`retrieval/hybrid.py`)

La ricerca è implementata da `HybridSearcher` (`hybrid.py:140`) sopra Qdrant
(in modalità embedded quando `QDRANT_URL` è vuoto, `config.py:52`).

**Modelli esatti** (default in `config.py:54-55`):

- Denso: `intfloat/multilingual-e5-large` (`dense_model`), eseguito in locale
  via fastembed/ONNX (`ingestion/embeddings.py`). La famiglia E5 richiede i
  prefissi `"query: "` per le interrogazioni e `"passage: "` per i documenti
  (`PASSAGE_PREFIX`/`QUERY_PREFIX`, `embeddings.py:29-30`); il testo
  indicizzato è `titolo\nchunk` (`build_passage_text`, `embeddings.py:84`),
  con budget di 552 token cl100k (`EMBED_TOKEN_BUDGET`, `embeddings.py:43`).
- Sparso: `Qdrant/bm25` (`sparse_model`), BM25 con modificatore IDF
  (`SparseVectorParams(modifier=qm.Modifier.IDF)`, `ingestion/indexer.py:116`).

**Indice**: la collection Qdrant (`qdrant_collection = "chunks"`) ha due
vettori nominati, `DENSE_VECTOR_NAME = "dense"` e
`SPARSE_VECTOR_NAME = "sparse"` (`ingestion/indexer.py:32-33`). Il denso usa
distanza COSINE con quantizzazione scalare INT8 (`quantile=0.99`,
`always_ram=True`) per ridurre la RAM (~4x) con rescoring sui vettori
originali (`indexer.py:103-112`).

**Fusione**: la fusione dei due ranking è delegata a Qdrant con Reciprocal
Rank Fusion nativa — nessuna implementazione Python locale
(`hybrid.py:1-11`). `HybridSearcher.search` (`hybrid.py:147`) esegue
`query_points` con due `qm.Prefetch` (uno per vettore, default
`dense_k=20` e `sparse_k=20`) e `qm.FusionQuery(fusion=qm.Fusion.RRF)` come
query finale (`hybrid.py:178-197`). Gli score RRF grezzi sono molto piccoli
(~0.01-0.03) e NON sono confrontabili con le soglie di pertinenza: per questo
esiste il flag `SearchResult.reranked` (`hybrid.py:111-114`).

**Filtri** (`SearchFilters`, `hybrid.py:32-89`): `source_id`, `doc_type`,
`date_from`/`date_to` (range sul campo payload `modified_at`, timestamp Unix),
`document_ids` (`MatchAny`: vincolo a un insieme chiuso di documenti, usato
dal ciclo agentico per i documenti collegati dal knowledge graph). I filtri
sono applicati sia ai prefetch sia alla query finale.

**Concorrenza**: in modalità embedded Qdrant non tollera accessi concorrenti;
la query gira sotto lo stesso `asyncio.Lock` dell'indice e in
`asyncio.to_thread` (il client è sincrono) (`hybrid.py:8-11, 164-199`). Gli
embedding (bloccanti) sono calcolati in parallelo in thread separati
(`hybrid.py:169-172`).

**Risultato** (`SearchResult`, `hybrid.py:92-114`): `chunk_id`,
`document_id`, `title`, `snippet` (max 300 caratteri, ripulito dal prefisso
`[Sezione: ...]` e dai marcatori markdown, `make_snippet` `hybrid.py:117`),
`context_text` (finestra estesa del chunk), `score`, `page`, `section_path`,
`source_name`, `doc_type`, `modified_at`, `bboxes` (rettangoli di layout per
l'evidenziazione nel PDF viewer), `text` (grezzo, per il reranker),
`reranked` (provenienza dello score).

### 1.2 Reranker cross-encoder (`retrieval/rerank.py`)

- Modello esatto: `BAAI/bge-reranker-v2-m3` (`reranker_model`,
  `config.py:56`), cross-encoder di sentence-transformers, ~1-2 GB scaricati
  da HuggingFace al primo uso reale (`rerank.py:1-8, 56-62`).
- È un singleton lazy (`Reranker.get()`, `rerank.py:40-45`); il caricamento
  avviene in un thread. Se `reranker_enabled` è False o il
  caricamento/predict fallisce, la ricerca NON fallisce: i risultati tornano
  inalterati con un warning (graceful degradation, `rerank.py:64-84, 116-124`).
- Quando interviene: `retrieve()` (`service.py:42`) lo applica dopo la ricerca
  ibrida se `rerank=True`. Nell'agente il flag è
  `agent_rerank_enabled` (default True, `config.py:100`): il tool
  `hybrid_search` (`agent/tools.py:87`) riordina l'intero pool
  (`rerank_top_n=limit`) senza perdere passaggi. La route `POST /api/search`
  lo usa sempre (`api/routes_search.py:50-57`).
- Input al cross-encoder: coppie `(query, titolo + "\n" + testo del chunk)`
  troncate a 2000 caratteri (`_RERANK_TEXT_MAX_CHARS`, `rerank.py:26,
  107-115`). Si usa il testo del chunk (il passaggio citabile), non la
  finestra estesa, per non gonfiare lo score con contenuto adiacente fuori
  tema.
- Output: `score` sovrascritto col valore del cross-encoder (scala sigmoid
  [0,1]) e `reranked=True` (`rerank.py:126-130`); ordinamento per score
  decrescente, restituiti i primi `top_n` (default 5).

### 1.3 Servizio di retrieval (`retrieval/service.py`)

`retrieve()` (`service.py:42`) orchestra: ricerca ibrida → (se `session`
fornita) scarto dei chunk orfani non più presenti nel DB (deriva indice/DB,
es. documenti eliminati, `service.py:64-74`) → reranking opzionale. Condivide
lo stesso `VectorIndex` della pipeline di ingestion: in embedded il path
Qdrant è bloccato dal primo client, quindi non esiste mai un secondo client
sullo stesso path (`service.py:1-9`).

### 1.4 Query al knowledge graph (`knowledge/query.py`)

Il KG entra nel retrieval in due modi: come strumenti esposti all'agente e
come nodo `kg_lookup` del grafo (vedi §3). Funzioni di sola lettura:

- `search_entities` (`query.py:52`): match LIKE case-insensitive su
  `name`/`display_name`, con conteggio dei documenti che menzionano l'entità.
- `entity_context` (`query.py:78`): documenti che menzionano l'entità e
  relazioni in entrambe le direzioni, con controparti e documento di
  provenienza.
- `documents_for_entities` (`query.py:167`): documenti che menzionano le
  entità date; `mode="any"` (almeno una) o `mode="all"` (tutte — quesiti
  multi-documento). Nomi confrontati in forma normalizzata.
- `match_entities_in_text` (`query.py:224`): riconoscimento DETERMINISTICO
  (nessuna chiamata LLM) delle entità citate in un testo: prefiltro per
  sottostringa + regex con confini di parola (`(?<!\w)nome(?!\w)`); nomi più
  corti di 4 caratteri ignorati (`_MIN_NAME_MATCH_LEN`, `query.py:23`);
  elenco entità cacheato 120 s per engine DB (`_ENTITY_CACHE_TTL_S`,
  `query.py:30`), invalidato a ogni scrittura del grafo.
- `entity_subgraph` (`query.py:387`): espansione BFS delle relazioni fino a
  `depth` salti (nell'agente `kg_agent_depth = 1`, `config.py:85`).
- `full_graph` (`query.py:307`): top-N entità per numero di documenti e soli
  archi interni al sottoinsieme, per la visualizzazione UI.

**Come il KG entra nel contesto della risposta** (`agent/graph.py:534-597`):
il nodo `kg_lookup` (solo percorso agentic, solo con `kg_enabled`) riconosce
le entità del quesito con `match_entities_in_text`, poi:

1. le RELAZIONI del sottografo diventano `kg_context`: righe testuali
   `- Soggetto —predicato→ Oggetto (da «Titolo documento»)`, al massimo 20
   (`_MAX_KG_RELAZIONI_CONTESTO`, `graph.py:138`), aggiunte al messaggio di
   sintesi con l'etichetta esplicita "CONTESTO DAL KNOWLEDGE GRAPH ...
   (guida, NON fonte citabile)" — regola ribadita nel `SYNTHESIZE_PROMPT`;
2. i DOCUMENTI collegati (`kg_document_ids`) alimentano una ricerca ibrida
   vincolata in `retrieve` (filtro `document_ids`), limitata a
   `kg_agent_max_extra_docs = 3` documenti non già coperti (`config.py:86`,
   `graph.py:674-704`).

Tutto best-effort: grafo vuoto o errore → flusso identico a quello senza KG
(`graph.py:579-583`).

---

## 2. Soglie anti-allucinazione

### 2.1 Valori esatti

In `backend/app/config.py:68-69`:

```python
retrieval_min_score: float = 0.15
retrieval_strong_score: float = 0.60
```

Le soglie si applicano SOLO agli score del cross-encoder di reranking (scala
sigmoid [0,1]). Il commento in `config.py:58-67` documenta la calibrazione
sul corpus reale (26/08): i documenti pertinenti stanno a ~0.2-0.5, quelli
fuori tema sotto 0.1 (caso reale: 0.02); una soglia min a 0.30 provocava
over-astensione su domande brevi — per questo il default è 0.15.
(Nota: documenti di progetto più vecchi riportano 0.30/0.60; il default del
codice, più recente, è 0.15/0.60. Il file `.env` di produzione può
sovrascrivere i valori via `RETRIEVAL_MIN_SCORE`/`RETRIEVAL_STRONG_SCORE`,
vedi `.env.example:73-74`.)

Semantica delle due soglie:

- sotto `retrieval_min_score` → l'agente SI ASTIENE invece di generare;
- sotto `retrieval_strong_score` → i passaggi passano dal grading di
  rilevanza (nodo `evaluate`) anche sui percorsi fast/ReAct, e la confidence
  è limitata a "media" in `verify`.

### 2.2 Applicabilità: solo score rerankati

`_reranked_max_score` (`graph.py:335-351`) restituisce
`(max_score, scores_reranked)`: `scores_reranked` è True solo se TUTTI i
risultati non esclusi hanno `reranked=True`. Con score RRF grezzi o misti il
gate è saltato con un warning esplicito ("score non rerankati (RRF o misti):
gate di pertinenza saltato", `graph.py:917-923`) e la sintesi procede: le
soglie non sarebbero confrontabili su quella scala. È il compromesso
documentato: se il reranker non è disponibile il sistema resta funzionante ma
con una difesa in meno.

### 2.3 Astensione sotto soglia: messaggio esatto

In `synthesize` (`graph.py:873-949`), prima di qualunque chiamata LLM:

- se `scores_reranked and max_score < retrieval_min_score` → astensione;
- altrimenti, check dei termini distintivi (§2.4) → eventuale astensione.

La risposta di astensione è costruita da `_abstain_answer` (`graph.py:850-870`)
ed è deterministica (nessuna chiamata LLM). Testo esatto:

```
Nel materiale indicizzato non ho trovato documenti pertinenti a: «<quesito>».
```

Il prefisso è la costante `ABSTENTION_PREFIX = "Nel materiale indicizzato non
ho trovato documenti pertinenti"` (`backend/app/evaluation.py:19`),
centralizzata così la batteria di valutazione la riconosce senza duplicare la
stringa. Se esistono risultati "vicini" ma non pertinenti, il messaggio
aggiunge i titoli dei primi 3 documenti distinti:

```
 I documenti più vicini trovati (forse cercavi questi) sono: «Titolo 1»; «Titolo 2».
```

L'astensione ha `confidence = "alta"` (è un esito certo), nessuna citazione,
un warning `astensione: <motivo>` e uno step SSE dedicato `abstain`
(`graph.py:938-949`). Il nodo `verify` la salta ("astensione: verifica non
necessaria", `graph.py:1030-1034`) e non viene mai rigenerata dal refine
(`graph.py:1330`).

Caso limite distinto: se NON c'è alcun passaggio (ricerche a vuoto o grading
CRAG che ha scartato tutto), la risposta di cortesia è
(`graph.py:896-911`):

```
Non ho trovato passaggi pertinenti nei documenti indicizzati per rispondere
al quesito. Ho eseguito le ricerche: <query1>; <query2>. Prova a riformulare
la domanda o ad arricchire l'archivio.
```

### 2.4 Gate dei termini distintivi

Anche con score rerankati ALTI il sistema verifica deterministicamente che i
passaggi parlino dell'oggetto del quesito (il caso reale "quesito sul
ricevitore NICE Florx2r, passaggi solo su manuali Siemens").

`_distinctive_terms(question)` (`graph.py:358-391`) estrae i termini
"distintivi" (lowercase):

- sequenze tra virgolette (`"..."`, `“...”`, `«...»`, lunghezza 2-60);
- token con cifre E lettere o con maiuscole interne: codici/modelli tipo
  "Florx2r", sigle tipo "DURC";
- parole Capitalizzate non a inizio frase (nomi propri); l'inizio frase è
  riconosciuto dopo i caratteri di `_SENTENCE_BREAK = ".!?:;\n"`
  (`graph.py:355`);
- i numeri puri (es. anni) NON sono considerati: troppo soggetti a
  riformulazioni.

`_passages_mention_any(passages, terms)` (`graph.py:394-400`) cerca ogni
termine (sottostringa case-insensitive) in `titolo + context_text + snippet`
di ciascun passaggio selezionato: se NESSUN passaggio menziona NEMMENO un
termine distintivo → astensione, a costo zero. Se il quesito non ha termini
distintivi (es. "come funziona il rimborso spese?") il check non si applica.

Test di riferimento: `backend/tests/test_antihallucination.py`
(`test_distinctive_terms_estrazione`, `test_astensione_su_termini_distintivi_assenti`,
`test_termini_distintivi_presenti_sintesi_normale`).

### 2.5 Grading condizionale (CRAG gate)

`_needs_grading` (`graph.py:1273-1284`): se gli score sono rerankati e il
massimo è sotto `retrieval_strong_score`, anche i percorsi fast e ReAct
passano dal nodo `evaluate` (grading CRAG) prima della sintesi. Sopra soglia
(o con score non confrontabili) si va diretti: latenza invariata sui quesiti
facili. Test: `test_fast_path_con_evidenza_debole_passa_dal_grading`,
`test_fast_path_con_evidenza_forte_niente_grading`,
`test_react_con_evidenza_debole_passa_dal_grading`.

---

## 3. Il flusso agentico completo

### 3.1 Grafo e stati (`agent/graph.py`)

Grafo LangGraph (`StateGraph(AgentState)`, `build_graph` `graph.py:1336`).
Topologia (docstring `graph.py:3-13`):

```
START → classify ─┬─ fast ────→ retrieve ─┬─ fast ────→ synthesize → verify → END
                  └─ agentic → plan → kg_lookup ─┬─ react → research ─┬─ ok ─┘
                                                 │                    └─ ko → retrieve
                                                 └─ graph → retrieve ─┤
                                         evaluate ←───────────────────┘
                                           └─ (lacune, iterazioni < max) → retrieve
(con reflexion attiva: verdict "fragile" in verify → refine → verify, una sola volta)
```

Stato condiviso `AgentState` (`graph.py:160-190`): `question`,
`standalone_question`, `history`, `path`, `sub_questions`,
`pending_queries`, `kg_context`, `kg_document_ids`, `react_ok`,
`excluded_passages`, `abstained`, `scores_reranked`, `confidence`,
`critic_issues`, `refined`, più le chiavi con reducer che si accumulano tra i
nodi: `queries_run` (add), `results` (dict-or per `chunk_id`), `warnings`,
`steps`.

Nodi:

- **classify** (`graph.py:471`): decide `fast` vs `agentic` (in caso di dubbio
  o di JSON irrecuperabile → `agentic`, percorso prudente) e, con storia
  conversazionale, produce la `standalone_question` (riferimenti risolti, es.
  "e nel 2024?" → quesito autonomo). La riformulazione è tracciata nello step
  e finisce in audit.
- **plan** (`graph.py:503`): scompone il quesito agentic in max 5
  sotto-quesiti di ricerca autonomi; guardia lingua
  (`research.ensure_italian_query`): una query non italiana è sostituita dal
  quesito autonomo.
- **kg_lookup**: vedi §1.4.
- **research** (`graph.py:1219`): research agent ReAct (default con
  `agent_mode = "react"`, `config.py:106`); su `ReActUnavailable` imposta
  `react_ok=False` e il grafo ripiega sul ciclo classico
  `retrieve`/`evaluate` con warning tracciato.
- **retrieve** (`graph.py:600`): esegue le interrogazioni pendenti IN
  PARALLELO con tetto `retrieve_max_concurrency = 3` (`config.py:128`), ogni
  task con la propria sessione DB; fusione deterministica per `chunk_id` con
  score massimo. Applica HyDE se `hyde_enabled` (default True): per ogni
  interrogazione il modello scrive prima un passaggio ipotetico
  (`HYDE_PROMPT`, temperatura 0.3 — 0.7 produceva derive lessicali), si fanno
  due ricerche ibride e si fondono per score massimo
  (`research.hyde_search` `research.py:280`, `fuse_hits` `research.py:258`).
  Il testo HyDE non italiano è scartato. Poi l'eventuale ricerca vincolata ai
  documenti del KG (§1.4).
- **evaluate** (`graph.py:722`): valutazione di copertura + grading CRAG
  (`crag_enabled`, default True): il modello classifica ogni passaggio come
  `rilevante`/`parziale`/`irrilevante`; gli irrilevanti finiscono in
  `excluded_passages` (saltati dalla selezione), i parziali restano con score
  dimezzato (`_CRAG_PARTIAL_WEIGHT = 0.5`, `graph.py:141`). Se l'evidenza è
  insufficiente propone 1-3 `refined_queries`; il loop `evaluate → retrieve`
  è limitato da `max_agent_iterations = 3` (`config.py:89`).
- **synthesize**: vedi §2 e §4; selezione dei passaggi con
  `_select_passages` (`graph.py:298`): max 12 passaggi
  (`_MAX_PASSAGGI_SINTESI`), max 3 per documento (`_MAX_PER_DOCUMENTO`),
  dedup dei quasi-duplicati (chiave: primi 200 caratteri del testo
  normalizzato, `_dedup_key` `graph.py:290` — intercetta lo stesso contenuto
  indicizzato in documenti diversi).
- **verify**: vedi §3.3.
- **refine**: vedi §3.4.

### 3.2 Strumenti disponibili (`agent/tools.py`, `agent/research.py`)

Funzioni async pure, senza decoratori LangChain: con `agent_mode="graph"` è
il grafo a invocarle; con `agent_mode="react"` sono esposte al modello via
function calling OpenAI (`TOOLS_SPEC`, `research.py:140-248`):

- `hybrid_search(query, limit=8)`: ricerca ibrida + reranking (tool
  principale);
- `read_document(document_id)`: documento intero (metadati + `context_text`
  dei chunk in ordine); nel loop ReAct l'esito è troncato a 2000 caratteri
  (`_READ_MAX_CHARS`);
- `list_documents(source_id?, doc_type?)`: metadati dei documenti noti (max
  10 mostrati al modello, `_LIST_MAX_ITEMS`);
- `cerca_entita(query)`: entità del KG per nome;
- `contesto_entita(entity_id)`: documenti e relazioni di un'entità.

(`documenti_per_entita` esiste in `tools.py:253` ed è esposta via REST/MCP,
ma non è tra gli schemi del loop ReAct.) Gli strumenti KG con `kg_enabled`
spento rispondono con un messaggio esplicito invece di fallire
(`_KG_DISABLED_DETAIL`, `tools.py:30`).

Loop ReAct (`research.run_research`, `research.py:465`): fino a
`react_max_rounds = 6` round (`config.py:108`); ogni chiamata a strumento è
tracciata come step `research_tool`; 2 round consecutivi senza tool_call
validi (`_MAX_MALFORMED_ROUNDS`, `research.py:65`) o eccezione LLM →
`ReActUnavailable` → fallback al grafo classico. Un singolo strumento
fallito non ferma il loop: l'errore rientra nei messaggi e il modello può
correggere il tiro. Le query generate in inglese sono sostituite dal quesito
autonomo (guardia lingua `looks_italian`/`ensure_italian_query`,
`research.py:94-126`).

### 3.3 Verify, critic e confidence composita

`verify` (`graph.py:1016-1154`) lavora solo sulle citazioni effettivamente
presenti nella risposta:

1. Risposta articolata SENZA marcatori `[n]` pur avendo passaggi disponibili
   → fragile per definizione: `confidence = "bassa"`, criticità esplicita che
   diventa feedback di refine ("riscrivila facendo terminare OGNI
   affermazione fattuale con il marcatore...") (`graph.py:1041-1064`).
2. Verifica di aderenza (`VERIFY_PROMPT`): `supported` true/false +
   `unsupported_claims`. `supported=false` → warning con le affermazioni
   contestate.
3. Critic avversariale (`critic_enabled`, default True; `CRITIC_PROMPT`): un
   secondo prompt cerca attivamente di CONFUTARE ogni affermazione usando
   solo i passaggi citati. Verdict `solido`/`parziale`/`fragile` → confidence
   `alta`/`media`/`bassa` (`_CONFIDENCE_DA_VERDICT`, `graph.py:143`); le
   `issues` diventano warning e restano nello stato come `critic_issues`.

CONFIDENCE COMPOSITA (`graph.py:1125-1148`): la confidence finale è il MINIMO
(`_min_confidence`) tra:

- il verdict della critic;
- "bassa" se `supported=false` (le affermazioni contestate sono aggiunte a
  `critic_issues` anche con critic spenta);
- "bassa" se verdict "parziale" MA con affermazioni non supportate;
- "bassa" se il max score rerankato è sotto `retrieval_min_score`, "media" se
  sotto `retrieval_strong_score`.

Il badge non può quindi dichiarare "alta" su una risposta scollegata dalle
fonti o su evidenza debole. La confidence è esposta nell'evento SSE `answer`
(`api/routes_chat.py:216-224`). Test:
`test_confidence_composita_score_deboli_limitano_il_verdict`,
`test_confidence_composita_aderenza_fallita_e_bassa`.

### 3.4 Reflexion: refine una tantum

`_route_after_verify` (`graph.py:1317-1333`): con `reflexion_enabled`
(default True, richiede la critic) e `confidence == "bassa"` e nessuna
rigenerazione già fatta e nessuna astensione → nodo `refine`
(`graph.py:1157`). La risposta è RIGENERATA una sola volta con
`REFINE_PROMPT` (bozza + criticità + passaggi), riversata in streaming: la
route SSE emette un evento `reset` allo step `refine` e il client sostituisce
la bozza con la versione corretta (`api/routes_chat.py:188-194`). Il grafo
torna a `verify` per la riverifica; il flag `refined` evita il loop. Il
refine scatta anche quando la confidence "bassa" arriva dall'aderenza fallita
o dalle soglie di pertinenza, non solo dalla critic. Test:
`backend/tests/test_reflexion.py`, `test_aderenza_fallita_innesca_refine`.

### 3.5 Semaforo LLM_MAX_CONCURRENCY

In `backend/app/llm.py:38-54`: semaforo GLOBALE lazy di dimensione
`llm_max_concurrency` (default 2, `config.py:44`). Ogni chiamata al modello
lo attraversa — incluso lo streaming, per l'INTERA durata dello stream
(`llm.py:326-337`): verso un provider seriale come Ollama lo slot resta
occupato finché il modello genera, evitando che job paralleli (es.
estrazione KG) si accumulino e affamino la chat. Nessun rischio di deadlock:
l'agente fa chiamate sequenziali, mai annidate.

Resilienza (`llm.py`): retry con backoff esponenziale + jitter fino a
`llm_max_retries = 4` tentativi su errori transitori (HTTP 429/500/502/503/
504, timeout, rete; `_TRANSIENT_STATUSES` `llm.py:58`), rispettando l'header
`Retry-After`; errori permanenti (400/401/403) e saldo esaurito/account
sospeso (`LLMInsufficientBalanceError`, marcatori "insufficient balance"/
"suspended") falliscono subito. Nello streaming il retry è possibile solo
PRIMA del primo delta (`llm.py:340-341`). Le chiamate JSON dei nodi usano
`extract_json` (tollera code fence e testo extra, `llm.py:350`) con UN retry
di correzione (`graph._chat_json` `graph.py:243`).

### 3.6 Stream interrotto: "(risposta interrotta)"

In `api/routes_chat.py:242-262`: se il client si disconnette a metà stream,
il task dell'agente è cancellato e atteso (sessione DB e chiamata LLM
rilasciate subito), ma i token già generati NON vanno persi: la risposta
parziale è persistita nello storico della sessione con il suffisso esatto

```
_(risposta interrotta)_
```

(aggiunto dopo `"\n\n"`), in un task schermato dalla cancellazione con
`asyncio.shield` (il generatore viene chiuso in un contesto già cancellato).
Nessuna riga di audit per le risposte parziali: l'agente non ha prodotto un
esito completo (`_persist_partial_answer`, `routes_chat.py:94-110`).

Contratto SSE (`routes_chat.py`): eventi `step`, `token`, `reset`,
`citations`, `answer` (con `path`, `warnings`, `confidence`), `done`,
`error`; keep-alive come commento `: keep-alive` dopo 15 s di silenzio
(`_HEARTBEAT_SECONDS`).

### 3.7 Budget di contesto: SYNTHESIZE_MAX_CONTEXT_TOKENS vs OLLAMA_CONTEXT_LENGTH

- `synthesize_max_context_tokens = 10000` (default `config.py:94`, ribadito
  nel `docker-compose.yml:83`): budget di token (stima cl100k via tiktoken,
  `chunker.count_tokens`) per il contesto passato alla sintesi.
  `_fit_citation_map_to_budget` (`graph.py:428-459`) taglia i passaggi meno
  rilevanti (l'ultimo della mappa ordinata per score) finché prompt + elenco
  + extra (KG + storia) rientrano nel budget; almeno un passaggio è sempre
  conservato — il budget è una guardia best-effort, non un motivo per
  rinunciare a rispondere. Il taglio avviene PRIMA di assegnare i numeri di
  citazione. Test: `backend/tests/test_synthesize_budget.py`.
- `OLLAMA_CONTEXT_LENGTH: "16384"` e `OLLAMA_NUM_PARALLEL: "1"` nel
  `docker-compose.yml:19-20` (lato server Ollama): col default di Ollama
  (4096 diviso su 2 slot = ~2048 token/richiesta) i prompt RAG col corpus
  reale venivano TRONCATI e il modello rispondeva fuori tema. 16k con slot
  singolo: KV cache ~3 GB, sta in VRAM col modello. Il budget di sintesi
  (10000) è dimensionato per lasciar spazio a prompt di sistema, storia,
  contesto KG e margine di generazione dentro i 16384.

---

## 4. Citazioni (`agent/citations.py`)

Flusso: i passaggi selezionati per la sintesi sono numerati da
`build_citation_map` (`citations.py:50`): dedup per `chunk_id` (tiene lo
score migliore) e ordine per score decrescente — il passaggio più rilevante è
sempre `[1]`. Nei prompt i passaggi sono presentati con header
`[n] «titolo» — fonte: <sorgente>, data: <data>, pag. <n>`
(`graph._format_passage`, `graph.py:408`).

Il modello marca le affermazioni con `[n]` (regola del `SYNTHESIZE_PROMPT`:
ogni affermazione fattuale DEVE terminare col marcatore; si possono citare
più passaggi insieme `[1][3]`). `parse_citations` (`citations.py:65`) estrae
i marcatori con la regex `\[(\d{1,3})\]` (`_MARKER_RE`), li risolve nei
metadati del passaggio, li restituisce in ordine di prima comparsa e
deduplicati; quelli senza passaggio corrispondente finiscono in
`invalid_markers` e producono un warning ("marcatori di citazione non
risolvibili nella risposta: [n]...", `graph.py:995-999`).

Formato della citazione risolta (`Citation`, `citations.py:24-40`):

```json
{
  "n": 1,
  "document_id": 42,
  "title": "Contratto di manutenzione 2024.pdf",
  "page": 3,
  "section_path": ["Cap. 2", "Garanzia"],
  "snippet": "...",
  "source_name": "Cartella di rete IA-LOCALE",
  "score": 0.83,
  "bboxes": [{"l": ..., "t": ..., "r": ..., "b": ..., "coord_origin": "BOTTOMLEFT"}]
}
```

`score` è il punteggio di pertinenza del passaggio (cross-encoder se
rerankato, RRF altrimenti); `bboxes` sono i rettangoli del passaggio nel PDF
(punti, origine bottom-left) usati dal viewer per evidenziare il passaggio
citato — vuota per documenti senza coordinate.

Validazione euristica: `strip_or_report_uncited` (`citations.py:99`) segnala
una risposta "non banale" (≥80 caratteri e ≥2 frasi, `_MIN_LEN_NON_BANALE` /
`_MIN_FRASI_NON_BANALE`) priva di marcatori. Il controllo rigoroso è però a
monte: `verify` tratta la mancanza totale di citazioni come risposta fragile
da rigenerare (§3.3), non solo come warning.

---

## 5. Knowledge graph

### 5.1 Estrazione (`knowledge/extractor.py`, `knowledge/prompts.py`)

Per ogni documento indicizzato (`extract_for_document`, `extractor.py:138`):

1. raccolta del testo (`context_text` dei chunk in ordine), troncato a
   `kg_max_chars_per_doc = 12000` caratteri (`config.py:80`);
2. una chiamata LLM in JSON mode con `EXTRACT_PROMPT` (un retry di
   correzione su JSON imperfetto);
3. upsert delle entità (match su `name` normalizzato lowercase/spazi
   compattati + `type`), max `kg_max_entities_per_doc = 40`;
4. cancellazione e riscrittura di mention e relation del documento
   (idempotente al re-ingest); relazioni solo tra entità estratte, predicato
   in snake_case (max 128 caratteri);
5. pulizia delle entità orfane e invalidazione della cache.

Contratto best-effort: nessuna eccezione verso la pipeline; ogni fallimento è
riportato in `error` del report (`{"document_id", "n_entities",
"n_mentions", "n_relations", "error"}`) e il documento resta `indexed`.

Tipi di entità (vocabolario chiuso, `ENTITY_TYPES` `extractor.py:31-42`):
`persona`, `organizzazione`, `contratto`, `progetto`, `data`, `importo`,
`prodotto`, `altro`. Il prompt impone: solo entità esplicitamente nominate
(niente "il cliente" senza nome proprio), nome canonico completo, date e
importi nella forma in cui compaiono, predicati brevi in italiano snake_case
(es. `ha_firmato`, `scade_il`, `ammonta_a`), solo relazioni esplicitamente
affermate dal testo.

`kg_enabled` è False di default (`config.py:79`): l'estrazione costa UNA
chiamata LLM per documento. Il grafo si popola anche a posteriori con
`backend/scripts/kg_build.py` (tutti i documenti `indexed`, o uno solo con
`--document-id N`), senza reindicizzare.

### 5.2 Endpoint REST (`api/routes_kg.py`)

Tutti autenticati; con KG disabilitato rispondono 404 con dettaglio su come
attivarlo (`_require_kg`, `routes_kg.py:28-31`):

- `GET /api/kg/entities?q=&limit=` — ricerca testuale entità
  (`limit` 1-100, default 10);
- `GET /api/kg/entities/{entity_id}` — contesto completo (documenti +
  relazioni con controparti);
- `GET /api/kg/graph?limit=` — vista grafica: top-`limit` entità per numero
  di documenti (`limit` 1-2000, default 300), soli archi interni al
  sottoinsieme, più `total_nodes`/`total_edges` complessivi
  (`query.full_graph`);
- `GET /api/kg/documents?entities=a,b&mode=any|all` — documenti che
  menzionano le entità (nomi separati da virgola); `mode=all` richiede che le
  menzionino TUTTE.

---

## 6. Valutazione

### 6.1 Scoring (`backend/app/evaluation.py`)

`score_answer` (`evaluation.py:55`) è una funzione pura (nessuna chiamata
LLM, nessun I/O) con tre controlli indipendenti di peso uguale:

- `expected_substrings`: ogni sottostringa attesa deve comparire nella
  risposta (case-insensitive);
- `must_cite`: almeno una citazione presente;
- `min_results`: passaggi recuperati ≥ soglia.

`passed` è True solo se TUTTI i controlli passano; `score` è la frazione di
controlli superati.

Quesiti anti-allucinazione (`expects_no_answer: true`): il controllo
sostituisce i tre standard — la risposta attesa è un'ASTENSIONE riconosciuta
da `is_abstention` (`evaluation.py:33`) tramite i pattern
`_NO_ANSWER_PATTERNS` (`evaluation.py:24-30`): "non ho trovato", "non
contiene informazioni", "non risultano informazioni", "non sono presenti
informazioni", "non è presente alcuna informazione". Una risposta "nel
merito" su un fatto assente dal corpus è esattamente il fallimento che la
batteria deve intercettare.

### 6.2 Batterie in `backend/eval/`

- `questions.example.json`: 5 quesiti normali di esempio (rimborso
  trasferte, approvazioni, scadenze, sanzioni, dati personali) con
  `expected_substrings`, `must_cite: true`, `min_results` 1-2.
- `questions.hallucination.json`: 10 quesiti — 5 con
  `expects_no_answer: true` su oggetti/entità INVENTATI (ricevitore "NICE
  Florx2r", centralina "BFT Athos AC A25", ditta "Verdini Impianti Srl",
  "ingegner Tarquinio Malaspina", plotter "Ricoh ZX-9100") e 5 normali con
  `must_cite`/`min_results` (contratti, DURC, corsi STEM/PNRR, preventivi,
  email su scadenze/pagamenti). Serve da guardia contro l'eccesso di
  prudenza: il sistema deve astenersi su ciò che non esiste E rispondere su
  ciò che esiste.

Il runner si aspetta di default `eval/questions.json` (da creare copiando
una delle batterie, o passare `--questions`).

### 6.3 Runner (`backend/scripts/evaluate.py`)

Esegue l'agente REALE (`run_agent`) su ogni quesito e produce un report
tabellare a console + JSON (default `eval_report.json`).

```bash
cd backend
uv run python scripts/evaluate.py --questions eval/questions.hallucination.json --verbose
uv run python scripts/evaluate.py --from-feedback   # + quesiti dai feedback negativi
```

Prerequisiti: LLM configurato e corpus già indicizzato. Codici di uscita:
0 = tutti superati; 1 = almeno un fallito; 2 = errore di configurazione.
`--from-feedback` riesegue i quesiti dei feedback negativi dal DB come
batteria supplementare (sezione separata, non influenza l'exit code): le
segnalazioni utente diventano regressioni monitorate.

Metriche (`hallucination_metrics`, `scripts/evaluate.py:148`):

- `hallucination_rate`: tra i quesiti `expects_no_answer`, frazione con
  risposta NEL MERITO invece di astensione (il caso da eliminare);
- `over_abstention_rate`: tra i quesiti normali, frazione degenerata in
  astensione (guardia contro l'eccesso di prudenza).

Il report JSON include `overall_score`, `passed`/`failed`, le metriche e,
per ogni quesito, risposta, percorso, warning, `abstained`,
`expects_no_answer`, dettaglio dei check e latenza.

Nota operativa (da `AGENTS.md`): con Qdrant embedded il file dell'indice è
bloccato dal processo dell'app — l'eval end-to-end va lanciata con l'app
ferma oppure fatta passare via API. Al 24/08 la batteria anti-allucinazione
sul corpus reale dava 10/10 con `hallucination_rate` 0.0.

Test relativi: `backend/tests/test_evaluation.py`,
`test_antihallucination.py` (inclusi `test_score_expects_no_answer_*` e
`test_hallucination_metrics`).

---

## 7. I prompt di sistema (`agent/prompts.py`, `knowledge/prompts.py`)

Ruolo e struttura (riassunto, senza riprodurli integralmente). Tutti in
italiano; i nodi che si aspettano JSON usano `extract_json` con un retry di
correzione.

- `CLASSIFY_PROMPT`: decide `fast` (fatto puntuale, una ricerca) vs
  `agentic` (confronti, raffronti temporali, aggregazioni, più domande
  implicite) e produce la `standalone_question` risolvendo pronomi e
  riferimenti alla conversazione. In caso di dubbio: `agentic`.
- `PLAN_PROMPT`: scomposizione in max 5 sotto-quesiti autonomi ottimizzati
  per ricerca ibrida (entità, nomi propri, date espliciti; niente "esso",
  "questo"); un sotto-quesito per versione/data nei confronti.
- `EVALUATE_PROMPT`: giudizio di copertura + grading CRAG per-passaggio
  (`grades`: rilevante/parziale/irrilevante), `sufficient`, `missing` e 1-3
  `refined_queries` non ripetute.
- `SYNTHESIZE_PROMPT`: il cuore anti-allucinazione lato generazione. Regole:
  rispondere ESCLUSIVAMENTE dai passaggi; verificare prima che riguardino
  esattamente l'oggetto del quesito (esempio esplicito NICE Florx2r vs
  manuali Siemens: l'unica risposta ammessa è dichiarare l'assenza, MAI
  adattare contenuti di un oggetto diverso); ogni affermazione fattuale
  termina con `[n]`; niente fatti/cifre/date inventati né conoscenza
  esterna; se i passaggi non bastano, dichiararlo; la conversazione
  precedente è solo per tono, MAI fonte di fatti; il contesto KG è solo una
  guida, MAI fonte citabile; niente bibliografia finale.
- `VERIFY_PROMPT`: aderenza fattuale della risposta ai passaggi citati
  (`supported`, `unsupported_claims` testuali); ignora stile e forma.
- `RESEARCH_PROMPT`: istruisce il research agent ReAct: raccogliere passaggi
  con gli strumenti (non scrivere la risposta), variare le interrogazioni,
  coprire tutti i sotto-quesiti, usare il KG come guida, lavorare in
  italiano.
- `HYDE_PROMPT`: scrivere un estratto PLAUSIBILE di documento aziendale (3-5
  frasi) che risponderebbe al quesito; i dettagli inventati servono SOLO
  come interrogazione e non sono mai mostrati all'utente.
- `CRITIC_PROMPT`: critico avversariale — tentare attivamente di confutare
  ogni affermazione principale usando solo i passaggi citati (cifre/date
  diverse, condizioni omesse, ambito più ristretto, deduzioni non
  esplicitate); verdict `solido`/`parziale`/`fragile` + `issues`.
- `REFINE_PROMPT`: riscrittura della bozza fragile affrontando UNA PER UNA
  le criticità, solo dai passaggi; esito ammesso: dichiarare che il
  materiale non contiene informazioni sull'oggetto, senza citazioni
  forzate.
- `EXTRACT_PROMPT` (KG): estrazione di entità (tipi chiusi, nomi canonici)
  e relazioni (predicati snake_case, solo affermazioni esplicite) da un
  documento.

---

## 8. Limiti noti e onestà del sistema

- **Il gate di pertinenza richiede il reranker.** Se il cross-encoder non è
  disponibile (download fallito, `reranker_enabled=false`, macchina debole),
  gli score restano RRF e le soglie `retrieval_min_score` /
  `retrieval_strong_score` NON sono applicabili: il gate è saltato con un
  warning e la difesa si riduce al check dei termini distintivi, ai prompt e
  a verify/critic (`graph.py:917-923`).
- **Euristica dei termini distintivi.** È volutamente conservativa: i numeri
  puri (anni, importi) non sono distintivi, le maiuscole a inizio frase sono
  escluse, il match è per sottostringa case-insensitive. Quesiti senza
  termini distintivi non attivano il check; un quesito su un anno o un
  importo specifico può quindi ricevere una risposta "nel merito" costruita
  su passaggi che citano un anno diverso — mitigato da verify/critic, non
  eliminato.
- **Copertura della selezione.** La sintesi vede al massimo 12 passaggi
  (`_MAX_PASSAGGI_SINTESI`) e al massimo 3 per documento
  (`_MAX_PER_DOCUMENTO`), con dedup dei quasi-duplicati (chiave sui primi
  200 caratteri normalizzati). Conteggi esaustivi o elenchi completi
  distribuiti su molti documenti ("quanti contratti ci sono in tutto",
  "somma di tutte le righe di queste tabelle") non sono garantiti: la
  risposta copre i passaggi recuperati, non l'intero corpus. Per i
  documenti duplicati/copie (stesso testo in file diversi) resta solo la
  copia con score migliore: la citazione punta a UNA delle copie e la
  confidence non segnala l'esistenza delle altre.
- **Confidence è una stima LLM.** Il minimo composito (§3.3) lega il badge
  ai segnali duri (aderenza, score), ma il componente principale resta il
  verdict di un modello: può sbagliare in entrambe le direzioni. Va letta
  come indicazione, non come garanzia.
- **KG troncato e best-effort.** L'estrazione vede solo i primi 12000
  caratteri per documento (`kg_max_chars_per_doc`) e max 40 entità; il
  contesto KG in sintesi è limitato a 20 relazioni ed è per contratto una
  guida, mai una fonte citabile. Un fallimento del KG non interrompe mai il
  flusso.
- **Tabelle.** Le tabelle sono preservate in conversione e indicizzate come
  chunk dedicati con descrizione testuale (si veda la documentazione di
  ingestion), ma operazioni di counting/aggregazione riga-per-riga restano
  affidate al modello su finestre di testo: non c'è un motore di calcolo, e
  la verifica di aderenza controlla le affermazioni, non la completezza
  aritmetica.
- **Stream e job.** Il semaforo LLM (default 2 slot) serializza di fatto le
  generazioni verso Ollama: un quesito lungo in corso rallenta gli altri
  utenti; le risposte interrotte restano nello storico marcate
  `_(risposta interrotta)_` senza audit né citazioni.
- **L'astensione dipende dal retrieval.** Tutte le difese a valle (gate,
  termini distintivi, verify, critic) operano sui passaggi RECUPERATI: se la
  ricerca ibrida non recupera affatto il documento giusto, il sistema si
  astiene (corretto), ma se recupera materiale affine fuori tema con score
  alto e senza termini distintivi nel quesito, l'ultima linea resta il
  prompt di sintesi + verify/critic — non una prova formale.

---

## Riferimenti rapidi

| Componente | Path | Nomi chiave |
|---|---|---|
| Ricerca ibrida | `backend/app/retrieval/hybrid.py` | `HybridSearcher`, `SearchFilters`, `SearchResult`, `make_snippet` |
| Reranking | `backend/app/retrieval/rerank.py` | `Reranker`, `BAAI/bge-reranker-v2-m3` |
| Servizio retrieval | `backend/app/retrieval/service.py` | `retrieve` |
| Grafo agente | `backend/app/agent/graph.py` | `AgentState`, `build_graph`, `run_agent`, `_distinctive_terms`, `_reranked_max_score`, `_needs_grading` |
| Strumenti | `backend/app/agent/tools.py` | `hybrid_search`, `read_document`, `list_documents`, `cerca_entita`, `contesto_entita` |
| ReAct/HyDE | `backend/app/agent/research.py` | `run_research`, `hyde_search`, `fuse_hits`, `ensure_italian_query`, `TOOLS_SPEC` |
| Citazioni | `backend/app/agent/citations.py` | `Citation`, `build_citation_map`, `parse_citations`, `strip_or_report_uncited` |
| Prompt | `backend/app/agent/prompts.py` | `CLASSIFY/PLAN/EVALUATE/SYNTHESIZE/VERIFY/RESEARCH/HYDE/CRITIC/REFINE_PROMPT` |
| KG | `backend/app/knowledge/{extractor,query,prompts}.py` | `extract_for_document`, `match_entities_in_text`, `full_graph`, `EXTRACT_PROMPT` |
| LLM client | `backend/app/llm.py` | `KimiClient`, `_get_llm_semaphore`, `extract_json` |
| Eval | `backend/app/evaluation.py`, `backend/scripts/evaluate.py` | `score_answer`, `is_abstention`, `ABSTENTION_PREFIX`, `hallucination_metrics` |
| Config | `backend/app/config.py` | `retrieval_min_score=0.15`, `retrieval_strong_score=0.60`, `synthesize_max_context_tokens=10000`, `llm_max_concurrency=2` |
| Test | `backend/tests/test_antihallucination.py`, `test_agent*.py`, `test_reflexion.py`, `test_react.py`, `test_synthesize_budget.py`, `test_evaluation.py` | — |
