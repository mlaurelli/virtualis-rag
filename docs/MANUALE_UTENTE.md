# Manuale utente — Sistema agentico di gestione documentale

Guida per chi usa il sistema da browser. Per l'installazione e l'amministrazione tecnica vedere `README.md`.

## Accesso

Aprire il browser all'indirizzo fornito dall'amministratore (es. `https://<workstation>:8000`).

- **Primo avvio**: il sistema chiede di creare il primo amministratore (nome utente e password di almeno 8 caratteri).
- **Accessi successivi**: nome utente e password personali nella pagina di login.

## La chat (pagina principale)

Scrivere la domanda in linguaggio naturale, in italiano o inglese, e premere Invio.

- **Domande semplici** (es. *«Quanto dura la garanzia nel contratto 2025?»*) ricevono risposta in pochi secondi.
- **Domande complesse** (confronti, più documenti, date: *«Le condizioni di garanzia sono cambiate tra il 2023 e il 2025? Cosa dice il verbale del CDA?»*) attivano il ciclo agentico: sopra la risposta compare la **timeline dei passi** (classificazione, piano, ricerche, valutazione, sintesi, verifica) con la durata di ciascuno. I passi sono collassabili.
- **Follow-up**: si può continuare la conversazione (*«e nel 2024?»*): il sistema capisce il riferimento al contesto.

### Citazioni e verifica

Ogni affermazione della risposta termina con un riferimento numerato `[n]`:

- **Passando il mouse** sul numero si vede l'anteprima del passaggio (documento, fonte, estratto).
- **Cliccando** si apre il pannello citazioni con tutti i riferimenti.
- **«Apri ed evidenzia»** apre il PDF originale alla pagina citata con il passaggio evidenziato in giallo. Per i documenti non-PDF si apre il file originale.

Se l'evidenza nei documenti è insufficiente, il sistema lo **dichiara esplicitamente** invece di inventare una risposta. Eventuali avvisi di verifica compaiono sotto la risposta.

### Feedback

Sotto ogni risposta ci sono i bottoni 👍 / 👎. Il giudizio negativo può includere un commento: aiuta l'amministratore a migliorare il sistema (le domande con giudizio negativo entrano nella batteria di valutazione).

### Gestione conversazioni

La colonna sinistra elenca le conversazioni precedenti: clic per riaprirle, icona per eliminarle. «Nuova conversazione» riparte da zero.

## Ricerca documenti (pagina Ricerca)

Per cercare direttamente nei documenti senza porre una domanda:

- digitare i termini (funzionano sia il significato sia i termini esatti: codici, sigle, numeri di delibera);
- filtrare per **fonte**, **tipologia** e **intervallo di date**;
- cliccare un risultato per aprire il documento alla pagina.

## Documenti (pagina Documenti)

- Elenco di tutti i documenti indicizzati con **stato** (in coda, in elaborazione, indicizzato, errore) e numero di pagine.
- **Caricamento diretto**: trascinare i file nell'area di upload (PDF, Word, Excel, PowerPoint, immagini, Markdown). Il documento è interrogabile entro pochi minuti; lo stato di avanzamento è visibile.
- **Eliminazione**: rimuove il documento dall'indice (i file originali delle cartelle di rete non vengono toccati).

## Knowledge Graph (pagina Knowledge Graph)

Visibile se l'amministratore ha attivato la funzione. Permette di:

- cercare **entità** (persone, organizzazioni, contratti, progetti, prodotti) estratte automaticamente dai documenti;
- vedere per ogni entità i documenti che la menzionano e le **relazioni** con le altre entità (es. *chi ha firmato cosa*), navigandole con un clic.

## Amministrazione (solo admin, pagina Amministrazione)

- **Utenti**: creare/disattivare utenti, cambiare ruolo e password.
- **Fonti**: aggiungere cartelle di rete da indicizzare, attivare/disattivare fonti, avviare la sincronizzazione.
- **Feedback**: statistiche e dettaglio dei giudizi degli utenti.
- **Audit**: registro completo delle interrogazioni — domanda, percorso, passi dell'agente, documenti consultati, tempi. Base per verifiche e diagnosi.

## Domande frequenti

- **Il sistema risponde che l'evidenza è insufficiente.** È il comportamento corretto: il documento cercato potrebbe non essere indicizzato o la domanda riguarda informazioni assenti dal corpus. Verificare nella pagina Documenti che la fonte sia indicizzata.
- **Ho caricato un file ma non lo trovo.** Controllare lo stato nella pagina Documenti: se «in elaborazione» attendere; se «errore» passare il mouse sul badge per il motivo.
- **Le risposte citano documenti che non dovrei vedere?** Segnalare all'amministratore: la visibilità è definita a livello di fonte e raccolta.
- **Il primo avvio è lento.** Normale: i modelli locali vengono scaricati e preriscaldati una tantum.

## Riservatezza

Documenti, indici e conversazioni risiedono sulla workstation del cliente. I connettori cloud (Google, Microsoft) operano in sola lettura. Il solo testo rilevante di ogni domanda viene inviato al servizio di generazione configurato (API Kimi) per produrre la risposta; nessun documento integrale lascia il perimetro.

## Appendice — Screenshot dell'interfaccia

Gli screenshot seguenti (cartella `docs/screenshots/`, viewport desktop
1440×900, temi chiaro e scuro) documentano l'aspetto delle pagine
principali. Rigenerabili con `npm run qa:shots` dalla cartella `frontend/`
(richiede il backend attivo con dati demo, vedi commento in
`frontend/scripts/qa-shots.mjs`).

- `01-login-dark.png`, `02-login-light.png` — pagina di accesso
- `10-chat-empty-dark.png`, `11-chat-empty-light.png` — chat vuota con suggerimenti
- `12-chat-conversation-dark.png`, `13-chat-conversation-light.png` — conversazione con markdown, citazioni e feedback
- `14-chat-citation-popover-dark.png` — anteprima citazione al passaggio del mouse
- `15-chat-citations-panel-dark.png`, `16-chat-citations-panel-light.png` — pannello «Fonti citate»
- `20-search-results-dark.png`, `21-search-results-light.png` — ricerca con risultati
- `30-documents-dark.png`, `31-documents-light.png` — archivio documenti con badge di stato
- `40-knowledge-dark.png`, `41-knowledge-light.png` — Knowledge Graph (stato non attivo)
- `50-admin-users-dark.png` — amministrazione: utenti
- `51-admin-feedback-dark.png` — amministrazione: feedback
- `52-admin-audit-dark.png` — amministrazione: registro audit
- `60-viewer-pdf-dark.png` — visualizzatore PDF integrato
