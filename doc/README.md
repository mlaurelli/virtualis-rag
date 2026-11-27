# Documentazione Virtualis

Documentazione completa del sistema, organizzata per livello di approfondimento
e per pubblico. Ogni documento è autonomo e rimanda agli altri dove serve.

> Nota: questa cartella (`doc/`) contiene la documentazione di riferimento
> approfondita. La cartella `docs/` (radice del repo) conserva i documenti
> storici e di progetto (manuale utente, relazioni, stato di avanzamento).

## Per chi è cosa

| Vuoi... | Leggi |
|---|---|
| Capire il sistema senza essere un tecnico | [07 — Come funziona, spiegato semplice](07-come-funziona-spiegato-semplice.md) |
| Avere la visione d'insieme e il perché delle scelte | [01 — Architettura](01-architettura.md) |
| Mettere le mani nel codice backend | [02 — Guida tecnica backend](02-backend-guida-tecnica.md) |
| Capire come i documenti diventano interrogabili | [03 — Pipeline di ingest](03-pipeline-ingest.md) |
| Capire come risponde (e perché non inventa) | [04 — Retrieval, agente e anti-allucinazione](04-retrieval-agente-antillucinazione.md) |
| Lavorare sull'interfaccia web | [05 — Frontend](05-frontend.md) |
| Valutare/gestire la sicurezza | [06 — Sicurezza](06-sicurezza.md) |
| Installare, aggiornare, monitorare, risolvere problemi | [08 — Operazioni e manutenzione](08-operazioni-manutenzione.md) |

## I documenti

### [01 — Architettura](01-architettura.md)
Visione d'insieme del sistema: componenti (FastAPI, Qdrant embedded, SQLite,
Ollama, frontend React, connettori), diagrammi dei flussi (domanda RAG, ingest),
le 13 scelte architetturali chiave con trade-off e incidenti che le hanno
motivate, deployment e non-obiettivi.

### [02 — Guida tecnica backend](02-backend-guida-tecnica.md)
Il codice di `backend/app/` modulo per modulo: responsabilità, funzioni e classi
principali, modello dati (tutte le tabelle), tutti i parametri di configurazione
con env var, mappa completa delle API REST, ciclo di vita dell'app, testing.

### [03 — Pipeline di ingest](03-pipeline-ingest.md)
Deep-dive sull'indicizzazione: ciclo di vita di un documento (stati e
transizioni), conversione Docling con isolamento in sottoprocesso (limiti di
memoria VAS/RSS), chunking, embedding, indicizzazione vettoriale, affidabilità
(coda, resume, watchdog), connettori (SMB, Google Drive, M365, upload), reindex.

### [04 — Retrieval, agente e anti-allucinazione](04-retrieval-agente-antillucinazione.md)
Come il sistema risponde: ricerca ibrida denso+sparso, reranker, soglie di
pertinenza e astensione, flusso agentico (verify/critic/refine), citazioni,
knowledge graph, batterie di valutazione e metriche (hallucination_rate).

### [05 — Frontend](05-frontend.md)
L'interfaccia React: routing, autenticazione, le pagine una per una (chat con
streaming SSE, documenti, grafo, M365, admin), componenti riusabili, theming,
test e2e Playwright, come estendere.

### [06 — Sicurezza](06-sicurezza.md)
Modello di minaccia, autenticazione JWT e anti-bruteforce, TLS, superficie di
esposizione di rete e mitigazioni (fail2ban, port forwarding), gestione dei
segreti, backup e integrità, audit, privacy, checklist di hardening.

### [07 — Come funziona, spiegato semplice](07-come-funziona-spiegato-semplice.md)
Per tutti: cos'è Virtualis, il viaggio di un documento e di una domanda con
l'analogia della biblioteca, perché preferisce dire "non lo so" piuttosto che
inventare, differenze con ChatGPT, FAQ e glossario.

### [08 — Operazioni e manutenzione](08-operazioni-manutenzione.md)
Manuale operativo: installazione da zero, procedura di deploy, operazioni
ricorrenti (cambio modello, reindex, sync sorgenti), manutenzione automatica
(backup, watchdog GPU, fail2ban), monitoraggio, troubleshooting (15 casi),
sviluppo locale e CI.

## Convenzioni della serie

- Tutti i documenti citano i file sorgente (es. `backend/app/ingestion/pipeline.py`)
  a supporto di ogni affermazione: se codice e documento divergono, **il codice
  ha ragione** — segnalare e aggiornare il documento.
- Credenziali, segreti e indirizzi IP reali non compaiono mai: i segnaposto
  (`<host>`, `<password-admin>`, ...) vanno sostituiti con i valori operativi,
  custoditi altrove (canali riservati, non nel repo).
- Aggiornare questi documenti quando si modificano architettura, pipeline,
  soglie, sicurezza o procedure operative.
