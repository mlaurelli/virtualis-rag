# 05 — Frontend

Guida tecnica al frontend di Virtualis: SPA React + TypeScript servita dal
backend FastAPI in produzione e da Vite in sviluppo. Radice: `frontend/`.

## 1. Stack e struttura del progetto

### Stack (`frontend/package.json`)

- **React 18.3** + **react-dom**, nessun framework di state management.
- **react-router-dom v7** per il routing (BrowserRouter).
- **TypeScript ~6.0** in modalità strict (`frontend/tsconfig.app.json`:
  `strict`, `noUnusedLocals`, `noUnusedParameters`, `erasableSyntaxOnly`,
  `verbatimModuleSyntax`).
- **Vite 8** + `@vitejs/plugin-react` per dev server e build
  (rolldown-based; binding opzionale `@rolldown/binding-darwin-arm64`).
- **oxlint** come linter (config `frontend/.oxlintrc.json`).
- Dipendenze funzionali mirate:
  - `react-markdown` + `remark-gfm` per il rendering delle risposte
    dell'assistente;
  - `pdfjs-dist` per il viewer PDF in-app (caricato lazy);
  - `d3-force` per il layout force-directed del knowledge graph;
  - `@playwright/test` per la suite E2E.

### Script npm

| Script | Comando | Note |
|---|---|---|
| `dev` | `vite` | dev server su porta 5173 |
| `build` | `tsc -b && vite build` | typecheck + bundle in `frontend/dist/` |
| `lint` | `oxlint` | |
| `preview` | `vite preview` | serve la build |
| `qa:shots` | `node scripts/qa-shots.mjs` | screenshot QA delle pagine (entrambi i temi) |
| `test:e2e` | `npm run build && node e2e/clean-data.mjs && playwright test` | build + dati puliti + suite |

### Proxy di sviluppo (`frontend/vite.config.ts`)

In dev, tutte le chiamate `/api/*` sono proxate verso il backend:
`target` = `process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000'`,
`changeOrigin: true`, `secure: false` (accetta il cert self-signed della
produzione, utile puntando il proxy a un tunnel SSH verso il server).
In produzione è il backend FastAPI a servire direttamente `frontend/dist/`
come SPA, quindi non esistono variabili d'ambiente runtime lato client:
la base URL dell'API è sempre il path relativo `/api`.

### Layout delle directory

```
frontend/src/
├── main.tsx            # bootstrap: StrictMode + BrowserRouter + ThemeProvider + AuthProvider
├── App.tsx             # SetupGate, routing, guard, ErrorBoundary globale
├── api/
│   ├── client.ts       # wrapper fetch: token, errori tipizzati, upload, blob protetti
│   ├── sse.ts          # consumo dello stream SSE di POST /api/chat
│   └── types.ts        # contratto API completo (tipi derivati da backend/API.md)
├── auth/
│   ├── AuthContext.tsx # AuthProvider, RequireAuth, RequireAdmin
│   └── context.ts      # createContext + hook useAuth
├── theme/
│   ├── ThemeContext.tsx# ThemeProvider (data-theme su <html>, localStorage)
│   └── context.ts      # hook useTheme, THEME_KEY = 'virtualis_theme'
├── pages/              # una route per file (vedi §3)
├── components/         # componenti riusabili (vedi §5)
│   └── admin/          # pannelli della pagina Amministrazione
├── styles/global.css   # design system completo, unico foglio di stile
└── utils/openDocument.ts
```

### Routing e bootstrap

`main.tsx` monta l'albero: `StrictMode > BrowserRouter > ThemeProvider >
AuthProvider > App`. In `App.tsx`:

- `SetupGate` interroga `GET /api/setup/status`; finché `needs_setup` è true
  (nessun utente nel sistema) dirotta tutto su `/setup`. Se il backend non
  risponde non blocca la UI (stato `done`). Un utente già autenticato conta
  come setup completato.
- L'`ErrorBoundary` globale avvolge tutto il routing.
- Route pubbliche: `/setup`, `/login`.
- Route protette da `RequireAuth` dentro il `Layout`: `/` (redirect a
  `/chat`), `/chat`, `/chat/:sessionId`, `/search`, `/documents`, `/m365`,
  `/knowledge`, `/viewer/:documentId` (lazy via `React.lazy` + `Suspense`
  perché include pdfjs), `/admin` (ulteriormente protetta da `RequireAdmin`).
- Catch-all `*` → redirect a `/chat`.

### State management

Nessuna libreria esterna: lo stato vive nei componenti con
`useState`/`useReducer`-style updates immutabili, e due React context
(Auth, Theme). Le pagine tengono il proprio stato di caricamento/errore;
il polling è fatto con `setInterval` dentro `useEffect`. La sessione è
persistita in `localStorage` (chiavi `virtualis_token`, `virtualis_user`,
`virtualis_theme`).

## 2. Autenticazione lato client

### Client HTTP (`src/api/client.ts`)

Wrapper centralizzato su `fetch` con base URL `/api`:

- `getToken()/setSession()/clearSession()/getStoredUserJson()` gestiscono
  token JWT e JSON utente in `localStorage`.
- `api.get/post/patch/delete` serializzano il body JSON e aggiungono
  `Authorization: Bearer <token>` se presente; `api.postPublic` salta
  l'auth (login, setup).
- Ogni risposta non-ok diventa un `ApiError(status, detail)` con il
  messaggio preso dal campo `detail` del body (`{"detail": "..."}` formato
  FastAPI).
- Su **401**: `handleUnauthorized()` pulisce la sessione e invoca il
  callback registrato con `setUnauthorizedHandler` (l'`AuthProvider` ci
  attacca `logout`); senza callback, redirect forzato a `/login`.
- `uploadFile(path, formData)` per upload multipart (`POST /api/ingest/upload`).
- `fetchProtectedBlob(path)` scarica una risorsa binaria autenticata come
  `Blob`; `openProtectedFile(path, fragment)` la apre in una nuova scheda
  come object URL (revoca ritardata di 60 s). Serve perché il backend
  richiede il Bearer token anche sui file originali, che un semplice
  `<a href>` non può inviare.
- Helpers feedback: `submitFeedback` (`POST /api/feedback`),
  `getAdminFeedback` (`GET /api/admin/feedback?limit=&rating=`),
  `getAdminFeedbackStats` (`GET /api/admin/feedback/stats`).

### AuthContext (`src/auth/AuthContext.tsx`, `src/auth/context.ts`)

`AuthContextValue = { user, initializing, login, logout }`.

- All'avvio, se esiste un token salvato, lo valida con `GET /api/auth/me`;
  in caso di fallimento pulisce la sessione. `initializing` resta true
  finché la verifica non è conclusa (le guard mostrano "Caricamento…").
- `login(username, password)` → `POST /api/auth/login` (pubblico), salva
  `access_token` + `user` in localStorage e nello stato.
- Guard `RequireAuth`: se non autenticato → `<Navigate to="/login">` con
  `state.from` = path richiesto (usato dal login per tornare indietro).
- Guard `RequireAdmin`: non autenticato → `/login`; autenticato non-admin
  → redirect a `/chat`.

### Login e setup

- `LoginPage` (`/login`): form username/password; errori distinti per 401
  ("Credenziali non valide"), 403 (messaggio del backend, es. utente
  disattivato) e rete. Dopo il login torna a `state.from` o `/chat`.
  Include il toggle del tema (la pagina è fuori dal `Layout`).
- `SetupPage` (`/setup`): wizard di primo avvio, raggiungibile solo finché
  `GET /api/setup/status` riporta `needs_setup=true`. Validazioni client:
  username non vuoto, password ≥ 8 caratteri, conferma coincidente.
  `POST /api/setup` crea il primo admin, poi login automatico e redirect a
  `/chat`. Gestisce 409 (setup già completato) e 422.

## 3. Le pagine

Tutte le pagine sono accessibili a utenti autenticati di qualunque ruolo,
salvo dove indicato. La colonna "Permessi" riporta il vincolo lato client;
il backend applica comunque i propri controlli.

### ChatPage — `/chat`, `/chat/:sessionId`

**Scopo**: conversazione con l'agente documentale. È la pagina principale.
Componenti: `ChatView`, `MessageList`, `CitationPanel`, `StepTimeline`
(indiretto), sidebar sessioni interna alla pagina.
Permessi: tutti gli utenti.

API usate:

- `GET /api/chat/sessions` — elenco sessioni (sidebar).
- `GET /api/chat/sessions/{id}` — storico di una sessione (404 → redirect a `/chat`).
- `DELETE /api/chat/sessions/{id}` — eliminazione (con ricarica elenco).
- `POST /api/chat` — invio messaggio con stream SSE (vedi §4).

Dettagli: la sidebar mostra le prime 100 sessioni (`SESSIONS_PAGE`), con
"Mostra altre" incrementale, skeleton durante il caricamento e stato
vuoto/errore dedicati. Cambiando sessione nella URL viene abortito
l'eventuale stream in corso (i suoi token altrimenti finirebbero nella
sessione appena aperta). Il pannello citazioni laterale (`CitationPanel`)
si apre al click su un marker `[n]` e cambia il layout
(`chat-layout with-citations`).

### SearchPage — `/search`

**Scopo**: ricerca semantica diretta nei chunk indicizzati (senza agente).
Componenti: `FilterBar`. Permessi: tutti.

API:

- `GET /api/ingest/status` — le fonti per il filtro (route accessibile a
  tutti i ruoli).
- `POST /api/search` — body `{query, filters?, limit: 20}`; filtri
  `source_id`, `doc_type`, `date_from`, `date_to` (i vuoti sono rimossi).

Ogni risultato è una card cliccabile che apre il documento originale alla
pagina del chunk via `openDocument`.

### DocumentsPage — `/documents`

**Scopo**: archivio indicizzato, upload manuale, stato della coda di
ingestion, sync delle cartelle di rete. Componenti: `UploadZone`,
`DocumentTable`, `Pagination`, `ConfirmModal`. Permessi: tutti; il
pulsante di sync compare solo se `GET /api/admin/sources` riesce (di
fatto solo admin) ed esistono sorgenti `local_fs` abilitate; la DELETE è
admin-only lato backend.

API:

- `GET /api/documents?offset=&limit=50&status=` — elenco paginato, filtro
  per stato (`pending|processing|indexed|failed`).
- `GET /api/ingest/status` — stato coda, per-source e ultimi documenti.
  Polling adattivo: 4 s con coda attiva o sync appena avviata
  (`POLL_ACTIVE_MS`), 15 s a riposo (`POLL_IDLE_MS`). La tabella viene
  ricaricata solo quando la "firma" dei contatori cambia
  (`ingestSignature`), così copre anche indicizzazioni più veloci
  dell'intervallo di polling.
- `POST /api/ingest/upload` — multipart (in `UploadZone`); mostra esito e
  id documento.
- `POST /api/ingest/sync` con body `{}` — "Aggiorna da cartella di rete":
  sincronizza tutte le sorgenti `local_fs` abilitate. Il pulsante resta
  disabilitato almeno 8 s (`SYNC_HOLD_MS`) dopo la 202 e mostra l'ultimo
  aggiornamento (dal `last_sync_at` live del polling, con fallback su
  quello caricato una tantum).
- `DELETE /api/documents/{id}` — eliminazione con `ConfirmModal`
  (rimuove anche chunk e file originale).

Nota sul "requeue": il frontend **non** espone un pulsante di requeue; i
documenti `failed` si riaccodano solo via API backend
(`POST /api/ingest/requeue`). La pagina mostra però gli ultimi fallimenti
in un alert con titolo e messaggio d'errore.

### KgPage — `/knowledge`

**Scopo**: esplorazione del knowledge graph (entità e relazioni estratte
dai documenti). Componenti: `KgGraph`. Permessi: tutti. Se il KG è
disabilitato (`404` sulla lista) mostra una pagina guida con le
istruzioni di attivazione (`KG_ENABLED=true`, `scripts/kg_build.py`).

API:

- `GET /api/kg/entities?q=&limit=50` — ricerca entità con debounce 300 ms;
  query vuota elenca le prime entità. Le risposte fuori ordine sono
  scartate con un sequence number (`requestSeq`).
- `GET /api/kg/entities/{id}` — contesto entità: descrizione, documenti
  che la menzionano, relazioni (mostrate divise in "come soggetto" /
  "come oggetto", con link alla controparte e al documento di origine).
- `GET /api/kg/graph?limit=` — usato da `KgGraph` nella vista "Grafo".

Due viste con toggle Tabella/Grafo (`role="tablist"`). Badge colorati per
il vocabolario chiuso dei tipi entità (`persona`, `organizzazione`,
`contratto`, `progetto`, `data`, `importo`, `prodotto`, `altro` — fallback
`altro` per tipi sconosciuti).

### M365Page — `/m365`

**Scopo**: import di contenuti da un tenant Microsoft 365 (OneDrive,
SharePoint, mailbox). Permessi: **solo admin** (guard esplicita nella
pagina oltre al backend: gli altri ruoli vedono un avviso). La voce di
menu nel `Layout` è mostrata solo agli admin.

API:

- `POST /api/m365/test` — verifica credenziali.
- `POST /api/m365/device/begin`, `GET /api/m365/device/status` — flusso
  device code (polling 5 s finché non autorizzato, countdown di scadenza
  con tick 1 s).
- `POST /api/m365/start` — avvio job; 409 gestito ("import già in corso").
- `GET /api/m365/status` — polling 2 s solo mentre `state === 'running'`.
- `POST /api/m365/cancel` — annullamento.

Tre modalità di autenticazione (radio): **device code** (default,
consigliata, compatibile MFA; tenant da specificare), **utenza**
(username/password, ROPC),
**app registration** (client credentials: tenant_id, client_id,
client_secret). Le credenziali non vengono mai persistite.

Scoping: checkbox OneDrive/SharePoint/Posta (almeno una obbligatoria) e
campo "Limita a un utente" (UPN). Sezione **"Opzioni avanzate"**
(`<details class="m365-advanced">`):

- `folders`: textarea, una cartella per riga nel formato `upn:/percorso`;
  le righe non vuote diventano `folders: string[]` nel body, textarea
  vuota → `folders: null` (tutto il drive). È l'equivalente UI dell'import
  mirato già possibile via API.
- `max_file_mb`: numero 1–500 (clamp lato client), tetto di dimensione
  per singolo file.

Il body di `/m365/start` è `{auth, include_onedrive, include_sharepoint,
include_mail, user, folders, max_file_mb, dry_run}`. "Avvia anteprima"
usa `dry_run: true` (solo scansione); "Avvia import completo" chiede
conferma con `ConfirmModal`.

Vista di progresso live (card dedicata quando `state !== 'idle'`):
stepper a 4 fasi (`auth → scan → download → ingest`), barra di progresso
con percentuale (indeterminata prima del download), file corrente,
statistiche (file scaricati/totali, byte, velocità, ETA, falliti,
saltati), conteggi di indicizzazione, riepilogo scansione (file OneDrive,
file SharePoint con n. siti, messaggi, allegati), banner di stato
terminale, elenco errori per file collassabile e feed eventi live
(ultimi 12, `aria-live="polite"`). A import completato: link "Vai ai
documenti".

### AdminPage — `/admin`

**Scopo**: amministrazione. Permessi: **solo admin** (`RequireAdmin` nella
route). Quattro tab: Utenti, Fonti, Registro audit, Feedback.

- `UsersPanel` (`components/admin/UsersPanel.tsx`): `GET /api/admin/users`,
  `POST /api/admin/users` (creazione con ruolo user/admin),
  `PATCH /api/admin/users/{id}` (cambio ruolo, attiva/disattiva inline).
- `SourcesPanel` (`components/admin/SourcesPanel.tsx`):
  `GET /api/admin/sources`, `POST /api/admin/sources` (il form crea solo
  fonti `local_fs` con `config.path`), `PATCH /api/admin/sources/{id}`
  (abilita/disabilita), `POST /api/ingest/sync {source_id}` per la sync
  della singola fonte. Le fonti gdrive/m365 si configurano via API.
- `AuditPanel` (interno ad `AdminPage.tsx`): `GET /api/admin/audit?limit=50&offset=`
  con `AuditTable` (righe espandibili: risposta, chunk consultati, passi
  in JSON) e `Pagination`.
- `FeedbackPanel` (`components/admin/FeedbackPanel.tsx`): statistiche
  (`GET /api/admin/feedback/stats`) + elenco filtrabile per giudizio
  (`GET /api/admin/feedback?rating=1|-1&limit=100`).

### ViewerPage — `/viewer/:documentId?page=N`

**Scopo**: viewer PDF in-app con evidenziazione del passaggio citato.
Permessi: tutti. Caricata **lazy** (pdfjs fuori dal bundle principale).

API: `GET /api/documents/{id}/file` via `fetchProtectedBlob`.

La citazione (con i `bboxes` in punti PDF, origine bottom-left, formato
Docling) viaggia nello `state` della navigazione da `CitationPanel`
("Apri ed evidenzia"); a un reload diretto resta solo la pagina dalla
query string. Se il blob non è `application/pdf`, fallback: apertura in
nuova scheda come object URL (comportamento dei non-PDF).

### LoginPage e SetupPage

Vedi §2. Sono le uniche pagine fuori dal `Layout` e senza guard
(`SetupGate` gestisce il dirottamento a `/setup`).

## 4. Chat in profondità

### Lo stream SSE (`src/api/sse.ts`)

`POST /api/chat` risponde con uno stream Server-Sent Events. Si usa
`fetch` + `ReadableStream` (non `EventSource`) perché serve una POST con
header `Authorization`. Il parser accumula i chunk in un buffer
(`TextDecoder` in modalità stream), splitta sugli eventi separati da riga
vuota (`\n\n`), e per ogni riga `data: {json}` dispaccia l'evento
tipizzato `ChatStreamEvent`; un blocco residuo a fine stream viene
comunque processato, le righe non JSON ignorate.

Eventi (`src/api/types.ts`):

| Tipo | Payload | Significato |
|---|---|---|
| `step` | `{step, detail}` | passo agentico (classify, plan, kg_lookup, research, research_tool, retrieve, evaluate, synthesize, verify, refine) |
| `token` | `{text}` | token della risposta in streaming |
| `reset` | — | reflexion: la bozza streamata va scartata, arriva la risposta corretta |
| `citations` | `{citations: Citation[]}` | fonti della risposta |
| `answer` | `{text, path, warnings, confidence?}` | risposta finale, percorso (`fast`/`agentic`), avvisi, affidabilità dalla critic |
| `done` | `{session_id, audit_id}` | fine stream |
| `error` | `{message}` | errore applicativo nello stream |

Errori HTTP prima dello stream → `ApiError` (401 → clearSession +
redirect `/login`); gli errori dentro lo stream arrivano via `onError`.

### Flusso di un messaggio (`ChatPage.handleSend`)

1. **Invio**: se non è già in streaming, crea un `AbortController`,
   aggiunge ottimisticamente due messaggi locali (utente + assistant
   vuoto con `streaming: true`) e chiama `streamChat` con
   `{message, sessionId, signal}`. I messaggi hanno una `key` locale
   (`m1`, `m2`, …); quelli da DB usano `db-{id}`.
2. **Streaming**: ogni handler aggiorna l'ultimo messaggio in modo
   immutabile (`updateLast`), così `MessageItem` (memoizzato) ri-renderizza
   solo il messaggio attivo:
   - `onStep` → accoda a `steps` con timestamp client (per la durata);
   - `onToken` → append al contenuto;
   - `onReset` → azzera il contenuto (reflexion);
   - `onCitations` → salva le citazioni;
   - `onAnswer` → se i token non sono arrivati usa `e.text`, più
     `warnings` e `confidence` (badge "Affidabilità: alta/media/bassa");
   - `onDone` → fine streaming; se era una nuova sessione naviga a
     `/chat/{session_id}` (la prima risposta dà il titolo alla sessione).
     Poiché `done` non espone l'id del messaggio assistant, la pagina
     ricarica la sessione per recuperarlo: senza id il feedback resta
     nascosto fino al prossimo caricamento. Infine ricarica la sidebar.
   - `onError` → marca fine stream, messaggio di fallback e warning.
3. **Citazioni**: i marker `[n]` nel markdown diventano `CitationMarker`
   cliccabili (vedi §5); il click apre il `CitationPanel` laterale con
   titolo, fonte, pagina, section_path, snippet evidenziato e due azioni:
   "Apri ed evidenzia" (solo se ci sono `bboxes` → `/viewer/:id?page=N`)
   e "Apri originale" (`openDocument`, nuova scheda con `#page=N`).
4. **Feedback**: sotto ogni risposta persistita (`id` noto, non in
   streaming) compare `MessageFeedback` (👍/👎, commento opzionale sul
   negativo; `POST /api/feedback`, upsert lato backend).

### StepTimeline (fasi agentiche)

`StepTimeline` riceve `{steps: StepInfo[], active}` e renderizza un
`<details>` collassabile (aperto durante lo streaming): spinner e
"Elaborazione in corso · N passi" mentre lavora, "N passi dell'agente" a
fine. Ogni passo ha icona unicode, etichetta italiana (`STEP_LABELS`),
dettaglio e durata calcolata dai timestamp client (`at`). Passi futuri
sconosciuti hanno fallback (⚙️ + nome grezzo).

### Errori e interruzioni

- **Stop utente**: pulsante "Interrompi" (al posto di "Invia" durante lo
  streaming) → `abort()` sull'`AbortController`; il `.catch` distingue
  l'abort volontario (solo chiusura dello stato streaming) dagli errori
  veri (warning "Errore di connessione al server." / messaggio API).
- **Cambio sessione / uscita dalla pagina**: abort dello stream attivo.
- **Stream chiuso senza `done`** (connessione interrotta): il `.then`
  dopo `streamChat` chiude comunque lo stato streaming — senza questo
  ramo l'input resterebbe bloccato. Le risposte interrotte vengono
  salvate dal backend con suffisso "(risposta interrotta)".
- **Errori nello stream**: warning nel messaggio + testo di fallback.

Auto-scroll (`MessageList`): salto immediato (non `smooth`) a ogni nuovo
messaggio; durante lo streaming solo se l'utente è già entro 120 px dal
fondo, così può risalire a leggere senza essere trascinato giù.

## 5. Componenti riusabili chiave

| Componente | Props principali | Responsabilità |
|---|---|---|
| `Layout` | — (usa `useAuth`, `useTheme`) | Shell: topbar con brand, nav (voci admin condizionali: Microsoft 365, Amministrazione), toggle tema, logout, `<Outlet/>` |
| `ErrorBoundary` | `children` | Class component globale: errore di rendering → card con pulsante "Ricarica" invece di pagina bianca; logga in console |
| `ChatView` | `messages, streaming, onSend, onStop, onCitationClick` | Area centrale chat: empty state con 3 suggerimenti, input textarea (Invio invia, Maiusc+Invio a capo), pulsanti Invia/Interrompi |
| `MessageList` | `messages, onCitationClick` | Lista messaggi con auto-scroll intelligente; `MessageItem` memoizzato (ruolo, StepTimeline, markdown/testo, badge affidabilità, warnings, feedback) |
| `MarkdownMessage` | `content, citations, onCitationClick` | react-markdown + remark-gfm; attraversa i figli testuali dei tag testo (p, li, td, th, h1–h4, strong, em, del) e sostituisce i marker `[n]` con `CitationMarker`; nel codice i marker restano letterali; HTML raw non renderizzato (no XSS) |
| `CitationMarker` | `citation, onOpen` | Bottone `[n]` inline con popover pure-CSS (`:hover`/`:focus-within`, accessibile da tastiera): titolo, fonte, snippet |
| `StepTimeline` | `steps, active` | Timeline collassabile dei passi agente (vedi §4) |
| `CitationPanel` | `citations, selected, onSelect, onClose` | Pannello laterale fonti citate con azioni viewer/originale (vedi §4) |
| `MessageFeedback` | `messageId` | Pollice su/giù + commento opzionale (Invio o blur), upsert via API |
| `DocumentTable` | `documents, onDelete, sourceNames?` | Tabella documenti: titolo cliccabile (apre originale), tipo, `StatusBadge`, fonte (mappa `source_id`→nome), pagine, dimensione, data, delete |
| `StatusBadge` | `status, error?` | Badge stato indicizzazione; per `failed` l'errore è in tooltip/aria-label |
| `UploadZone` | `onUploaded` | Drag&drop + click per `POST /api/ingest/upload`, con esito/errore |
| `FilterBar` | `sources, filters, onChange` | Filtri ricerca: fonte, tipologia (pdf/docx/xlsx/pptx/txt/md/html), date; "Azzera filtri" |
| `Pagination` | `offset, limit, total, label, onOffsetChange` | Paginazione per offset condivisa (documenti, audit) |
| `ConfirmModal` | `title, children, confirmLabel?, danger?, onConfirm, onCancel` | Modale di conferma generica (click su backdrop = annulla, autofocus su Annulla) |
| `AuditTable` | `entries` | Registro audit con riga espandibile (risposta, chunk, passi JSON) |
| `KgGraph` | `selectedId, onSelect` | Vista a grafo del KG (vedi sotto) |
| `PdfViewer` | `data, initialPage, bboxes` | Viewer PDF canvas + overlay highlight (vedi sotto) |
| `admin/UsersPanel`, `admin/SourcesPanel`, `admin/FeedbackPanel` | — | Tab di AdminPage (vedi §3) |

### KgGraph (dettaglio)

Layout force-directed con **d3-force** (solo fisica, nessun DOM d3): la
simulazione (carica repulsiva Barnes-Hut, link, collisione su raggio,
gravità centrale) muta le posizioni, applicate **in modo imperativo** con
`setAttribute` dentro `requestAnimationFrame` — nessun setState a ogni
tick, la tab resta fluida con centinaia di nodi. Posizioni iniziali a
spirale di Fibonacci (stabili). Archi paralleli aggregati in un unico
arco (spessore ∝ n. relazioni, tooltip con i primi 5 predicati). Raggio
nodo ∝ √document_count (5–26 px). Pan dello sfondo, zoom con rotellina
(listener nativo non-passivo: React attaccherebbe `onWheel` passivo),
drag dei nodi (con `fx/fy` e riscaldamento della simulazione), doppio
click / pulsante "Adatta" per il fit. Etichette a dimensione costante su
schermo via counter-scale con CSS variables (`--kg-label-size`,
`--kg-ring-w`); a zoom basso solo le top-40 entità per document_count.
Toolbar: ricerca con risultati (Enter/dick centra sul nodo), select
limite (100/300/600, passato a `GET /api/kg/graph?limit=`), legenda
cliccabile per filtrare i tipi, conteggio "N entità · M relazioni (su
totali)". Hover/selezione evidenziano il vicinato e attenuano il resto.

### PdfViewer (dettaglio)

`pdfjs-dist` con worker importato come URL (`pdf.worker.min.mjs?url`).
Render della pagina corrente su canvas; overlay SVG con i rettangoli dei
`bboxes` della citazione, convertiti da punti PDF bottom-left (Docling)
al viewport top-left con `viewport.convertToViewportRectangle([l,b,r,t])`.
Toolbar: pagina precedente/successiva, zoom 0,5×–3,0× a passi di 0,25
(default 1,25×). Il render task viene cancellato a ogni cambio
pagina/zoom.

### Utility `openDocument` (`src/utils/openDocument.ts`)

`openDocument(documentId, page, docType?)`: apre il file originale in
nuova scheda come blob autenticato; per i PDF aggiunge il fragment
`#page=N` (se `docType` è omesso assume PDF).

## 6. Tema e CSS

Unico foglio di stile `src/styles/global.css` (~2600 righe), nessun CSS
module né framework: design system a **custom properties**.

### Token e temi

- Token base su `:root`: font (`--font-sans` Inter, `--font-mono`
  JetBrains Mono), scala spaziature `--space-1..8`, raggi
  `--radius-sm/md/lg/full`, ombre `--shadow-sm/md/lg`, `--transition`.
- Colori in due blocchi: `:root, :root[data-theme='dark']` (default
  scuro) e `:root[data-theme='light']`. Superfici (`--bg`, `--bg-raised`,
  `--bg-overlay`, `--bg-hover`), bordi, testo a tre livelli (`--text`,
  `--text-muted`, `--text-faint`), accento e colori semantici
  (`--success`, `--warning`, `--danger`, `--info`, `--violet`, `--pink`,
  ciascuno con variante `-soft`), bolle chat (`--user-bubble`,
  `--assistant-bubble`).
- Il tema è applicato da `ThemeProvider` come attributo `data-theme` su
  `<html>`, persistito in `localStorage` (`virtualis_theme`), default
  dark. Toggle in topbar e nelle pagine login/setup.

### Convenzioni

- Classi semantiche globali: `.btn` (+ `-primary`, `-danger`, `-ghost`,
  `-sm`), `.input`/`.select`/`.textarea`, `.card`, `.badge`
  (+ `badge-pending/processing/indexed/failed`, `badge-fast/agentic`,
  `badge-neutral`), `.alert` (+ `-warning`, `-success`, `-info`), `.table`
  dentro `.table-wrap`, `.empty-state`, `.spinner`, `.skeleton` per gli
  skeleton loader, `.field` per label+controllo.
- Sezioni commentate per area (`/* ---------- Chat ---------- */`, ecc.);
  le pagine complesse hanno namespace dedicati (`.chat-*`, `.kg-*`,
  `.m365-*`, `.pdf-*`, `.feedback-*`).
- Accessibilità: `color-scheme` per tema, `@media
  (prefers-reduced-motion: reduce)` che disattiva animazioni, focus ring
  coerente, ruoli ARIA su controlli custom (tablist, progressbar, dialog).
- Responsive minimo, desktop-first (`@media (max-width: 960px)`).
- Keyframes dedicate: `spin`, `blink` (cursore streaming), `shimmer`
  (skeleton), `message-in`, `step-in`, `m365-pulse`, `m365-indeterminate`.

## 7. Testing e quality

### Lint e typecheck

- `npm run lint` → **oxlint** (`frontend/.oxlintrc.json`: plugin `react`,
  `typescript`, `oxc`; `react/rules-of-hooks: error`,
  `react/only-export-components: warn`).
- `npm run build` → `tsc -b` (typecheck strict su `tsconfig.app.json` +
  `tsconfig.node.json`) + `vite build`. Il typecheck è parte della build,
  non c'è un job separato.
- `make lint` alla radice aggrega ruff + mypy (backend) + oxlint.

### E2E Playwright (`frontend/e2e/`)

- Config `frontend/playwright.config.ts`: un solo progetto Chromium, **1
  worker, niente parallelismo, niente retry** (i test condividono un
  backend reale con SQLite e Qdrant embedded: serializzati per
  determinismo). Timeout 300 s (il primo seed include upload +
  indicizzazione reale con modelli fastembed), trace `retain-on-failure`.
- Il `webServer` avvia uvicorn su porta **8033** con env dedicata:
  `DATA_DIR=e2e/.data` (azzerato da `clean-data.mjs` prima di ogni run),
  `ADMIN_AUTO_CREATE=true`, `WARMUP_ON_START=false`, `KG_ENABLED=false`,
  `MOONSHOT_API_KEY` vuota (la suite **non** esercita il LLM). Il backend
  serve la SPA da `frontend/dist`: `npm run test:e2e` fa build + pulizia
  + test.
- `e2e/env.ts`: costanti condivise (porte, path, credenziali admin di
  test). `e2e/fixtures.ts`: fixture worker-scoped `seed` (login API,
  upload di 2 markdown via pipeline reale con attesa `indexed`, sessione
  chat con citazioni inserita direttamente nel DB via `e2e/seed_chat.py`)
  e helper `loginViaApi` (autentica la pagina scrivendo il token in
  localStorage, stesso meccanismo del login UI).
- Spec: `auth.spec.ts` (gate setup, login ko/ok, logout), `chat.spec.ts`
  (empty state con 3 suggerimenti, markdown, marker citazione con
  popover/pannello, feedback), `documents.spec.ts`, `search.spec.ts`.

### CI (`.github/workflows/ci.yml`)

Tre job: backend (bloccante), **frontend** (bloccante: `npm ci`, build
con typecheck, oxlint) ed **e2e** (informativo, `continue-on-error`:
`npm ci`, `npx playwright install --with-deps chromium`, `npm run
test:e2e`).

### Screenshot QA

`npm run qa:shots` (`frontend/scripts/qa-shots.mjs`): cattura screenshot
di tutte le pagine in entrambi i temi contro un backend con dati demo
(`QA_BASE_URL`, default `http://localhost:8022`; output in
`docs/screenshots`).

## 8. Come estendere il frontend

### Aggiungere una pagina

1. **Tipi**: aggiungere in `src/api/types.ts` le interfacce del contratto
   (commentate con l'endpoint, come il resto del file; la fonte di verità
   è `backend/API.md`).
2. **Pagina**: creare `src/pages/NuovaPage.tsx` seguendo lo schema delle
   esistenti: stato `loading/error/dati`, caricamento in `useEffect`,
   errori come `ApiError` con fallback "Errore di connessione al server.",
   skeleton/spinner per il loading, `.empty-state` per il vuoto, testi in
   italiano, attributi ARIA sui controlli custom.
3. **Route**: in `App.tsx` aggiungere la `<Route>` dentro il gruppo
   `RequireAuth > Layout` (o `RequireAdmin` se admin-only). Se la pagina
   ha dipendenze pesanti, caricarla lazy come `ViewerPage`.
4. **Navigazione**: aggiungere il `NavLink` in `components/Layout.tsx`
   (condizionandolo a `user?.role === 'admin'` se serve).
5. **Stili**: nuova sezione commentata in `global.css` riusando i token
   (`var(--space-*)`, `var(--accent)`, …) e le classi base
   (`.card`, `.btn`, `.table`, `.badge`, `.alert`).
6. **E2E**: spec dedicata in `frontend/e2e/` appoggiandosi a `fixtures.ts`
   (`loginViaApi`, eventuale estensione del `seed`).

### Aggiungere una chiamata API

- Endpoint JSON: usare `api.get/post/patch/delete` (token e gestione
  401/errori sono automatici). Endpoint pubblici: `api.postPublic`.
- Upload multipart: `uploadFile`. File binari protetti:
  `fetchProtectedBlob` / `openProtectedFile`.
- Nuovo stream SSE: seguire il modello di `sse.ts` (fetch + reader, non
  EventSource, perché serve il Bearer header in POST).
- Convenzione errori: leggere `detail` dal body e propagare `ApiError`;
  la UI mostra `err.message` con fallback generico.

### Note operative

- Mai leggere `import.meta.env` per la base URL: è sempre `/api` (proxy
  Vite in dev, stesso origin in produzione).
- Il frontend non deve mai persistere credenziali di terze parti (vedi
  M365: le credenziali vivono solo nello stato del form).
- Le route admin-only vanno protette sia lato client (guard + voce di
  menu condizionale) sia, soprattutto, lato backend: il controllo client
  è solo UX.
