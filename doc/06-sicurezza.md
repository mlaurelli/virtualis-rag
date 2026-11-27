# 06 — Sicurezza

Documento di sicurezza del sistema Virtualis: modello di minaccia,
autenticazione, trasporto, superficie di esposizione, gestione dei segreti,
integrità/disponibilità, audit, privacy e hardening residuo.

Tutte le affermazioni sono verificate nel codice: per ogni meccanismo è citato
il file sorgente di riferimento. Le credenziali reali (password, secret,
indirizzi IP, hostname) sono sostituite da segnaposto come `<password-admin>`,
`<jwt-secret>`, `<ip-lan>`, `<ip-pubblico>`, `<host-server>`.

> Ambito: stato del codice sul branch `main` al 27/08/2026.
> Configurazioni operative, indirizzi e credenziali reali sono esclusi dalla
> versione pubblica.

---

## 1. Modello di minaccia

### 1.1 Cosa proteggiamo

Il patrimonio informativo gestito da Virtualis è costituito da:

- **Documenti aziendali riservati** importati da OneDrive, SharePoint, mailbox
  Microsoft 365, Google Drive, cartella di rete SMB e upload manuali
  (contratti, offerte, mail con allegati, documentazione interna);
- le **copie locali** di quei documenti in `data/uploads/`
  (`backend/app/config.py`, `upload_dir`);
- l'**indice vettoriale** Qdrant embedded in `data/qdrant/` e il **database
  SQLite** `data/virtualis.db` (chunk di testo integrale dei documenti,
  knowledge graph con entità/relazioni, storico chat, audit log, utenti);
- le **credenziali delle sorgenti** (service account Google, eventuale
  client secret del connettore M365) in `sources.config_json`;
- il **segreto JWT** in `data/.jwt_secret` e la **chiave privata TLS** in
  `data/certs/virtualis.key`.

### 1.2 Da chi

- **Attaccante dalla rete locale (LAN)**: la superficie primaria prevista dal
  design. Il servizio è pensato per l'uso interno all'azienda del cliente.
- **Attaccante da Internet**: scenario originariamente escluso (il provider
  filtrava l'handshake TLS dall'esterno), **rientrato in gioco dal 26/08/2026**:
  l'applicazione risulta raggiungibile da IP pubblici per via del port
  forwarding sul router del cliente; nei log sono stati osservati scanner
  automatici con risposte 200 su `/`, `/remote/login`, `/robots.txt`
  (vedi §4).
- **Utente autenticato non admin**: non deve poter amministrare utenti,
  sorgenti, né cancellare documenti (autorizzazione per ruolo, §2.4).
- **Malware/scanner opportunistici**: brute-force su login web e SSH
  (mitigati rispettivamente dal throttle applicativo e da fail2ban, §2.6 e §4).

Fuori scope dichiarato: attaccante con accesso fisico alla macchina, attacchi
alla supply chain delle dipendenze, compromissione del tenant Microsoft 365
del cliente.

### 1.3 Principio architetturale: «nessun dato esce dalla macchina»

- Il **LLM è locale** (Ollama in container Docker sulla stessa macchina,
  `docker-compose.yml` servizio `ollama`): la porta 11434 **non è pubblicata**
  sull'host (commento nel compose: «raggiungibile solo dentro la rete
  compose»). Prompt, documenti e risposte non lasciano mai il server.
- L'endpoint LLM configurato in produzione è `http://ollama:11434/v1`
  (`docker-compose.yml`, `KIMI_BASE_URL`); la variabile `MOONSHOT_API_KEY`
  contiene il valore segnaposto `ollama`, non una chiave reale.
- **Uniche comunicazioni in uscita** previste: le API delle fonti da importare
  (Microsoft Graph, Google Drive) iniziate dal connettore/job, in sola lettura
  (scope `drive.readonly` per Google, `backend/app/connectors/gdrive.py:147`),
  e i download dei modelli Hugging Face al primo avvio.
- I modelli di retrieval (dense `intfloat/multilingual-e5-large`, sparse BM25,
  reranker `BAAI/bge-reranker-v2-m3`, `backend/app/config.py:54-56`) girano
  in-process nel container applicativo: nemmeno gli embedding escono.

---

## 2. Autenticazione e autorizzazione

File di riferimento: `backend/app/auth.py`, `backend/app/api/routes_setup.py`,
`backend/app/config.py`, `backend/app/api/deps.py`.

### 2.1 Hash delle password

- Algoritmo: **argon2** tramite `pwdlib.PasswordHash.recommended()`
  (`backend/app/auth.py:25`). I parametri sono quelli raccomandati dalla
  libreria (argon2id di default in pwdlib).
- Le password in chiaro non sono mai memorizzate: la tabella `users` contiene
  solo `password_hash` (`backend/app/models.py:49-59`).
- **Anti enumerazione utenti / anti timing attack**: al login, se lo username
  non esiste viene comunque verificata la password contro un hash argon2
  fittizio (`_DUMMY_PASSWORD_HASH`, `auth.py:30` e `auth.py:283`), così i tempi
  di risposta non rivelano quali username sono validi. Il messaggio d'errore
  è uniforme («Credenziali non valide») per utente inesistente e password
  errata (`auth.py:285-294`).
- Lunghezza minima password: **8 caratteri** sia nel wizard di primo avvio
  (`routes_setup.py:32`, `password: str = Field(min_length=8, ...)`) sia nella
  creazione/modifica utenti admin (`routes_admin.py:69,74`). Nessun requisito
  di complessità ulteriore (vedi checklist §9).

### 2.2 JWT

- Algoritmo di firma: **HS256** (`auth.py:24`).
- Claim emessi: `sub` (user id), `role`, `iat`, `exp`
  (`auth.py:58-68`). Scadenza: `jwt_expire_minutes`, **default 720 minuti
  (12 ore)** (`config.py:49`).
- Emissione: `POST /api/auth/login` (JSON o form-encoded OAuth2-compatible,
  `auth.py:248-304`) e `POST /api/setup` al primo avvio.
- Verifica: ogni richiesta protetta decodifica il token con
  `decode_access_token` (401 se invalido/scaduto, `auth.py:71-80`) e ricarica
  l'utente dal DB (`get_current_user`, `auth.py:87-111`).
- **Revoca immediata**: `is_active` è ricontrollato a ogni richiesta —
  disattivare un utente invalida all'istante anche i token già emessi e non
  scaduti (`auth.py:93-97`, comportamento documentato nel docstring).
- Non esiste un meccanismo di refresh token né di logout lato server
  (token stateless): la revoca passa solo dalla disattivazione utente o dalla
  rotazione del segreto (vedi checklist §9).

### 2.3 Segreto JWT: dove vive

Gerarchia di risoluzione (`config.py:186-227`, validator
`_default_jwt_secret`):

1. `JWT_SECRET` da env/`.env`, se impostato (precedenza massima, non tocca il
   file);
2. altrimenti il file **`data/.jwt_secret`**, letto se esistente o **generato
   al primo avvio** con `secrets.token_urlsafe(48)` e persistito con
   **permessi 600** (`os.open(..., 0o600)`, `config.py:210`): i token
   sopravvivono ai riavvii;
3. se la persistenza fallisce, ripiego su segreto effimero con warning
   (i token non sopravvivono al riavvio).

In produzione il file vive dentro il volume `./data` dell'host
(`docker-compose.yml:91`). In sviluppo locale è presente in `data/.jwt_secret`
(la cartella `data/` è in `.gitignore`).

### 2.4 Ruoli e route admin-only

Ruoli: `admin` e `user` (`models.py:20-22`). La dipendenza `require_admin`
(`auth.py:114-123`) risponde 403 a chi non è admin; `get_current_user`
risponde 401 a chi non è autenticato.

Route riservate agli admin (verificate per grep su `require_admin`):

- **Tutto il router `/api/admin/*`** tramite `dependencies=[Depends(require_admin)]`
  a livello di router (`routes_admin.py:38-42`):
  - `GET/POST /api/admin/users`, `PATCH /api/admin/users/{id}` — gestione utenti;
  - `GET /api/admin/audit` — consultazione audit log;
  - `GET/POST/PATCH /api/admin/sources[...]` — gestione sorgenti documentali
    (le risposte includono `config_json`, credenziali comprese: dato visibile
    solo agli admin);
  - `POST /api/admin/material/reset` — cancellazione totale del materiale
    (richiede `confirm: true`, rifiuta 409 a pipeline occupata);
  - `POST /api/admin/reindex`, `GET /api/admin/reindex/status`;
- **Tutto il router `/api/m365/*`** (`routes_m365.py`): `test`, `start`,
  `status`, `cancel`, `device/begin`, `device/status` — ogni handler ha
  `Depends(require_admin)`;
- **`/api/admin/feedback` e `/api/admin/feedback/stats`**
  (`routes_feedback.py:23-27`);
- `DELETE /api/documents/{id}` (`routes_docs.py:168-173`);
- `POST /api/ingest/sync` e `POST /api/ingest/requeue`
  (`routes_ingest.py:122-128, 212-216`);
- `GET /api/health/deep` (espone dettagli infrastrutturali,
  `api/main.py:175-189`).

Route autenticate (qualsiasi utente attivo): chat, ricerca, lista documenti,
`GET /api/ingest/status`, feedback propri. Le sessioni di chat e i messaggi
sono **privati per utente**: un messaggio altrui risponde 404 senza rivelarne
l'esistenza (`routes_feedback.py:70-73`).

Tutele aggiuntive sull'account admin:

- l'ultimo amministratore attivo **non può essere degradato né disattivato**
  (`routes_admin.py:122-139`);
- non esiste una delete degli utenti nel contratto API
  (`routes_admin.py:1-6`): si disattiva (`is_active=false`), con revoca
  immediata dell'accesso.

### 2.5 Setup iniziale

Due vie, entrambe in `auth.py:135-174` (`bootstrap_admin`) e
`routes_setup.py`:

- **Wizard di primo avvio** (via preferita): `GET /api/setup/status` riporta
  `needs_setup: true` finché non esiste alcun utente; `POST /api/setup` crea
  il primo admin (password ≥ 8 caratteri) e risponde **409** a setup già
  completato — l'endpoint pubblico si «richiude» da solo dopo l'uso.
- **Bootstrap da env**: attivo solo se richiesto esplicitamente
  (`ADMIN_AUTO_CREATE=true` oppure entrambe `ADMIN_USERNAME`/`ADMIN_PASSWORD`).
  Con i default `admin/admin` viene emesso un warning in log che invita a
  cambiarle subito (`auth.py:160-164`); `.env.example` lo sconsiglia
  esplicitamente in consegna.

In produzione la password admin è stata impostata forte e viene qui indicata
come `<password-admin>`.

### 2.6 Anti brute-force sul login (parametri esatti)

Implementazione in `auth.py:181-230`, parametri in `config.py:160-161`:

- soglia: **`login_max_attempts = 5`** fallimenti;
- finestra: **`login_window_seconds = 300`** secondi (scorrevole);
- chiave del contatore: **coppia `IP client | username normalizzato`**
  (`_throttle_key`, `auth.py:187-190`);
- al superamento: **HTTP 429** con header **`Retry-After`** calcolato sulla
  finestra residua (`check_login_throttle`, `auth.py:199-216`);
- il controllo avviene **prima** di qualunque lavoro (query DB, hash argon2):
  una chiave bloccata non costa nulla (`auth.py:275-278`);
- un login riuscito azzera il contatore della chiave (`auth.py:300`);
- contatore **in memoria del processo**: adeguato al dispiegamento a nodo
  singolo; un riavvio dell'app lo azzera (limite noto, vedi §9).

---

## 3. Trasporto (TLS)

### 3.1 Certificato self-signed

- Generazione: `backend/scripts/gen_cert.py` (target `make cert`).
  Chiave **RSA 2048**, firma SHA-256, validità default **825 giorni** (massimo
  riconosciuto dai browser, `gen_cert.py:42`), estensioni `BasicConstraints
  ca=false` (critical) ed `ExtendedKeyUsage serverAuth`.
- **SAN incluse automaticamente**: hostname della macchina, `localhost`,
  `127.0.0.1` e l'IP LAN rilevato; altre SAN (es. IP statico, hostname
  aggiuntivi) passabili come argomenti posizionali (`gen_cert.py:119-152`).
  In produzione la SAN copre: `<ip-pubblico>`, `<ip-lan>`, `<host-server>`,
  `localhost`.
- File: `data/certs/virtualis.crt` e `virtualis.key`, quest'ultima scritta con
  **permessi 600** (`gen_cert.py:107`).
- Attivazione: `TLS_CERT_FILE` + `TLS_KEY_FILE` in `.env`/compose; entrambi
  valorizzati → uvicorn in HTTPS (`backend/app/__main__.py:20-25`). Se solo
  uno dei due è impostato, l'app lo segnala.
- Rigenerazione: rieseguire `backend/scripts/gen_cert.py` (eventualmente con
  SAN aggiornate) e riavviare lo stack.

### 3.2 Perché niente HTTP in chiaro

Le credenziali di login e i token JWT viaggiano in header/body di ogni
richiesta; su una LAN aziendale (con eventuale Wi-Fi) l'HTTP in chiaro li
esporrebbe a sniffing e session hijacking. Il certificato self-signed richiede
l'accettazione dell'eccezione nel browser (o l'installazione nelle CA fidate
dei client): trade-off accettato per un servizio interno senza CA aziendale.

### 3.3 Porte esposte

Da `docker-compose.yml:52-54`:

- `8000:8000` — accesso locale / tunnel SSH;
- `8080:8000` — accesso LAN (ed esterno, vedi §4): l'app risponde **solo in
  HTTPS** su entrambe (TLS è applicato dal processo uvicorn, non da un proxy).

Non esposte: Ollama 11434 (rete compose interna), nessuna porta Qdrant
(embedded in `/data/qdrant`, `docker-compose.yml:62-63`), nessuna porta DB
(SQLite su file).

### 3.4 Header di sicurezza HTTP

Middleware su ogni risposta (`api/main.py:124-149`):
`X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
`Referrer-Policy: strict-origin-when-cross-origin`,
`Permissions-Policy: camera=(), microphone=(), geolocation=()` e una CSP
moderata (`default-src 'self'`, `script-src 'self'`, `object-src 'self'`,
`frame-src 'self'`, worker blob per il viewer PDF). Manca `Strict-Transport-
Security` (vedi §9). CORS: origini limitate a `settings.cors_origins`
(default: dev Vite `localhost:5173/3000`, `config.py:153-155`),
`allow_credentials=True` con metodi e header `*`.

---

## 4. Superficie di esposizione

### 4.1 Stato attuale

- L'applicazione è raggiungibile dalla **LAN** del cliente su
  `https://<ip-lan>:8000` (e `:8080`).
- **Dal 26/08/2026 l'app risulta raggiungibile anche da IP pubblici**: il
  port forwarding sul router del cliente (FortiGate) la espone su
  `<ip-pubblico>` e il filtro TLS del provider non blocca più l'handshake.
  Nei log del container si osservano **scanner Internet** con risposte 200 su
  `/`, `/remote/login`, `/robots.txt`. Sullo stesso router l'interfaccia admin
  del FortiGate risulta esposta su una porta pubblica (da segnalare al
  cliente) e altre porte pubbliche inoltrano verso servizi non correlati
  (redirect rotto, DVR videosorveglianza).
- L'IP LAN del server è assegnato via **DHCP dinamico**: la prenotazione sul
  FortiGate è ancora da fare (rischio di cambio IP → SAN del certificato e
  abitudini degli utenti disallineate).

### 4.2 Mitigazioni in atto

- **fail2ban sull'host** (installato il 27/08/2026): jail sshd con **5
  tentativi falliti in 10 minuti → ban 1 ora**
  (`/etc/fail2ban/jail.d/virtualis-sshd.conf`). Protegge SSH, esposto per la
  manutenzione.
- **Anti brute-force applicativo** sul login web (§2.6): 5 fallimenti in
  300 s per coppia IP+username → 429.
- **Password admin forte** (`<password-admin>`, reimpostata il 29/07/2026).
- **Autenticazione obbligatoria** su ogni route dati: gli scanner ottengono la
  SPA e gli endpoint pubblici (`/api/health`, `/api/setup/status`), ma nessun
  contenuto documentale senza token valido.
- TLS attivo su tutte le porte pubblicate (§3).
- Healthcheck dettagliato `/api/health/deep` solo admin: non espone topology
  a chi non è autenticato.

### 4.3 Raccomandazioni

1. **Verificare col cliente il port forwarding sul FortiGate**: se l'accesso
   da Internet non serve, chiuderlo; se serve, valutare di limitarlo.
2. **Prenotazione DHCP** per il server sul FortiGate (IP stabile).
3. Valutare un **allowlist di reti/IP** (lato FortiGate o reverse proxy) per
   l'accesso dall'esterno.
4. Chiudere/spostare l'interfaccia admin del FortiGate esposta su porta
   pubblica.
5. Considerare un jail fail2ban anche per i 429 del backend (log applicativo
   strutturato già presente: `login limitato per troppi fallimenti`).

---

## 5. Gestione dei segreti

### 5.1 Inventario

| Segreto | Dove vive | Note |
|---|---|---|
| Hash password utenti | tabella `users` (SQLite) | argon2, mai in chiaro |
| `<jwt-secret>` | `data/.jwt_secret` (permessi 600) o env `JWT_SECRET` | generato al primo avvio se assente |
| Chiave privata TLS | `data/certs/virtualis.key` (permessi 600) | self-signed |
| Credenziali sorgenti (es. service account Google Drive) | `sources.config_json` in SQLite | **JSON in chiaro nel DB** — vedi §5.2 |
| Credenziali import M365 | **mai persistite** — solo in memoria per la durata del job | verificato, §5.3 |
| Chiave API LLM | non presente in produzione (LLM locale; `MOONSHOT_API_KEY=ollama` segnaposto) | una chiave reale andrebbe solo in `.env` |
| Password SSH/sudo host, password admin web | `AGENTS.md` (escluso da git) | fuori dal repository |

### 5.2 Credenziali delle sorgenti in `config_json`

Il contratto dei connettori prevede le credenziali in `Source.config_json`
(`backend/app/connectors/base.py:21-23`): per Google Drive l'intero JSON del
service account (`connectors/gdrive.py:3-10`), per il connettore M365 storico
`tenant_id`/`client_id`/`client_secret` (`connectors/m365.py:4-11`).
**Non è presente cifratura a riposo**: il JSON è conservato in chiaro nella
colonna `config_json` di SQLite (`models.py:62-73`) ed è restituito dalle API
admin delle sorgenti (`routes_admin.py:207-215`). La protezione affidata a:
DB su disco della macchina on-premise, volume Docker non pubblicato, accesso
DB possibile solo dall'host o dal container, API di lettura limitate al ruolo
admin. I backup (§6.1) includono il DB e quindi anche queste credenziali: gli
archivi vanno protetti di conseguenza. La cifratura di `config_json` è in
checklist (§9).

### 5.3 Credenziali M365: mai persistite (verifica)

Verificato nel codice:

- `routes_m365.py:16-17` (docstring): «Le credenziali NON vengono mai
  persistite: viaggiano nel body della richiesta di avvio e vivono solo per
  la durata del job.»
- `M365AuthIn` (`routes_m365.py:48-76`) esiste solo come modello della
  richiesta; `job.start(body.auth.as_dict(), ...)` lo passa al job in memoria.
- Nel job, le credenziali vivono solo in `GraphClient._auth`
  (`m365_import.py:174-186`) e producono un token tenuto in memoria con
  scadenza (`_token`, `_token_exp`).
- La sorgente «Microsoft 365» creata dal job ha **`config_json={}`** vuoto
  (`m365_import.py:1247-1251`): nessuna credenziale finisce nel DB.
- Device code flow: il **refresh token** vive solo nel dataclass in memoria
  `_device_flow` (`m365_import.py:403-417`); un riavvio dell'app lo perde e
  serve una nuova approvazione.
- Conseguenza nota e accettata: lo stato dei job M365 è in memoria — un
  riavvio dell'app uccide il job, che va rilanciato re-immettendo le
  credenziali (backlog: persistenza dello stato del job, non delle
  credenziali).

### 5.4 Rotazione

- **JWT**: ruotare impostando un nuovo `JWT_SECRET` in `.env` (o cancellando
  `data/.jwt_secret`: ne viene generato uno nuovo al riavvio). Effetto:
  invalidazione immediata di tutti i token → re-login di tutti gli utenti.
- **Password utenti**: `PATCH /api/admin/users/{id}` con nuova password
  (ri-hash argon2, `routes_admin.py:141-142`).
- **Service account Google / secret M365**: rigenerare lato provider e
  aggiornare la sorgente via `PATCH /api/admin/sources/{id}` (per M365
  basta re-immetterli al prossimo import, non essendo persistiti).
- **Certificato TLS**: `backend/scripts/gen_cert.py` + riavvio (§3.1).

### 5.5 Esclusione da git

`.gitignore` (radice repo) esclude: `.env`, `AGENTS.md` (contiene credenziali
operative: non va mai su GitHub), `data/` (DB, upload, `.jwt_secret`,
certificati). Il deploy via `rsync` esclude esplicitamente `.env`, `data/` e
`AGENTS.md` per non sovrascrivere i segreti del server (procedura in
`AGENTS.md`).

---

## 6. Integrità e disponibilità

### 6.1 Backup

Due livelli:

- **Script applicativo** `backend/scripts/backup.py` (target `make backup`):
  archivio `.tar.gz` di `data_dir` con `manifest.json` (data, versione app,
  conteggi documenti/chunk/utenti, modalità Qdrant). Il DB SQLite è copiato in
  modo **consistente con l'API di backup di sqlite3** (`_snapshot_sqlite`,
  `backup.py:68-78`), sicura anche a backend in esecuzione grazie al WAL;
  i file live `virtualis.db-wal/-shm` sono esclusi e sostituiti dalla copia
  consistente. Con Qdrant in modalità server i vettori non sono inclusi (il
  manifest lo dichiara); in produzione Qdrant è embedded → incluso. Il
  restore è **reversibile**: la `data_dir` esistente è rinominata in
  `<data_dir>.pre-restore-<timestamp>` invece di essere cancellata
  (`backup.py:237-241`).
- **Backup automatico sull'host** (cron installato il 24/08/2026,
  `/etc/cron.d/virtualis-backup`): **giornaliero** (DB consistente + `.env` +
  certificati, **rotazione 7**) e **settimanale** (anche uploads + Qdrant,
  **rotazione 2**) in una directory di backup dedicata sull'host; log in
  `/var/log/virtualis-backup.log`.

### 6.2 Watchdog GPU e protezione dagli aggiornamenti

Dall'incidente del 22-24/08/2026 (un aggiornamento automatico del kernel ha
fatto perdere la GPU ai container → chat in CPU a ~3 tok/s, percepita come
«non risponde»):

- **watchdog GPU** (`/etc/cron.d/virtualis-gpu-watch`): ogni 5 minuti verifica
  `nvidia-smi` nel container ollama e lo riavvia se la GPU è persa (bug NVML
  post-update); log in `/var/log/virtualis-gpu-watch.log`;
- **blacklist apt** (`/etc/apt/apt.conf.d/51virtualis-blacklist`): kernel e
  driver NVIDIA esclusi da unattended-upgrades; gli update kernel/driver
  vanno fatti a mano in finestra concordata + reboot.

### 6.3 Limiti di memoria dei container come difesa da OOM

L'host ha 30 GB di RAM; i `mem_limit` del compose evitano che un processo
fuori controllo mandi l'intero host in OOM (che ucciderebbe anche SSH e gli
altri servizi):

- `app`: **20g** (alzato il 27/08 dopo che a 16g i malloc nativi fallivano
  durante l'ingest — `std::bad_alloc` — e la coda moriva in silenzio);
- `ollama`: **10g** (uso reale ~8 GB col modello caricato).

Entrambi con `restart: unless-stopped` e healthcheck (ollama) per il recupero
automatico.

### 6.4 Isolamento della conversione in sottoprocesso

La conversione documenti (Docling) gira in un **processo figlio persistente
con limite di memoria proprio** (`CONVERT_IN_SUBPROCESS=true`,
`CONVERT_MAX_MEMORY_MB=24576` nel compose — limite sul VAS via RLIMIT_AS, non
sull'RSS; il tetto RSS resta il `mem_limit` del container). Un documento
patologico (planimetria raster, PDF enorme) fallisce da solo con stato
`failed` («limite di memoria superato») invece di far uccidere l'intera app
dall'OOM killer (3 incidenti di questo tipo il 24/08). Implementazione:
`backend/app/ingestion/convert_worker.py`. Il worker è inoltre **riciclato
ogni 8 conversioni** (`CONVERT_WORKER_MAX_TASKS=8`) perché torch/Docling non
rilasciano RSS. A completamento, il **watchdog della pipeline**
(`pipeline._watchdog_loop`, 27/08) ogni 60 s rianima il worker di ingest se
morto, sblocca lavorazioni oltre 2× il timeout e riaccoda i `pending` orfani.

### 6.5 Resilienza del database

SQLite in **WAL mode** con `synchronous=NORMAL` (`backend/app/db.py:24-36`):
letture e scritture concorrenti senza blocchi; i file `-wal`/`-shm` sono
normali e gestiti correttamente dal backup (§6.1).

---

## 7. Audit e tracciabilità

### 7.1 Tabella `audit_logs`

Modello: `backend/app/models.py:189-203`. Una riga per **ogni risposta
completa generata** (scrittura in `routes_chat.py:_persist_result`,
righe 113-140):

- `user_id`, `session_id` — chi ha chiesto e in quale conversazione;
- `question` — il quesito integrale;
- `path` — percorso dell'agente (`fast` | `agentic`);
- `steps_json` — i passi eseguiti dal grafo agentico;
- `retrieved_json` — gli id dei chunk recuperati (le fonti usate);
- `answer` — la risposta integrale;
- `warnings_json` — avvisi (es. astensione anti-allucinazione);
- `latency_ms`, `created_at`.

Le risposte **interrotte a metà stream** non generano riga di audit (l'agente
non ha prodotto un esito completo) ma restano nello storico col suffisso
«(risposta interrotta)» (`routes_chat.py:94-110`).

Consultazione: `GET /api/admin/audit` (solo admin, paginata, più recenti
prima, `routes_admin.py:157-199`). Scopi: trasparenza verso il cliente,
debug, ricostruzione di chi ha avuto accesso a quali informazioni.

Nota privacy: l'audit log contiene domande e risposte in chiaro — è voluto
(tracciabilità), ma va considerato dato sensibile a tutti gli effetti: è
incluso nei backup ed eliminabile solo con il reset del materiale con
`include_chat: true` (`routes_admin.py:354-364`).

### 7.2 Feedback utenti

`POST /api/feedback` raccoglie pollice su/giù + commento sulle risposte
(`routes_feedback.py`): un solo feedback per messaggio (upsert), `question` e
`answer_excerpt` (primi 500 caratteri) denormalizzati per leggibilità anche a
chat eliminata. Viste admin: `/api/admin/feedback` e `/stats`. I feedback
negativi alimentano la batteria di valutazione (`scripts/evaluate.py
--from-feedback`). Non è un log di sicurezza, ma contribuisce alla
tracciabilità della qualità.

### 7.3 Log applicativi

Logging strutturato (`structlog`) su stdout del container: eventi rilevanti
per la sicurezza includono i login limitati per troppi fallimenti (con chiave
IP|username), i bootstrap admin, le eliminazioni di documenti via API (con
`user_id`, `routes_docs.py:200-205`) e gli eventi dei job di import.
Consultabili con `docker logs virtualis-app-1` sull'host.

---

## 8. Privacy e compliance

- **Dati tutti on-premise**: documenti, indici, chat, audit e credenziali
  vivono sul server del cliente (o nella workstation di sviluppo). Nessun
  servizio cloud terzo tratta i contenuti, fatta eccezione per le sorgenti
  stesse (Microsoft 365, Google Drive) dalle quali i dati vengono
  **scaricati** in sola lettura.
- **Minimizzazione delle uscite**: l'unico LLM è locale (§1.3); i connettori
  usano scope di sola lettura (`drive.readonly`; per M365 i permessi
  applicativi `Files.Read.All`, `Sites.Read.All`, `Mail.Read`).
- **Cancellazione del singolo documento**: `DELETE /api/documents/{id}` (solo
  admin) rimuove punti Qdrant, chunk (delete esplicita, il cascade ORM non è
  usabile in async), voci del knowledge graph (mention, relation ed entità
  orfane) e la riga Document; il file su disco è eliminato **solo** per la
  sorgente upload e solo se dentro `upload_dir` — mai gli originali
  `local_fs` degli utenti (`routes_docs.py:168-206`).
- **Cancellazioni propagate dalle sorgenti**: i tombstone dei connettori
  (Drive Changes API, scan completo SMB) producono la stessa rimozione
  completa (indice + KG + righe DB, `connectors/gdrive.py:498-511`).
- **Reset totale del materiale**: `POST /api/admin/material/reset`
  `{"confirm": true}` elimina documenti, chunk, indice vettoriale, knowledge
  graph, file su disco e sorgenti; con `include_chat: true` anche sessioni,
  messaggi, feedback e audit log (`routes_admin.py:307-378`). Operazione a
  batch (commit ogni 200 documenti) per non tenere il DB bloccato; rifiutata
  (409) a indicizzazione in corso.
- **Chat private per utente** (§2.4): nessun utente vede le conversazioni
  altrui; gli admin vedono l'audit ma non esiste una UI di lettura delle chat
  altrui.
- **Guardia anti-svuotamento** sui connettori: se uno scan completo non trova
  nulla ma il DB ha documenti (probabile revoca di condivisioni/credenziali),
  la sync si ferma invece di cancellare l'intero indice della sorgente
  (`connectors/gdrive.py:400-406`).
- Retention: nessuna scadenza automatica su chat/audit (checklist §9).

---

## 9. Checklist di hardening residua

In ordine di priorità suggerita:

1. **Esposizione Internet**: verificare/col ridurre il port forwarding sul
   FortiGate (§4.3); nel frattempo monitorare i log per i 429/401 ripetuti.
2. **Prenotazione DHCP** del server sul FortiGate (IP stabile per SAN e
   utenti).
3. **Cifratura di `sources.config_json`** (credenziali sorgenti in chiaro nel
   DB, §5.2): al minimo cifratura simmetrica con chiave in `.env`, oppure
   spostamento delle credenziali fuori dal DB.
4. **`Strict-Transport-Security`** una volta confermato che nessun client usa
   HTTP; valutare redirect 80 → 443 se si apre anche la porta 80.
5. **jail fail2ban per il backend** (ban IP su 429 ripetuti dal throttle
   login), in aggiunta al jail sshd esistente.
6. **Complessità password** oltre agli 8 caratteri minimi (lunghezza ≥ 12 o
   passphrase; oggi solo `min_length=8` in setup e admin API).
7. **Logout/revoca token**: blacklist dei JWT o claim `jti` + tabella di
   revoca; oggi la revoca passa solo da disattivazione utente/rotazione
   segreto (§2.2).
8. **Throttle login distribuito**: il contatore è in memoria di processo
   (§2.6) — adeguato al nodo singolo, da rivedere se mai si scalerà.
9. **Rotazione documentata dei segreti** (JWT, service account, cert TLS a
   scadenza 825 giorni) con promemoria operativi.
10. **Retention di audit log e chat**: policy di scadenza/purge periodico
    (oggi conservati indefinitamente, §7.1/§8).
11. **Hardening SSH residuo**: valutare il ritorno a login solo-chiave una
    volta stabilizzata la manutenzione (oggi password attiva, mitigata da
    fail2ban).
12. **Aggiornamenti di sicurezza delle dipendenze**: processo periodico di
    upgrade delle immagini Docker (`ollama/ollama:latest`, base
    `python:3.12-slim`) e delle dipendenze Python/npm, oggi manuale.
13. **Backlog noto**: persistenza dello stato dei job M365 (senza
    credenziali), diagnosi del worker asyncio che muore in silenzio invece di
    marcare `failed`, profilazione della crescita RSS del processo principale.

---

## Appendice — Riferimenti rapidi

- Autenticazione/JWT/throttle: `backend/app/auth.py`
- Configurazione e segreto JWT: `backend/app/config.py`
- Wizard primo avvio: `backend/app/api/routes_setup.py`
- App, CORS, security headers: `backend/app/api/main.py`
- Route admin: `backend/app/api/routes_admin.py`
- Import M365 (credenziali in memoria): `backend/app/m365_import.py`,
  `backend/app/api/routes_m365.py`
- Connettori: `backend/app/connectors/` (`base.py`, `gdrive.py`, `m365.py`,
  `local_fs.py`, `upload.py`)
- Modelli (users, audit_logs, sources): `backend/app/models.py`
- TLS: `backend/scripts/gen_cert.py`, `backend/app/__main__.py`
- Backup: `backend/scripts/backup.py` (host: `/etc/cron.d/virtualis-backup`)
- Compose (porte, mem_limit, volumi): `docker-compose.yml`
- Esclusioni git: `.gitignore`
