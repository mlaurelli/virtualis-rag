# Come funziona Virtualis — spiegato semplice

Guida divulgativa per chi usa o valuta il sistema, senza linguaggio tecnico.
Ogni affermazione qui sotto descrive il comportamento reale del software.

## 1. Cos'è Virtualis, in cinque righe

Virtualis è un assistente a cui si fanno domande in italiano (o inglese)
sui **documenti dell'azienda**, e che risponde **solo sulla base di quei
documenti**. Ogni risposta cita le fonti: si può cliccare e aprire il file
originale alla pagina esatta, con il passaggio evidenziato. Tutto gira su
un computer **dentro l'azienda**: i documenti non escono mai dal perimetro
aziendale. Se nei documenti la risposta non c'è, il sistema lo dice
chiaramente invece di inventarla.

## 2. Il viaggio di un documento: dalla cartella alla risposta

Per capire come funziona, immaginiamo Virtualis come una **biblioteca con
un bibliotecario molto diligente**. Un libro appena arrivato non può essere
consultato subito: prima va catalogato. Lo stesso vale per i documenti.

### Da dove arrivano i documenti

Un documento può entrare nel sistema in quattro modi:

- **Caricamento diretto**: si trascina il file nella pagina Documenti.
- **Cartella di rete aziendale**: una cartella condivisa, montata sul
  server in sola lettura, viene controllata periodicamente; i file nuovi o
  modificati entrano in lavorazione da soli.
- **Google Drive**: un connettore in sola lettura sincronizza le cartelle
  autorizzate, incluse le modifiche successive.
- **Microsoft 365**: un connettore importa OneDrive, SharePoint e le
  caselle di posta autorizzate (i messaggi diventano documenti di testo,
  gli allegati documenti a sé).

In tutti i casi il documento originale resta dove sta: Virtualis lavora su
una copia di lettura e non scrive mai verso le fonti.

### Passaggio 1 — La lettura (il libro viene "fotocopiato")

Il primo passo è **leggere il documento**, qualunque forma abbia: PDF,
Word, Excel, PowerPoint, pagine web, testo semplice e persino **immagini e
scansioni**. Per le scansioni — fogli che sono di fatto fotografie — entra
in gioco l'**OCR** (riconoscimento ottico dei caratteri, in italiano e
inglese): il software "guarda" l'immagine pagina per pagina e ne ricava il
testo, come farebbe una persona che la trascrive. Se un PDF contiene
troppo poco testo estraibile (tipico delle scansioni), il sistema se ne
accorge da solo e lo rilegge integralmente con l'OCR. I file identici già
lavorati vengono riconosciuti da un'impronta del contenuto: caricare due
volte lo stesso documento non crea duplicati.

### Passaggio 2 — Lo spezzettamento (il libro diventa schede)

Un contratto di 80 pagine non può essere maneggiato tutto intero a ogni
domanda. Il documento viene quindi diviso in **pezzi di senso compiuto** —
tecnicamente *chunk* — grandi più o meno come una scheda da mezza pagina,
rispettando la struttura del documento (titoli, sezioni, paragrafi). Ogni
pezzo conserva i suoi "dati anagrafici": da quale documento viene, da quale
pagina, da quale sezione. I pezzi vicini si sovrappongono leggermente, così
nessuna frase viene tagliata a metà sul confine.

### Passaggio 3 — L'impronta digitale del significato (lo schedario)

Ecco il passaggio più importante. Per ogni pezzo il sistema calcola un
**embedding**: una lunga lista di numeri che ne rappresenta il *significato*.
L'analogia giusta è quella delle **coordinate su una mappa**: così come due
città vicine hanno coordinate simili, due pezzi di testo che parlano della
stessa cosa hanno impronte numeriche simili — anche se usano parole
completamente diverse. «Tempi di consegna» e «quando arriva la merce»
avranno impronte vicine, pur non condividendo quasi nessuna parola. È
questo che permette a Virtualis di trovare i passaggi giusti anche quando
la domanda è formulata in modo diverso dal documento. Accanto all'impronta
"di significato", il sistema registra anche una seconda scheda basata sulle
**parole esatte** (utile per codici, sigle, numeri di delibera, nomi propri,
che il significato da solo potrebbe sfumare).

### Passaggio 4 — L'archivio speciale (lo schedario viene ordinato)

Tutte le schede finiscono in un **archivio specializzato** (un database
vettoriale, che vive sul disco del server aziendale), progettato proprio
per rispondere alla domanda: «quali schede hanno l'impronta più simile a
questa?». Da quel momento il documento è **interrogabile**. Nella pagina
Documenti si vede sempre lo stato di ogni file: *in coda*, *in elaborazione*,
*indicizzato* (cioè pronto) oppure *errore* (con il motivo visibile). Un
documento grande o una scansione da leggere con OCR può richiedere qualche
minuto: è normale.

## 3. Il viaggio di una domanda: dalla richiesta alla risposta

### Passo 1 — Capire la domanda

Per le domande semplici («quanto dura la garanzia nel contratto 2025?») il
sistema prende il **percorso rapido**: una sola ricerca mirata. Per le
domande che attraversano più documenti («le condizioni di garanzia sono
cambiate tra il 2023 e il 2025?») parte il **ciclo agentico**: la domanda
viene scomposta in sotto-domande, ognuna cercata separatamente, e i passi
sono visibili in tempo reale nell'interfaccia (classificazione, piano,
ricerche, sintesi, verifica). Se la domanda è un seguito della
conversazione («e nel 2024?»), il sistema la riformula da solo in una
domanda completa prima di cercare, usando lo scambio precedente come
contesto — mai come fonte di fatti.

### Passo 2 — Cercare i pezzi rilevanti

La domanda viene trasformata nella stessa impronta numerica vista sopra, e
l'archivio restituisce i pezzi con impronta più simile **e** quelli con le
parole esatte più corrispondenti. I due elenchi vengono fusi in un'unica
classifica. È il bibliotecario che gira per gli scaffali e torna con una
pila di schede candidate.

### Passo 3 — La doppia verifica (rerank)

La prima selezione è veloce ma approssimativa. Un secondo modello, più
lento e più preciso, **rilegge uno per uno** i candidati confrontandoli con
la domanda e li riordina: è il bibliotecario che si siede, apre davvero
ogni scheda e scarta quelle che suonavano bene ma non c'entrano. Solo i
pezzi che superano questo esame arrivano alla scrittura della risposta.

### Passo 4 — La risposta con le fonti

Il modello linguistico (il "redattore") riceve la domanda e **solo i pezzi
selezionati**, e scrive una risposta in cui ogni affermazione porta un
riferimento numerato `[n]`. Passando il mouse sul numero si vede
l'anteprima del passaggio; cliccando «Apri ed evidenzia» si apre il PDF
originale **alla pagina citata, col passaggio evidenziato in giallo**.

La risposta viene poi sottoposta a un controllo di coerenza: una
"revisione avversaria" prova attivamente a smentire le affermazioni usando
solo i passaggi citati. Il risultato diventa un giudizio di affidabilità
(alta / media / bassa) mostrato sotto la risposta; se la risposta risulta
fragile, viene riscritta una volta tenendo conto delle criticità.

### E quando non lo sa? Dice che non lo sa

Questo è il punto più importante dell'intero sistema. Ogni pezzo trovato
ha un **punteggio di pertinenza**: quanto è davvero attinente alla domanda,
secondo la doppia verifica. Se nemmeno il miglior pezzo supera la **soglia
minima di pertinenza**, Virtualis **si astiene**: risponde con una frase
del tipo *«Nel materiale indicizzato non ho trovato elementi sufficienti
per rispondere a…»*, magari suggerendo i documenti più vicini trovati
(«forse cercavi questi»).

È una scelta progettuale deliberata, non un limite: un assistente
documentale che inventa una risposta plausibile è molto peggio di uno che
ammette il vuoto. Fra la soglia minima e la soglia di "evidenza forte" la
risposta viene comunque data, ma con affidabilità segnalata più bassa e
controlli più severi. Questo meccanismo — la **barriera anti-allucinazione** —
è misurato da una batteria di collaudo dedicata, rieseguita a ogni
aggiornamento del sistema: le domande trabocchetto su argomenti assenti dal
corpus devono ricevere l'astensione.

## 4. Il grafo della conoscenza: la mappa dei protagonisti

Cercando pezzi di testo simili si risponde bene a domande puntuali. Ma
certe domande sono di un altro tipo: *«che rapporto c'è tra l'azienda Rossi
e il progetto Alfa?»*, *«quali documenti coinvolgono sia Bianchi sia il
contratto 2024?»*. Per queste serve una **mappa**.

Mentre i documenti vengono lavorati, il sistema può anche **leggerli alla
ricerca di protagonisti**: persone, organizzazioni, contratti, progetti,
prodotti, date, importi. Ognuno diventa un nodo, e i legami scoperti nei
testi («ha firmato», «fa parte di», «si riferisce a») diventano archi. Il
risultato è il **knowledge graph** — il grafo della conoscenza: un elenco
di *chi fa che cosa con chi*, con per ogni voce i documenti da cui è stata
tratta.

A cosa serve in pratica:

- nella pagina dedicata si può **esplorare la mappa**: cercare una persona
  o un'azienda e vedere i documenti che la menzionano e le relazioni con
  gli altri nodi, navigandole con un clic;
- nelle domande complesse, l'assistente la usa come **bussola**: se nella
  domanda compaiono nomi presenti nella mappa, il sistema guarda anche nei
  documenti collegati dalle relazioni — documenti che la sola ricerca per
  somiglianza avrebbe potuto mancare. Le affermazioni della risposta
  restano comunque ancorate ai passaggi dei documenti: la mappa orienta la
  ricerca, non è essa stessa una fonte citabile.

Il grafo si aggiorna da solo a ogni nuovo documento indicizzato e viene
ripulito quando un documento viene eliminato.

## 5. Perché è diverso da ChatGPT

| Aspetto | ChatGPT (e simili) | Virtualis |
|---|---|---|
| Dove gira il modello | Nei data center del fornitore | Sul server **dentro l'azienda** |
| Dove vanno i dati | Testo inviato al servizio esterno | **Nulla esce**: documenti, domande e risposte restano in locale |
| Su cosa risponde | Conoscenza generica appresa in addestramento | **Solo** i documenti aziendali indicizzati |
| Fonti | In genere assenti o non verificabili | Citazione puntuale, cliccabile fino alla pagina del file originale |
| Se non sa | Tende a rispondere comunque | **Si astiene** e lo dichiara esplicitamente |
| Tracciabilità | Nessuna | Registro di audit: ogni interrogazione, passaggio e documento consultato è registrato |

Il cuore generativo — il modello linguistico che scrive le risposte — gira
in locale sulla scheda grafica (GPU) del server aziendale, senza alcuna
connessione a servizi esterni. Anche tutti i modelli di supporto (impronte
del significato, doppia verifica, OCR) sono locali. L'unica uscita di rete
prevista è quella dei connettori autorizzati (Google Drive, Microsoft 365)
che **scaricano** documenti in sola lettura.

## 6. Domande frequenti

### Perché a volte è lento?

Dipende dal tipo di domanda. Quelle semplici richiedono pochi secondi;
quelle complesse attivano il ciclo agentico — scomposizione, ricerche
multiple, verifica, riscrittura — e possono richiedere qualche decina di
secondi: è il prezzo di un controllo più rigoroso, e i passi sono visibili
in tempo reale nell'interfaccia. Inoltre il modello generativo serve le
richieste **una alla volta**: se due persone fanno domande nello stesso
istante, la seconda aspetta il suo turno. Infine, dopo un riavvio del
server il modello va "scaldato" caricandolo in memoria sulla GPU: il
sistema lo fa da solo in avvio, ma la primissima richiesta può comunque
pagare qualche secondo in più.

### Ho caricato un documento ma non lo trova. Perché?

Le cause tipiche, in ordine di frequenza:

1. **È ancora in lavorazione**: controllare lo stato nella pagina Documenti
   (*in coda* / *in elaborazione*). Scansioni e file grandi richiedono
   minuti, non secondi.
2. **È andato in errore**: il badge di stato mostra il motivo passandoci il
   mouse (per esempio file troppo grande — oltre il limite di upload — o
   documento che ha superato la memoria dedicata alla conversione). Un file
   fallito può essere rimesso in coda.
3. **Formato non supportato**: sono gestiti PDF, Word, Excel, PowerPoint,
   HTML, Markdown, testo e le immagini più comuni (PNG, JPG, TIFF, BMP).
   CAD, archivi di posta e formati proprietari particolari restano fuori.
4. **La domanda usa parole troppo diverse dal documento**: riprovare con la
   ricerca documentale (pagina Ricerca), che accetta anche termini esatti
   come codici e sigle.

### I miei documenti sono al sicuro?

Il sistema è costruito per il perimetro aziendale:

- **tutto resta in locale**: documenti, indici, conversazioni e registri
  risiedono sul server aziendale; il modello generativo gira in sede,
  nessun testo viene inviato a servizi esterni per produrre le risposte;
- **accesso con nome utente e password**, con ruoli (amministratore /
  utente) e blocco anti tentativi ripetuti di login;
- **connessione cifrata (HTTPS)**: il traffico tra browser e server è
  cifrato, anche dentro la LAN aziendale;
- **sola lettura verso le fonti**: i connettori verso cartelle di rete,
  Google Drive e Microsoft 365 non possono modificare né cancellare nulla;
- **registro di audit**: ogni interrogazione è tracciata (chi, cosa, quali
  documenti consultati), a supporto di verifiche interne;
- **backup**: esiste una procedura di salvataggio dell'intero stato del
  sistema, pensata anche per l'esecuzione automatica periodica.

Nota di trasparenza: oggi tutti gli utenti autenticati vedono tutto
l'archivio indicizzato — i permessi fini dei singoli documenti (chi può
vedere cosa in SharePoint, per esempio) non sono ancora propagati. Se una
raccolta è riservata, va valutato con l'amministratore se indicizzarla.

### Come aggiungo documenti?

In quattro modi, a seconda di dove vivono:

- **pochi file, subito**: trascinarli nella pagina Documenti (upload web);
- **una cartella condivisa**: farla registrare come *fonte* da un
  amministratore, poi usare «Aggiorna da cartella di rete» per le nuove
  versioni;
- **Google Drive / Microsoft 365**: configurazione una tantum da parte
  dell'amministratore, poi le sincronizzazioni portano dentro novità e
  modifiche;
- **eliminazione**: dalla pagina Documenti si può rimuovere un documento
  dall'indice; i file originali delle fonti esterne non vengono toccati.

### Chi controlla che le risposte restino buone nel tempo?

Due meccanismi. Il primo è il **feedback degli utenti**: sotto ogni
risposta ci sono i bottoni pollice su / pollice giù, con commento
facoltativo; gli amministratori vedono le statistiche aggregate. Il secondo
è una **batteria di valutazione automatica**: un elenco di domande di
riferimento con le risposte attese, rieseguito a ogni aggiornamento del
sistema — comprese le domande che hanno ricevuto pollice giù, così un
problema segnalato diventa un controllo permanente.

## 7. Glossario minimo

- **Chunk (pezzo)**: frammento di documento di senso compiuto, circa mezza pagina, con indicazioni su documento, pagina e sezione d'origine. È l'unità che il sistema cerca e cita.
- **Embedding (impronta)**: lista di numeri che rappresenta il significato di un testo: testi che dicono cose simili hanno impronte simili.
- **Indicizzazione**: l'insieme dei passaggi che rendono un documento interrogabile (lettura, spezzettamento, impronte, archiviazione).
- **OCR**: riconoscimento ottico dei caratteri; trasforma scansioni e immagini in testo leggibile dal sistema.
- **Ricerca ibrida**: combinazione di ricerca per significato (impronte) e per parole esatte (utile per codici e sigle), fusi in un'unica classifica.
- **Rerank (doppia verifica)**: secondo esame, più lento e preciso, che riordina i pezzi candidati confrontandoli davvero con la domanda.
- **Soglia di pertinenza**: punteggio minimo di attinenza sotto il quale un pezzo non è una prova valida. Se nessun pezzo la supera, il sistema si astiene.
- **Astensione**: la risposta «non ho trovato elementi sufficienti nel materiale indicizzato». È il comportamento voluto quando l'evidenza manca: l'alternativa sarebbe inventare.
- **LLM (modello linguistico)**: il motore che scrive la risposta in linguaggio naturale. In Virtualis gira in locale, sul server aziendale, e riceve solo la domanda e i pezzi selezionati.
- **GPU**: la scheda grafica del server, usata come motore di calcolo del modello: è ciò che rende le risposte ragionevolmente veloci.
- **Knowledge graph (grafo della conoscenza)**: mappa di persone, aziende, contratti e progetti estratti dai documenti, con le relazioni tra loro.
- **Fonte (source)**: un canale di provenienza dei documenti (cartella di rete, Google Drive, Microsoft 365, upload).
- **Audit (registro)**: la traccia completa delle interrogazioni: domanda, percorso seguito, documenti consultati, tempi.
- **Citazione**: riferimento numerato `[n]` in fondo a ogni affermazione, collegato al passaggio esatto del documento originale.
- **Agente / ciclo agentico**: il percorso "a più tappe" usato per le domande complesse: scomposizione in sotto-domande, ricerche multiple, valutazione della copertura, sintesi e verifica finale.
