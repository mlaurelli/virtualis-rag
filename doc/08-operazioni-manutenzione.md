# 08 — Operazioni e manutenzione

Manuale operativo per chi amministra Virtualis in produzione: installazione,
deploy, operazioni ricorrenti, manutenzione automatica, monitoraggio e
risoluzione dei problemi. I comandi sono stati verificati sui file del
repository (`Makefile`, `docker-compose.yml`, `backend/Dockerfile`,
`backend/scripts/`); host, utenti e segreti sono indicati con segnaposto
(`<host>`, `<utente>`, `<password>`, `<share-smb>`, ...): i valori reali sono
nel documento operativo riservato del team (non versionato).

Convenzioni usate in tutto il documento:

- `<host>`: indirizzo o hostname del server di produzione;
- `<utente>`: utente SSH amministrativo sul server;
- `<password>`: password amministrativa dell'applicazione web (admin);
- `<repo-server>`: percorso del repository sul server (es. `/home/<utente>/virtualis`);
- nomi dei container: `virtualis-app-1` e `virtualis-ollama-1` (default compose).

---

## 1. Ambiente di produzione

### 1.1 Topologia

Macchina singola on-premise (Ubuntu 24.04 LTS), nessuna ridondanza. Lo stack
è orchestrato da Docker Compose (`docker-compose.yml` in radice del
repository) con due servizi:

| Servizio | Immagine / build | Funzione | Porte host |
|---|---|---|---|
| `app` | build multi-stage da `backend/Dockerfile` (stage Node 20 per il frontend, stage `python:3.12-slim` con uv per il backend) | Backend FastAPI + frontend React servito come SPA; Qdrant **embedded** in `/data/qdrant`; SQLite in `/data` | `8000:8000` (accesso locale/tunnel), `8080:8000` (accesso LAN/HTTPS) |
| `ollama` | `ollama/ollama:latest` | LLM locale, endpoint OpenAI-compatible su `http://ollama:11434/v1` | **nessuna porta pubblicata** (solo rete interna compose) |

Dipendenze e avvio: `ollama` espone un healthcheck (TCP su 11434) e `app`
parte solo a Ollama sano (`depends_on: condition: service_healthy`); entrambi
hanno `restart: unless-stopped`. Il container `app` ha un proprio
`HEALTHCHECK` su `/api/health` (interval 30s, start-period 60s).

Persistenza:

- `./data` (bind mount su `/data`): SQLite (`virtualis.db`, in WAL — i file
  `virtualis.db-wal`/`-shm` sono normali), upload, certificati TLS in
  `data/certs/`, Qdrant embedded, stato del reindex (`reindex_state.json`);
- volume `hf-models`: cache dei modelli di retrieval HuggingFace (~2-4 GB);
- volume `ollama-models`: modelli Ollama scaricati (`/root/.ollama`);
- bind mount in sola lettura della cartella di rete del cliente
  (`/mnt/ia-locale` sull'host → `/data/mounts/ia-locale` nel container, con
  `bind.propagation: rslave` per vedere i remount dell'automount systemd).

Limiti di memoria (host da ~30 GB RAM, verificati sul campo dopo gli
incidenti OOM di agosto):

- `app`: `mem_limit: 20g` — il processo principale parte da ~12 GB di RSS
  (modelli di retrieval + Qdrant embedded) e gonfia durante l'ingest;
- `ollama`: `mem_limit: 10g` — uso reale ~8 GB col modello caricato.

### 1.2 Prerequisiti hardware e driver

- GPU NVIDIA (in produzione: RTX 5080 16 GB VRAM) con **driver proprietari**
  e **`nvidia-container-toolkit`** installati sull'host: il compose prenota
  automaticamente la GPU (`deploy.resources.reservations.devices`, driver
  `nvidia`, `capabilities: [gpu]`).
- Modello attivo `qwen2.5:14b-instruct` (~9 GB): sta interamente nei 16 GB di
  VRAM (100% GPU, ~80-100 token/s). Configurazione Ollama nel compose:
  `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_NUM_PARALLEL=1` (contesto ampio per
  i prompt RAG; col default i prompt venivano troncati e il modello rispondeva
  fuori tema).
- Senza GPU l'inferenza ricade sulla CPU (molto più lenta, ~3 token/s):
  aumentare `LLM_REQUEST_TIMEOUT` (default nel compose: 600 s).
- Disco: qualche GB per le immagini e i volumi, più lo spazio del corpus in
  `data/`; i modelli di retrieval occupano ~2-4 GB nella cache HF.

---

## 2. Installazione da zero

Procedura verificata sul server di produzione.

```bash
# 1. Prerequisiti sull'host: docker + compose plugin, driver NVIDIA,
#    nvidia-container-toolkit (verificare con: docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi)

# 2. Codice
git clone <url-repo> <repo-server> && cd <repo-server>

# 3. Configurazione
cp .env.example .env
#    → impostare JWT_SECRET (obbligatorio in produzione);
#      MOONSHOT_API_KEY è già "ollama" nel compose, KG_ENABLED=true se si
#      vuole il knowledge graph.

# 4. Certificato TLS self-signed (SAN: hostname, localhost, IP LAN + extra)
cd backend && uv run python scripts/gen_cert.py <ip-lan> <hostname-lan>
#    → scrive data/certs/virtualis.crt e virtualis.key (permessi 600).
#      Le variabili TLS_CERT_FILE/TLS_KEY_FILE sono già valorizzate nel
#      compose (puntano a /data/certs/...): nient'altro da fare.
cd ..

# 5. Build e avvio dello stack (~10 minuti al primo build)
docker compose up -d --build

# 6. Modello di generazione (una tantum, ~9 GB; la banda del cliente può
#    rendere il pull lungo: usare nohup perché le sessioni SSH che cadono
#    uccidono il download)
docker compose exec ollama ollama pull qwen2.5:14b-instruct

# 7. Verifica
docker compose ps                      # entrambi i container "healthy"
curl -k https://localhost:8000/api/health

# 8. Primo utente admin: aprire https://<host>:8080 nel browser (accettare
#    l'eccezione per il cert self-signed): se non esiste alcun utente viene
#    mostrato automaticamente il wizard /setup (username + password ≥ 8
#    caratteri, login automatico). In alternativa al wizard si possono
#    impostare ADMIN_USERNAME e ADMIN_PASSWORD in .env prima del primo avvio.
```

Note:

- Al primo avvio vengono scaricati i modelli di retrieval (embedding denso +
  sparso + reranker, ~2 GB) nella cache del volume `hf-models`; il
  preriscaldamento (`WARMUP_ON_START=true`, default) li carica in background.
- Se serve la cartella di rete del cliente: montare la share SMB sull'host in
  `/mnt/ia-locale` (fstab con automount, credenziali in un file con permessi
  ristretti, montaggio read-only) **prima** di `compose up`, altrimenti il
  bind mount fallisce.

---

## 3. Deploy degli aggiornamenti

### 3.1 Procedura standard

Il codice in produzione coincide col branch `main`. Deploy dalla workstation
di sviluppo:

```bash
# dal repository locale, dopo aver verificato test e lint in locale
rsync -az --delete --exclude '.git' --exclude 'data/' --exclude '.env' \
  --exclude 'AGENTS.md' --exclude 'node_modules' --exclude 'dist' \
  --exclude '.venv' --exclude '__pycache__' --exclude 'backups' \
  ./ <utente>@<host>:<repo-server>/

ssh <utente>@<host> 'cd <repo-server> && docker compose up -d --build'   # ~10 min
```

Verifica post-deploy:

```bash
ssh <utente>@<host> 'docker compose -f <repo-server>/docker-compose.yml ps'
ssh <utente>@<host> 'docker logs --tail 50 virtualis-app-1'
curl -k https://<host>:8080/api/health        # 200 atteso
# prova funzionale: login + una domanda in chat dal browser
```

### 3.2 Cosa NON sovrascrivere mai

- **`.env` del server**: contiene `JWT_SECRET` e le configurazioni locali.
  L'rsync lo esclude già (`--exclude '.env'`); non forzarlo mai a mano.
- **`data/` del server**: è tutto lo stato applicativo (SQLite, upload,
  Qdrant embedded, certificati). Esclusa dall'rsync; non cancellarla né
  sostituirla con copie locali.
- **`AGENTS.md`**: documento operativo riservato, escluso da git e dal deploy.

### 3.3 Perché il vecchio deploy via `docker cp` è deprecato

Copiare i file dentro il container in esecuzione (`docker cp ... virtualis-app-1:/app`)
modifica solo il filesystem del container vivo: al primo `docker compose up -d`
con recreate (o a un crash con `restart`) il container viene ricreato
dall'immagine e **tutte le modifiche copiate a mano spariscono**. Il deploy
corretto aggiorna il sorgente sull'host e ricostruisce l'immagine
(`--build`): l'immagine è l'unica fonte di verità. Effetto collaterale noto:
il recreate cancella anche gli script copiati a mano in `/app/scripts` (es.
`kg_build.py`, vedi §4.5): vanno ricopiati dopo ogni recreate.

---

## 4. Operazioni ricorrenti

### 4.1 Log e riavvio

```bash
ssh <utente>@<host> 'docker logs -f virtualis-app-1'            # log backend/agente (live)
ssh <utente>@<host> 'docker logs --tail 50 virtualis-ollama-1'  # log LLM (velocità tok/s)
ssh <utente>@<host> 'docker restart virtualis-app-1'            # riavvio rapido app
ssh <utente>@<host> 'cd <repo-server> && docker compose up -d'  # applica modifiche a .env/compose
```

### 4.2 Cambio modello LLM

```bash
# 1. Scaricare il modello (download lunghi: nohup, le sessioni SSH cadono)
ssh <utente>@<host> 'nohup docker exec virtualis-ollama-1 ollama pull <modello> > /tmp/pull.log 2>&1 &'

# 2. Aggiornare KIMI_MODEL in <repo-server>/docker-compose.yml (servizio app)

# 3. Applicare
ssh <utente>@<host> 'cd <repo-server> && docker compose up -d'

# 4. Verifica: modello caricato e % GPU
ssh <utente>@<host> 'docker exec virtualis-ollama-1 ollama ps'
```

Attenzione ai modelli "reasoning" (qwen3 ibrido, gpt-oss): il thinking non è
disattivabile via endpoint OpenAI-compatible e le latenze salgono molto; per
questo la produzione usa `qwen2.5:14b-instruct`.

### 4.3 Reindex completo (reindicizzazione vettoriale)

Job in-app riprendibile (stato in `/data/reindex_state.json`; cede il passo
all'ingest; sopravvive ai restart dello script ma **non** a un recreate del
container senza stato — lo stato su file permette comunque la ripresa).
Lo script CLI `backend/scripts/reindex.py` pilota l'app via HTTP(S) (il client
Qdrant embedded non tollera un secondo processo sullo stesso path):

```bash
# dentro il container o comunque con accesso HTTPS all'app
python scripts/reindex.py --url https://localhost:8000 \
  --username admin --password '<password>' [--dry-run] [--restart]
# polling su GET /api/admin/reindex/status fino a stato terminale
```

Equivalente via API: `POST /api/admin/reindex` con `{"confirm": true}` (admin)
e polling su `/api/admin/reindex/status`. Su corpus grandi (150k chunk) la
durata è dell'ordine di 1-2 giorni, progressiva.

### 4.4 Requeue dei documenti failed

I documenti in stato `failed` non vengono più riaccodati automaticamente a
ogni riavvio: si rilavorano con `POST /api/ingest/requeue` (admin). Per i
formati Office legacy (`.doc`/`.xls`) che Docling non converte esiste lo
script one-shot `backend/scripts/m365_recover_failed.py` (converte in testo
con catdoc/xls2csv e ricarica via API; idempotente):

```bash
docker cp backend/scripts/m365_recover_failed.py virtualis-app-1:/tmp/ && \
docker exec virtualis-app-1 python3 /tmp/m365_recover_failed.py
```

### 4.5 Rigenerazione del knowledge graph

Con `KG_ENABLED=true` l'estrazione avviene automaticamente a ogni nuovo
ingest. Per (ri)costruire il grafo sul corpus già indicizzato (una chiamata
LLM per documento):

```bash
# in locale/sviluppo, dalla cartella backend/
uv run python scripts/kg_build.py                  # tutti i documenti indexed
uv run python scripts/kg_build.py --document-id 7  # un solo documento

# in produzione (lo script non è nell'immagine: va copiato dopo ogni recreate)
ssh <utente>@<host> 'docker exec virtualis-app-1 mkdir -p /app/scripts && \
  docker cp /tmp/kg_build.py virtualis-app-1:/app/scripts/kg_build.py && \
  docker exec -d virtualis-app-1 sh -c "cd /app && python scripts/kg_build.py > /data/kg_build.log 2>&1"'
```

Non lanciare l'estrazione durante una demo: Ollama serve le richieste in
serie e un job lungo blocca la chat (c'è un semaforo applicativo
`LLM_MAX_CONCURRENCY`, default 2, che evita solo il pile-up).

### 4.6 Sync delle sorgenti

- **Cartella di rete SMB** (sorgente `local_fs` sulla share montata): pulsante
  "Aggiorna da cartella di rete" nella pagina Documenti, oppure
  `POST /api/ingest/sync`. Se la share non risponde, controllare il mount
  sull'host (`mount | grep ia-locale`) e l'automount systemd.
- **Google Drive**: sync incrementale via Changes API; in modalità delta la
  sync **ritenta sempre anche i documenti `failed`** della sorgente (campo
  `retried_failed` nel report). Config sorgente:
  `{"credentials_json": ..., "folder_id"?/"shared_drive_id"?}`.
- **Microsoft 365**: pagina `/m365` (solo admin) per scan/import; route
  `/api/m365/test|start|status|cancel`. Graph throttla pesantemente (429 con
  attese di 60-90 s): gli scan completi durano ore, è atteso.

### 4.7 Reset completo del materiale

`POST /api/admin/material/reset` con body `{"confirm": true}` (admin):
cancella documenti, chunk, knowledge graph, indice vettoriale, file caricati
e sorgenti. Operazione distruttiva e irreversibile: fare prima un backup
(§5.1) e usarla solo per ripartire da zero con un nuovo import.

---

## 5. Manutenzione automatica sul server

Installata sull'host (non nel repository): verificare con
`ls /etc/cron.d/` e i log citati.

### 5.1 Backup (cron `virtualis-backup`)

- **Giornaliero**: database SQLite copiato con l'API di backup di sqlite3
  (consistente anche ad app in esecuzione — il DB è in WAL), più `.env` e
  certificati TLS; **rotazione 7**.
- **Settimanale**: come il giornaliero più `uploads/` e l'indice Qdrant
  embedded; **rotazione 2**.
- Destinazione: `<repo-server>-backups/` (directory dedicata sull'host);
  log: `/var/log/virtualis-backup.log`.

Ripristino:

```bash
# ripristino manuale da archivio (non cancella nulla: la data_dir esistente
# viene spostata in data.pre-restore-<timestamp> prima dell'estrazione)
cd backend && uv run python scripts/backup.py restore backups/<archivio>.tar.gz --yes
```

Lo stesso formato di archivio è prodotto da `make backup`
(`scripts/backup.py backup`, con manifest JSON: data, versione, conteggi
documenti/chunk/utenti, modalità Qdrant).

### 5.2 Watchdog GPU (cron `virtualis-gpu-watch`)

Ogni 5 minuti esegue `nvidia-smi` **dentro il container ollama** e, se la GPU
non risponde, riavvia il container. Log: `/var/log/virtualis-gpu-watch.log`.

**Perché esiste**: un aggiornamento automatico del kernel ha rotto la
comunicazione driver/container (NVML "Unknown Error") e l'inferenza è ricaduta
in CPU a ~3 token/s, percepita dagli utenti come "il sistema non risponde".
Il watchdog ripristina la GPU senza intervento manuale; se il problema persiste
serve un reboot dell'host.

### 5.3 Blacklist apt per kernel e driver NVIDIA

`/etc/apt/apt.conf.d/51virtualis-blacklist` esclude kernel e driver NVIDIA
dagli unattended-upgrades (causa dell'incidente GPU di cui sopra). Gli update
di kernel/driver vanno fatti **a mano, in finestra concordata, con reboot
pianificato** e verifica post-boot (`nvidia-smi`, `ollama ps`, prova chat).

### 5.4 fail2ban

Jail `sshd` in `/etc/fail2ban/jail.d/virtualis-sshd.conf`: 5 tentativi
falliti in 10 minuti → ban di 1 ora. Utile sia per SSH sia come argine agli
scanner che periodicamente raggiungono l'host. L'applicazione web ha un
proprio anti-bruteforce sul login (429 dopo 5 fallimenti in 300 s per coppia
IP+username), ma la superficie esposta va comunque verificata con il cliente
(port forwarding sul firewall perimetrale: solo le porte concordate).

---

## 6. Monitoraggio e salute

### 6.1 Endpoint e punti di controllo

- `GET /api/health` — liveness semplice (usato dall'healthcheck Docker);
- `GET /api/health/deep` — healthcheck approfondito (auth admin): DB, Qdrant,
  LLM configurato, embedder; risponde 200/503;
- `docker compose ps` — stato healthcheck dei container;
- `docker stats virtualis-app-1 virtualis-ollama-1` — RSS vs `mem_limit`;
- `docker exec virtualis-ollama-1 ollama ps` — modello caricato e % GPU/CPU;
- `docker logs virtualis-app-1` — pipeline di ingest, watchdog interno, errori.

### 6.2 Segnali di allarme e azioni

**Coda di ingest ferma** (documenti `pending` che non avanzano).
Incidente reale 26-27/08: worker asyncio morto in silenzio dopo fallimenti di
allocazione nativi, coda congelata due volte. Da allora esiste un watchdog
interno (`pipeline._watchdog_loop`, ogni 60 s): rianima il worker morto,
sblocca lavorazioni oltre 2× il timeout e riaccoda i `pending` orfani; i
pending il cui file non esiste più vengono marcati `failed`. Azione:
controllare i log per righe del watchdog; se la coda resta ferma, riavviare
l'app (`docker restart virtualis-app-1`) e poi requeue dei `failed`.

**Memoria del container app vicino al limite (20g)**.
Il processo principale parte da ~12 GB di RSS e cresce durante l'ingest
(allocatori torch/onnx che non rilasciano). Mitigazioni già attive: worker di
conversione Docling in sottoprocesso con `RLIMIT_AS` proprio
(`CONVERT_MAX_MEMORY_MB=24576` — il VAS coi modelli di layout/OCR supera i
16 GB anche su file piccoli) e riciclo del worker ogni 8 documenti
(`CONVERT_WORKER_MAX_TASKS=8`). Se `docker stats` mostra RSS stabilmente
>17-18 GB: riavviare l'app in una pausa; il documento patologico fallirà da
solo ("limite di memoria superato") senza buttare giù il container.

**GPU persa** (`ollama ps` mostra 100% CPU, o NVML "Unknown Error" nei log).
Vedi §5.2: il cron watchdog dovrebbe riavviare ollama entro 5 minuti. Se non
basta → reboot dell'host; se è appena avvenuto un update kernel/driver
automatico, verificare la blacklist apt (§5.3).

**Chat lenta** (risposte a ~3 token/s).
Quasi sempre inferenza in CPU: controllare `ollama ps` (deve dire 100% GPU)
e i log ollama. Se invece la GPU c'è, verificare che non sia in corso un job
lungo (kg_build, import M365): Ollama serve le richieste in serie.

**Throttling Microsoft Graph (429)** durante scan/import M365.
Atteso: Graph risponde 429 con attese di 60-90 s; gli scan durano ore. Non è
un errore: non riavviare il job, lasciarlo lavorare. Nota: i job M365 vivono
in memoria — un riavvio dell'app li uccide e l'import va rilanciato (§9).

**Linea di rete del cliente instabile**.
Le sessioni SSH cadono e uccidono i processi foreground: qualunque download
lungo (`ollama pull`, rsync corposi) va lanciato con `nohup ... &` e seguito
via log su file.

---

## 7. Troubleshooting

| Sintomo | Causa probabile | Rimedio |
|---|---|---|
| Chat a ~3 token/s, percepita come "non risponde" | GPU persa dal container ollama dopo update kernel automatico (NVML "Unknown Error") | Attendere il watchdog GPU (≤5 min) o `docker restart virtualis-ollama-1`; se persiste, reboot host; verificare blacklist apt |
| `ollama ps` mostra 100% CPU | Modello troppo grande per la VRAM o GPU non prenotata | Tornare a `qwen2.5:14b-instruct`; verificare `nvidia-container-toolkit` e la sezione `deploy.resources` del compose |
| Coda ingest congelata con decine di `pending` | Worker asyncio morto in silenzio (wedge nativo dopo OOM torch) | Il watchdog interno riparte da solo in ~60 s; altrimenti `docker restart virtualis-app-1` + `POST /api/ingest/requeue` |
| Documento fallisce con "limite di memoria superato" | Conversione Docling oltre il tetto VAS del sottoprocesso (`CONVERT_MAX_MEMORY_MB`) | Atteso su documenti patologici: il resto della coda prosegue; convertire il file a mano (es. in PDF testuale) e ricaricarlo |
| Container app killato (OOM) durante l'ingest | RSS oltre `mem_limit` (20g) | Mitigato da worker in sottoprocesso + riciclo ogni 8 task; se ricorre, riavviare in pausa ingest e valutare ingest a lotti più piccoli |
| Risposte LLM fuori tema / generiche | Contesto Ollama troppo corto che tronca i prompt RAG | Verificare `OLLAMA_CONTEXT_LENGTH=16384` e `OLLAMA_NUM_PARALLEL=1` nel compose |
| Scan/import M365 lentissimo, molti 429 nei log | Throttling di Microsoft Graph (Retry-After 60-90 s) | Normale: lasciare correre; non riavviare l'app (il job è in memoria e morirebbe) |
| Import M365 sparito dopo un riavvio | Job in memoria, non persistito | Rilanciare l'import dalla pagina `/m365`; i file già importati sono deduplicati via content hash |
| `docker compose up` fallisce sul bind `/mnt/ia-locale` | Share SMB non montata sull'host | Ripristinare il mount (automount systemd / `mount -a`), poi rilanciare compose |
| Browser: "certificato non attendibile" su HTTPS LAN | Cert self-signed | Accettare l'eccezione, o distribuire il cert come CA fidata; rigenerare con `gen_cert.py` se cambiano IP/hostname |
| Login web risponde 429 | Anti-bruteforce applicativo (5 fallimenti in 300 s) | Attendere `Retry-After`; verificare che non sia uno scanner esterno (fail2ban lato SSH) |
| Pull Ollama interrotto a metà | Caduta sessione SSH (rete instabile) | Rilanciare con `nohup ... > /tmp/pull.log 2>&1 &` |
| Script copiati in `/app/scripts` spariti | Recreate del container (immagine = unica fonte di verità) | Ricopiare con `docker cp` dopo ogni recreate (vedi §3.3 e §4.5) |
| Citazioni senza evidenziazione nei PDF vecchi | Documenti indicizzati prima dei bounding box Docling | Re-ingest del documento (sync o nuovo upload): la colonna `bboxes_json` è aggiunta all'avvio sui DB esistenti |

---

## 8. Sviluppo locale

### 8.1 Avvio fuori da Docker

```bash
make setup            # venv backend + deps Python (uv) + npm install frontend

# due terminali:
make dev-backend      # uvicorn con reload su http://localhost:8000
make dev-frontend     # Vite su http://localhost:5173 (proxy /api → 8000)
```

Equivalente manuale: `cd backend && uv run uvicorn app.api.main:app --reload --port 8000`
e `cd frontend && npm run dev`. Senza Docker, Qdrant gira embedded in
`DATA_DIR/qdrant`; per la chat serve un LLM OpenAI-compatible (Ollama locale
con `KIMI_BASE_URL=http://localhost:11434/v1` oppure API Kimi con
`MOONSHOT_API_KEY`).

### 8.2 Test e lint

```bash
make test       # backend: uv run pytest -q (LLM mockato, HTTP intercettato con respx)
make lint       # ruff + mypy (backend), oxlint (frontend)
make build      # build frontend con typecheck (tsc + vite)
make test-e2e   # Playwright contro backend reale su DATA_DIR di test (porta 8033)
```

### 8.3 CI GitHub Actions

`.github/workflows/ci.yml`, a ogni push su `main` e su ogni PR, tre job:

- **backend** (bloccante): Python 3.12, uv con cache, `ruff check app tests
  scripts`, `mypy app`, `pytest -q`; cache HuggingFace per i modelli di
  retrieval (~2 GB altrimenti riscaricati a ogni run);
- **frontend** (bloccante): Node 20, `npm ci`, `npm run lint` (oxlint),
  `npm run build` (typecheck incluso);
- **e2e** (informativo, `continue-on-error: true`): suite Playwright con
  ingest reale dei documenti di test; molto più lenta e pesante (primo run
  ~4 GB di modelli), segnala regressioni senza bloccare le PR.

---

## 9. Limiti noti e backlog tecnico

- **Job M365 e reindex in memoria**: un riavvio/recreate del container app
  uccide i job in corso. Il reindex riparte dallo stato su file
  (`/data/reindex_state.json`); l'import M365 **va rilanciato** (la
  deduplicazione via content hash evita doppioni). Backlog: persistenza dello
  stato dei job M365.
- **Crescita RSS del processo principale**: gli allocatori torch/onnx non
  rilasciano memoria durante l'ingest; mitigato con `mem_limit` 20g, worker di
  conversione in sottoprocesso e riciclo ogni 8 task. Backlog: profilare la
  crescita RSS e capire perché in passato il worker asyncio è morto in
  silenzio invece di marcare il documento `failed` (ora coperto dal watchdog
  interno, ma la causa ultima non è riprodotta).
- **Ollama seriale**: una richiesta alla volta; job LLM lunghi (kg_build)
  bloccano la chat. `LLM_MAX_CONCURRENCY` (default 2) evita solo il pile-up
  applicativo.
- **Niente alta affidabilità**: macchina singola, nessuna ridondanza; il
  failover è il ripristino da backup.
- **Nessun permission trimming per-utente**: tutti gli utenti autenticati
  vedono tutto l'archivio indicizzato (le ACL delle fonti non sono propagate).
- **Esposizione di rete da verificare col cliente**: l'app è raggiungibile
  in HTTPS sulle porte pubblicate; il port forwarding del firewall
  perimetrale va controllato (solo le porte concordate aperte), e va fatta la
  prenotazione DHCP dell'host. Lato applicazione: TLS attivo, anti-bruteforce
  sul login, fail2ban su SSH.
- **Modelli reasoning non adatti alla produzione**: thinking non
  disattivabile via endpoint OpenAI-compatible → latenze troppo alte per la
  pipeline agentica.
- **Formati legacy `.doc`/`.xls`**: Docling non li converte; recupero manuale
  con `scripts/m365_recover_failed.py` (§4.4).
