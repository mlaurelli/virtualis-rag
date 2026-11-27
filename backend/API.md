# Virtualis — Contratto API (backend)

Base URL: `/api`. Autenticazione: header `Authorization: Bearer <jwt>` (ottenuto da `/auth/login`).

Gli endpoint marcati **planned** sono definiti come contratto ma saranno implementati nelle fasi successive (chat/agente fase 5, ingestion e retrieval fasi intermedie).

## Convenzioni

- **Errori**: `{"detail": "..."}` con status code appropriato (`400` richiesta non valida, `401` non autenticato, `403` non autorizzato, `404` non trovato, `409` conflitto, `422` validazione fallita, `500` errore interno).
- **Date**: ISO 8601 UTC.
- **Ruoli**: `admin` (gestione utenti/sorgenti/audit), `user` (chat, ricerca, documenti, upload).

---

## Auth

### `POST /api/auth/login`
Login. Accetta **JSON** `{"username": "...", "password": "..."}` oppure form `application/x-www-form-urlencoded` (OAuth2-compatible).

Risposta `200`:
```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "user": {"id": 1, "username": "admin", "role": "admin"}
}
```
Errori: `401` credenziali non valide, `403` utente disattivato,
`429` troppi tentativi falliti (anti brute-force: dopo `LOGIN_MAX_ATTEMPTS`
fallimenti nella finestra `LOGIN_WINDOW_SECONDS` per coppia IP+username;
header `Retry-After` con i secondi di attesa).

### `GET /api/auth/me`
Profilo dell'utente autenticato.

Risposta `200`: `{"id": 1, "username": "admin", "role": "admin"}`

---

## Setup (wizard di primo avvio) — **nuovo**

Endpoint pubblici per la configurazione iniziale: attivi solo finché non
esiste alcun utente. Il frontend dirotta automaticamente su `/setup` quando
`needs_setup` è `true`.

### `GET /api/setup/status` — **nuovo**
Pubblico. Indica se il sistema richiede la configurazione iniziale.

Risposta `200`: `{"needs_setup": true}`

### `POST /api/setup` — **nuovo**
Pubblico, accettato SOLO se `needs_setup` è `true`. Crea il primo
amministratore e restituisce un token, come `/api/auth/login`.

Body: `{"username": "...", "password": "..."}` — password minimo 8 caratteri.

Risposta `201`:
```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "user": {"id": 1, "username": "admin", "role": "admin"}
}
```
Errori: `409` configurazione già completata, `422` validazione fallita
(username vuoto o password < 8 caratteri).

---

## Chat

### `POST /api/chat` — **planned**
Body: `{"message": "...", "session_id": 123?}` — se `session_id` assente, crea una nuova sessione.

Risposta: stream **SSE** (`text/event-stream`). Ogni evento è un messaggio `data:` contenente un JSON con campo `type`:

| type | payload | significato |
|---|---|---|
| `step` | `{"type":"step","step":"classify\|plan\|kg_lookup\|research\|research_tool\|retrieve\|evaluate\|synthesize\|verify\|refine","detail":"..."}` | avanzamento dell'agente (`research*` solo con `AGENT_MODE=react`, `refine` solo con reflexion attiva) |
| `token` | `{"type":"token","text":"..."}` | delta di testo della risposta (streaming) |
| `reset` | `{"type":"reset"}` | il testo streamato finora va scartato: sta arrivando la risposta rigenerata (reflexion, step `refine`) |
| `citations` | `{"type":"citations","citations":[{...}]}` | citazioni usate (vedi sotto) |
| `answer` | `{"type":"answer","text":"...","path":"fast\|agentic","warnings":["..."],"confidence":"alta\|media\|bassa"}` | risposta finale completa |
| `done` | `{"type":"done","session_id":1,"audit_id":42}` | fine stream, riferimenti persistiti |
| `error` | `{"type":"error","message":"..."}` | errore durante l'elaborazione |

Campo `confidence` (additivo, architettura agentica v2): presente solo se la
critic avversariale è abilitata (`CRITIC_ENABLED=true`, default) e la risposta
aveva citazioni da verificare; vale `alta` (verdict "solido"), `media`
("parziale") o `bassa` ("fragile"). Se assente o vuota, il frontend non mostra
il badge di affidabilità.

Reflexion (`REFLEXION_ENABLED=true`, default; richiede la critic): se il
verdict è "fragile", la risposta è **rigenerata una volta** con le criticità
come feedback e riverificata. Allo step `refine` il backend emette l'evento
`reset` seguito dai token della nuova risposta; la `confidence` finale è
quella della riverifica. Client che non conoscono `reset` possono ignorarlo:
l'evento `answer` riporta comunque il testo definitivo.

Formato citazione:
```json
{
  "n": 1,
  "document_id": 7,
  "title": "Manuale operativo.pdf",
  "page": 12,
  "section_path": ["Capitolo 3", "3.2 Procedure"],
  "snippet": "…testo citato…",
  "source_name": "Archivio locale",
  "bboxes": [
    {"page": 12, "l": 50.0, "t": 700.0, "r": 545.0, "b": 680.0,
     "page_width": 595.0, "page_height": 842.0}
  ]
}
```

`bboxes` (campo additivo) contiene i rettangoli del passaggio citato in
punti PDF con origine bottom-left (formato nativo Docling), usati dal viewer
interno per l'evidenziazione; è una lista vuota per i documenti senza
coordinate (non-PDF o indicizzati prima dell'introduzione del campo).

### `GET /api/chat/sessions` — **planned**
Elenco sessioni dell'utente corrente.

Risposta `200`: `{"sessions":[{"id":1,"title":"...","created_at":"...","message_count":5,"last_message":"anteprima ultimo messaggio…"}]}`

(`last_message` è un'estensione additiva: primi ~100 caratteri dell'ultimo messaggio, `null` se la sessione è vuota.)

### `GET /api/chat/sessions/{id}` — **planned**
Dettaglio sessione con messaggi.

Risposta `200`: `{"id":1,"title":"...","created_at":"...","messages":[{"id":10,"role":"user","content":"...","created_at":"..."},{"id":11,"role":"assistant","content":"...","citations":[...],"created_at":"..."}]}`
Errori: `404` sessione inesistente o di altro utente.

### `DELETE /api/chat/sessions/{id}` — **planned**
Elimina la sessione (e i suoi messaggi). Risposta `204`. Errori: `404`.

---

## Ricerca

### `POST /api/search` — **planned**
Body:
```json
{
  "query": "procedura reso merce",
  "filters": {"source_id": 2, "doc_type": "pdf", "date_from": "2024-01-01", "date_to": "2024-12-31"},
  "limit": 20
}
```
Tutti i filtri opzionali. Risposta `200`:
```json
{
  "results": [
    {
      "chunk_id": 1001,
      "document_id": 7,
      "title": "Manuale operativo.pdf",
      "snippet": "…",
      "score": 0.83,
      "page": 12,
      "section_path": ["Capitolo 3"],
      "source_name": "Archivio locale"
    }
  ]
}
```

---

## Documenti

### `GET /api/documents?status=&source_id=` — **planned**
Query param opzionali: `status` (`pending|processing|indexed|failed`), `source_id`. Estensione additiva: paginazione semplice con `offset` (default `0`) e `limit` (default `50`, max `200`).

Risposta `200`: `{"documents":[{"id":7,"source_id":2,"external_id":"docs/manuale.pdf","title":"...","doc_type":"pdf","author":"...","language":"it","status":"indexed","error":null,"page_count":48,"size_bytes":1048576,"modified_at":"...","created_at":"...","updated_at":"..."}], "total": 137}`

(`total` è un'estensione additiva: numero complessivo di documenti che soddisfano i filtri.)

### `GET /api/documents/{id}` — **planned**
Dettaglio documento (stessi campi dell'elenco, più eventuali metadati). Errori: `404`.

### `GET /api/documents/{id}/file` — **planned**
Restituisce il **file originale** (`application/pdf` ecc., con `Content-Disposition: inline`). Il frontend apre i PDF con `#page=N` per saltare alla pagina citata. Errori: `404` documento o file non trovato.

### `DELETE /api/documents/{id}` — **planned**
Elimina documento, chunk associati e file. Risposta `204`. Errori: `404`.

---

## Ingestion

### `POST /api/ingest/upload` — **planned**
Multipart (`file`): carica un documento nella sorgente `upload` e ne avvia l'indicizzazione asincrona.

Risposta `202`: `{"document_id": 7, "status": "pending"}`
Errori: `400` tipo file non supportato o file mancante.

### `POST /api/ingest/sync` — **planned**
Body: `{"source_id": 2?}` — avvia la sincronizzazione di una sorgente (o di tutte quelle abilitate).

Risposta `202`: `{"started": true, "sources": [2]}`

### `GET /api/ingest/status` — **planned**
Stato della coda di indicizzazione.

Risposta `200`:
```json
{
  "queue": {"pending": 3, "processing": 1},
  "per_source": [{"source_id": 2, "name": "Archivio locale", "enabled": true, "last_sync_at": "...", "indexed": 120, "failed": 2}],
  "recent": [{"document_id": 7, "title": "...", "status": "failed", "error": "PDF protetto da password"}]
}
```

---

## Admin (richiede ruolo `admin`)

### `GET /api/admin/users` — **planned**
Elenco utenti. Risposta `200`: `{"users":[{"id":1,"username":"admin","role":"admin","is_active":true,"created_at":"..."}]}`

### `POST /api/admin/users` — **planned**
Body: `{"username":"...","password":"...","role":"user"}`. Risposta `201`: utente creato (senza password). Errori: `409` username duplicato.

### `PATCH /api/admin/users/{id}` — **planned**
Body (campi opzionali): `{"password":"...","role":"admin","is_active":false}`. Risposta `200`: utente aggiornato. Errori: `404`.

### `GET /api/admin/audit?limit=` — **planned**
Log di audit delle risposte (default `limit=100`, max 1000).

Risposta `200`: `{"entries":[{"id":42,"user_id":1,"session_id":5,"question":"...","path":"agentic","steps":[{...}],"retrieved_chunk_ids":[1001,1002],"answer":"...","warnings":[],"latency_ms":2300,"created_at":"..."}]}`

### `GET /api/admin/sources` — **planned**
Elenco sorgenti. Risposta `200`: `{"sources":[{"id":2,"name":"Archivio locale","type":"local_fs","config":{...},"enabled":true,"last_sync_at":"..."}]}`

### `POST /api/admin/sources` — **planned**
Body: `{"name":"...","type":"local_fs|upload|gdrive|m365","config":{...},"enabled":true}`. Risposta `201`: sorgente creata.

### `PATCH /api/admin/sources/{id}` — **planned**
Body (campi opzionali): `{"name":"...","config":{...},"enabled":false}`. Risposta `200`: sorgente aggiornata. Errori: `404`.

---

## Feedback sulle risposte — **nuovo**

Giudizi degli utenti (pollice su/giù) sulle risposte dell'assistente. I feedback negativi alimentano la batteria supplementare di valutazione (`scripts/evaluate.py --from-feedback`).

### `POST /api/feedback` — **nuovo**
Body: `{"message_id":12,"rating":1,"comment":"..."}` — `rating` è `-1` o `+1`, `comment` facoltativo (max 2000 caratteri). Un solo feedback per messaggio: un secondo invio **sovrascrive** il precedente (upsert). Consentito solo su messaggi `assistant` delle proprie sessioni.

Risposta `200`: `{"id":3,"message_id":12,"rating":1,"comment":null,"question":"...","answer_excerpt":"primi 500 caratteri...","created_at":"...","updated_at":"..."}` (`question` ed `answer_excerpt` sono denormalizzati all'invio).

Errori: `400` messaggio non assistant, `404` messaggio inesistente o di un altro utente, `422` rating non valido.

### `GET /api/admin/feedback?rating=&limit=` — **nuovo**
Elenco feedback, più recenti prima. `rating=-1|1` filtra per giudizio, `limit` default 50 (max 500). Richiede ruolo `admin`.

Risposta `200`: `{"entries":[{"id":3,"message_id":12,"username":"mario","rating":-1,"comment":"Fonti mancanti","question":"...","answer_excerpt":"...","created_at":"...","updated_at":"..."}]}`

### `GET /api/admin/feedback/stats` — **nuovo**
Statistiche aggregate. Richiede ruolo `admin`.

Risposta `200`: `{"total":10,"positive":7,"negative":3,"positive_pct":70.0,"recent_negative":[{"id":3,"username":"mario","rating":-1,"comment":"...","question":"...","answer_excerpt":"...","created_at":"...","updated_at":"..."}]}` — `recent_negative` contiene gli ultimi 5 feedback negativi; `positive_pct` è `null` senza feedback.

---

## Knowledge graph (GraphRAG) — **nuovo**

Estrazione di entità e relazioni dai documenti indicizzati, a integrazione del recupero per i quesiti multi-documento. **Disabilitato di default**: richiede `KG_ENABLED=true` e la popolazione del grafo (automatica al re-ingest oppure con `scripts/kg_build.py` per il corpus esistente). Quando è disabilitato, tutti gli endpoint rispondono `404` con dettaglio esplicativo.

### `GET /api/kg/entities?q=&limit=` — **nuovo**
Ricerca testuale (case-insensitive) di entità per nome; `q` vuoto elenca le prime entità.

Risposta `200`:
```json
{
  "entities": [
    {"id": 3, "name": "mario rossi", "display_name": "Mario Rossi", "type": "persona", "description": "Legale rappresentante di Acme S.p.A.", "document_count": 4}
  ]
}
```

### `GET /api/kg/entities/{id}` — **nuovo**
Contesto completo di un'entità: documenti che la menzionano e relazioni di cui è soggetto od oggetto (con controparti e documento di provenienza).

Risposta `200`:
```json
{
  "entity": {"id": 3, "name": "mario rossi", "display_name": "Mario Rossi", "type": "persona", "description": "..."},
  "documents": [{"document_id": 7, "title": "Contratto fornitura 2024.pdf"}],
  "relations": [
    {"id": 12, "predicate": "ha_firmato", "subject": {"id": 3, "display_name": "Mario Rossi", "type": "persona"}, "object": {"id": 5, "display_name": "Contratto 2024/15", "type": "contratto"}, "document_id": 7, "document_title": "Contratto fornitura 2024.pdf"}
  ]
}
```
Errori: `404` entità inesistente o KG disabilitato.

### `GET /api/kg/documents?entities=a,b,c&mode=all|any` — **nuovo**
Documenti che menzionano le entità date (nomi separati da virgola, confronto normalizzato). `mode=all`: solo documenti che le menzionano TUTTE (quesiti multi-documento); `mode=any` (default): almeno una.

Risposta `200`:
```json
{
  "documents": [
    {"document_id": 7, "title": "Contratto fornitura 2024.pdf", "doc_type": "pdf", "matched_entities": ["Mario Rossi", "Acme S.p.A."], "matched_count": 2}
  ],
  "mode": "all"
}
```
Errori: `400` parametro `entities` vuoto, `422` `mode` non valido, `404` KG disabilitato.

---

## Salute

### `GET /api/health`
Nessuna autenticazione. Risposta `200`: `{"status": "ok", "llm_configured": true}`

### `GET /api/health/deep` — **nuovo**
Healthcheck approfondito per il monitoraggio. **Autenticazione admin richiesta** (la risposta espone dettagli infrastrutturali). Verifica:

- `database`: query `SELECT 1` sul DB SQLite;
- `qdrant`: raggiungibilità ed esistenza della collection (modalità `embedded` o `server` secondo `QDRANT_URL`);
- `llm`: **solo** verifica di configurazione (`MOONSHOT_API_KEY` presente), nessuna chiamata al provider;
- `embedder`: modelli denso/sparso configurati e flag `loaded` (i modelli NON vengono caricati dal check: caricamento lazy da 1-2 GB).

Risposta `200` se tutto ok, `503` con lo stesso payload se degradato:
```json
{
  "status": "ok",
  "checks": {
    "database": {"ok": true},
    "qdrant": {"ok": true, "mode": "embedded", "collection": "chunks", "collection_exists": true},
    "llm": {"ok": true, "configured": true, "note": "..."},
    "embedder": {"ok": true, "dense_model": "intfloat/multilingual-e5-large", "sparse_model": "Qdrant/bm25", "loaded": false, "note": "..."}
  }
}
```
Errori: `401` non autenticato, `403` non admin, `503` almeno un check fallito (dettaglio in `checks.<nome>.error`).
