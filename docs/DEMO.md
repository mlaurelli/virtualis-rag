# Guida alla sessione di consegna (demo)

Questa guida prepara e conduce la dimostrazione al cliente del sistema RAG
agentico Virtualis, con un corpus documentale italiano realistico generato
automaticamente.

## Prerequisiti

- Setup completato una tantum: `make setup` (oppure i passi manuali del
  README) e file `.env` con `MOONSHOT_API_KEY` valorizzata — la chat richiede
  il LLM di generazione.
- Al primo ingest vengono scaricati i modelli di retrieval (~2 GB, una
  tantum; gli avvii successivi usano la cache locale). Farlo **prima** della
  sessione con il cliente.

## Preparazione: corpus dimostrativo

```bash
# Dalla radice del repository
make demo-seed              # genera i documenti in backend/demo_corpus/
make demo-seed INGEST=1     # genera E indicizza su backend/data-demo/
```

equivalente a:

```bash
cd backend
uv run python scripts/demo_seed.py --ingest
```

Lo script crea 10 documenti coerenti tra loro (contratti di manutenzione
2023 e 2025 con clausole diverse, verbale CDA con delibera n. 2024-017,
listino e piano manutenzione 2025, nota operativa PDF, manuale sicurezza,
policy rimborsi, bilancio di sostenibilità, circolare) e li indicizza con la
pipeline reale su un `DATA_DIR` dedicato (`backend/data-demo/`), senza
toccare eventuali dati di lavoro in `data/`.

## Avvio della demo

```bash
# Terminale 1 — backend sul DATA_DIR demo (porta 8000)
cd backend && DATA_DIR=./data-demo uv run uvicorn app.api.main:app --port 8000

# Terminale 2 — frontend (porta 5173)
make dev-frontend
```

Aprire `http://localhost:5173` e accedere con `admin` / `admin` (o l'utente
creato in precedenza sul DATA_DIR demo).

## Percorso consigliato della demo

1. **Stato dell'archivio** — pagina Documenti: mostrare i 10 documenti
   indicizzati, i metadati (tipo, dimensione, data) e lo stato `indexed`.
2. **Ricerca diretta** — pagina Ricerca: una query lessicale/semantica con
   filtri (es. fonte = demo-corpus, tipo = pdf) per mostrare che il sistema
   non è solo chat.
3. **Domande fast** (percorso rapido, una ricerca puntuale):
   - *Qual è il canone annuo del contratto di manutenzione del 2025?*
   - *Quanto costa il sensore di pressione SP-220 nel listino 2025?*
4. **Domande agentiche** (ciclo plan → retrieve ⇄ evaluate → synthesize:
   sottolineare gli **step dell'agente** che compaiono in tempo reale):
   - *Confronta le clausole di garanzia e le penali del contratto di
     manutenzione 2023 con quelle del 2025: che cosa è cambiato?*
   - *Quale delibera del CDA ha approvato il budget di manutenzione 2025 e
     come si collega al contratto di manutenzione attualmente vigente?*
   - *Per sostituire i filtri della linea B: quali articoli del listino
     servono, quanto costano in totale e quale procedura devo seguire?*

## Cosa mostrare durante le risposte

- **Passi dell'agente**: classificazione (fast vs agentic), sotto-quesiti
  pianificati, ricerche eseguite, valutazione di copertura e riformulazioni.
- **Citazioni `[n]`**: ogni affermazione è agganciata a un passaggio
  specifico; il pannello citazioni mostra documento, pagina e sezione.
- **Apertura della fonte**: cliccare una citazione per aprire il documento
  originale — per il PDF (Nota Operativa) l'apertura avviene alla pagina
  citata.
- **Tracciabilità**: pagina Admin → registro di audit: ogni risposta è
  registrata con quesito, passaggi utilizzati e utente.
- **Multiutente** (se utile): creare un utente non admin e mostrare che le
  sessioni di chat sono private per utente.

## Ripristino

Per ripartire da zero: fermare il backend ed eliminare `backend/data-demo/`
(e rigenerare con `make demo-seed INGEST=1`). Il corpus in
`backend/demo_corpus/` può essere rigenerato in qualsiasi momento con
`make demo-seed`.
