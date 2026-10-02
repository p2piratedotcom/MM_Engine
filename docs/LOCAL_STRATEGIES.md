# Strategie locali: TUI + KDF + copertura MEXC

## Uso previsto

Il PC Zorin ospita KDF, il servizio locale e la TUI. Il servizio include feed
pubblici, supervisione, quotazione, registro persistente e, con `--with-mexc`,
il worker MEXC senza interfaccia grafica. Non occorrono VPS, SSH o Qt.
Le credenziali continuano a essere caricate dal portachiavi, non dai profili
delle strategie. I trasferimenti automatici restano disabilitati.

Il PC deve restare acceso. Chiudere solo la TUI non ferma il servizio, mentre
spegnere il PC interrompe anche feed e copertura. La durata degli atomic swap
non elimina il rischio di movimento del prezzo durante un'interruzione.

Questa implementazione **non è stata attivata sul canary finanziato esistente**.
Nessun nuovo ordine reale è stato inviato durante il suo sviluppo. Prima di
adottarla sul canary serve un riavvio controllato, dopo verifica di ordini,
swap e journal. Non avviare una seconda istanza con la stessa configurazione
KDF mentre i servizi precedenti sono attivi.

## Avvio (dopo aver predisposto l'ambiente della prova)

Dalla cartella del progetto, con gli stessi segreti e percorsi privati già
configurati per la prova locale:

```bash
.venv/bin/python -m kdf_mm local-service \
  --config runtime/local-funded-test/maker/MM2.json \
  --start-kdf --with-mexc --mexc-profile default
```

In un secondo terminale, con lo stesso `KDF_MM_AGENT_TOKEN` e lo stesso
`KDF_MM_DESKTOP_JOURNAL_DB`:

```bash
.venv/bin/python -m kdf_mm tui
```

`--with-mexc` include il worker locale, ma NON abilita da solo il trading.
Per le sole anteprime mantenere `KDF_MM_KDF_ORDER_WRITES=false`,
`KDF_MM_AUTO_HEDGE=false`, `KDF_MM_LIVE_TRADING=false` e
`KDF_MM_LIVE_TRANSFERS=false`. Il worker legge i saldi Spot per dimensionare
le proposte. Un feed pubblico da solo non dimostra la copertura finanziaria.

L'avvio live delle nuove strategie richiede le scritture KDF, l'auto-hedge e
il trading MEXC abilitati esplicitamente, più la conferma nella TUI. Le
variabili e i wallet del canary non sono modificati da questa guida.

Il worker precedente `desktop-agent` continua a funzionare senza GUI. Usare
**o** quel worker **o** `--with-mexc`, mai entrambi. Le nuove versioni usano
un lock sul journal per impedire due worker concorrenti sullo stesso file.
Un vecchio processo già avviato prima dell'aggiornamento va fermato durante
il passaggio: non conosce ancora quel lock o il protocollo multi-gamba.

## Procedura TUI

La pagina **[4] Repricing automatico** mostra per prima il motore strategie:
prezzo automatico attivo (anche durante l'attesa di stabilità/intervallo), sospeso
per un controllo, in pausa, motore fermo o prezzo fisso senza repricing. Il numero
degli ordini KDF pubblicati è separato da quello delle strategie abilitate.
Le frecce scorrono le strategie; **[2]** apre la pagina per gestirle.

**[L]** passa esplicitamente al motore precedente e viceversa. I suoi comandi
G/H, soglia e intervallo non agiscono sulle strategie; nella panoramica strategie
non vengono inviati comandi al vecchio motore. Se quest'ultimo non ha lati
configurati, compare «NON UTILIZZATO», non un blocco delle strategie.

La pagina **[3] Ordini** riporta il riepilogo del repricing delle strategie e **[4]**
ne apre i dettagli. Anche l'elenco della pagina [2] indica per ciascuna strategia
«REPRICING ATTIVO», lo stato di sospensione/pausa oppure «PREZZO FISSO».
Se la lettura dello stato fallisce, non viene dichiarato attivo un motore usando
informazioni della lettura precedente.

### Colonne degli ordini attivi

La pagina [3] mostra il **PREMIUM impostato** con segno, separato da commissioni
e buffer. L'associazione usa l'UUID persistente dell'ordine, non il solo pair:
i livelli Scala possono avere premium diversi. Un ordine non associato mostra n/d.

**PREZZO** resta espresso in coin comprata per coin venduta; la coppia in tabella
indica esplicitamente «vendi > compra». **USDT/COIN** compare per coppie cross o
inverse: indica il prezzo pubblicato della coin principale, valorizzato in USDT.
Per USDT-BEP20 → ARRR, per esempio, è l'inverso del prezzo KDF, in USDT/ARRR.
Per ARRR → BCH è il prezzo BCH/ARRR moltiplicato per il midpoint BCH/USDT aggiornato.
Con feed scaduto o assente compare n/d; non si ricicla una vecchia anteprima.
USDT non viene etichettato come USD e non è garantita la parità tra i due.
Per ARRR → USDT-BEP20 il valore è già nella colonna PREZZO e non viene duplicato.

Su schermi stretti ogni ordine occupa due righe con colonne numeriche allineate.
PgUp/PgDown scorrono l'elenco. I nuovi metadati richiedono la nuova versione del
servizio locale oltre alla TUI: con un servizio ancora precedente compaiono n/d,
senza ricostruzioni ambigue. Aggiornare il servizio solo con riavvio controllato,
dopo verifica di ordini e swap; la modifica non riavvia la sessione live.

Dal menu `[2] Mercato e nuova quotazione`:

- `N`: nuova strategia; selezione delle coin attive, mapping MEXC automatico
  quando noto, premium, prezzo auto/fisso, quantità auto/fissa, massimo per
  ordine auto/fisso, rifornimento, budget e intervallo aggiornamenti;
- mercato opposto: `no`, `si`, `personalizza`. La personalizzazione conserva
  i ticker invertiti e riapre le altre scelte;
- nell'anteprima `↑/↓` scorrono limiti e operazioni di copertura; `←/→` o
  Tab selezionano `[Salva]` / `[Modifica]`, Invio conferma. Salva conserva la
  strategia **in pausa**, senza pubblicare;
- `G`: apre `[Avvia]` / `[Indietro]`, selezionabili con frecce o Tab e Invio.
  La scelta iniziale è Indietro; Avvia resta subordinato ai permessi live;
- `H`: pausa e ritiro dell'ordine di quella strategia;
- `E`: modifica una strategia in pausa, senza azzerarne la storia o i budget;
- `D` (o Canc): apre la conferma di eliminazione. Se esiste il lato opposto,
  scegliere `[Solo questa]` oppure `[Entrambe]`; altrimenti `[Elimina]`.
  `[Indietro]` è preselezionato. Dopo Invio vengono messe in pausa le sole
  strategie confermate e ritirati i loro ordini: non occorre premere H.
  Swap non risolti e anomalie di riconciliazione bloccano l'operazione.
  La direzione torna disponibile per una nuova strategia; la configurazione
  precedente è archiviata nel database insieme allo storico, non cancellata
  fisicamente. Entrambe le configurazioni vengono archiviate insieme o
  nessuna: se una cancellazione KDF fallisce, restano visibili e gli ordini
  già ritirati non vengono ripubblicati. Il lato opposto viene eliminato
  soltanto scegliendo esplicitamente Entrambe;
- `Esc`: indietro/menu. `Q` chiude soltanto dal menu principale;
- `V`: vecchia pagina di quotazione, conservata per compatibilità.

Le richieste HTTP della nuova pagina sono eseguite fuori dal ciclo di input.
`Ctrl+U` svuota un campo; lettere come `q` restano testo mentre si compila.
I valori già inseriti rimangono disponibili tornando indietro.
L'aggiornamento automatico della lista non scarta i tasti né cambia la
strategia mostrata in una conferma già aperta.

«Quantità massima da vendere per volta»: `auto` non aggiunge un limite manuale
alla quantità calcolata sui book MEXC; `fixed` impone un tetto aggiuntivo.
Restano sempre vincolanti profondità, fondi KDF, copertura MEXC e budget residuo.
Con quantità fissa un importo non copribile viene bloccato, non ridotto di nascosto.

Il budget senza rifornimento mostra il saldo KDF letto all'apertura del modulo
e consente `tutto`, `50%`, `25%` oppure `personalizza`. Le percentuali diventano
un importo al momento della scelta, non si espandono con depositi successivi;
la disponibilità effettiva viene ricontrollata prima della pubblicazione.
Le nuove configurazioni TUI non hanno un tetto vendite sulle 24 ore né una
soglia percentuale di variazione quantità: restano intervallo minimo e
stabilizzazione degli aumenti. Un ordine invariato non viene riscritto.
Le strategie già salvate conservano i vecchi limiti fino alla modifica esplicita.

Gli errori HTTP sono visualizzati in una pagina scorrevole; Invio/Esc riporta
al modulo senza perdere i valori. Se il worker MEXC precedente rifiuta i simboli
con parentesi o caratteri non ASCII, riavviare anche il servizio aggiornato:
questi simboli vengono esclusi dalle capacità del bot, senza invalidare i saldi
Spot e i mercati supportati. Il trading live non viene abilitato dalla correzione.

## Prezzi e unità

Le quantità sono sempre nella **coin venduta su KDF**, anche quando si vende
USDT per comprare una coin. Il prezzo è espresso in quote/base. Se uno dei
due asset è USDT, la base canonica è l'altro; altrimenti è la coin inizialmente
scelta come base del mercato. I nomi interni `SELL_ARRR`/`BUY_ARRR` sono alias
storici: il motore non limita le strategie ad ARRR.

In automatico si usano i lati eseguibili dei book Spot asset/USDT. Per vendere
la base: ask della base / bid della quote. Per comprarla: bid della base /
ask della quote. A questa relazione si applicano premium firmato, commissioni
configurate per le gambe e buffer. Le camminate nel book verificano quantità,
controvalore e limite di impatto; il riferimento di prezzo è il miglior
bid/ask eseguibile, non l'ultimo trade o la media delle 24 ore.

In modalità fissa il prezzo inserito è il rapporto finale quote/base:
il premium non viene applicato una seconda volta e non c'è repricing.
Le valorizzazioni USDT sono informative e possono cambiare anche quando il
rapporto fisso KDF non cambia. USDT non è assunto equivalente a USD.

Il mercato opposto parte dalla stessa quantità equivalente in asset base
dell'anteprima e dal premium di segno opposto. Non copia numericamente una
quantità ARRR dentro un campo BCH o USDT. I due budget diventano indipendenti
e restano soggetti ai fondi condivisi; in modalità automatica la liquidità
dei due lati può produrre quantità finali diverse.

## Quantità, soglie e sicurezza

- Limite del livello peggiore: 1% dal miglior ask per BUY e dal miglior bid
  per SELL, con arrotondamento del prezzo limite verso l'interno.
- Quantità automatica: massimo metà della profondità così misurata. Ulteriori
  tetti: saldo KDF spendibile (`max_maker_vol`, non saldo lordo), altre quote
  del pool, saldi Spot, profondità già impegnata, limite utente, volume 24h,
  budget residuo e, solo per configurazioni precedenti che lo prevedono,
  tetto mobile sulle ultime 24 ore.
- Per A/B si dimensionano BUY A/USDT e SELL B/USDT sulle quantità effettive
  promesse dall'ordine KDF. Devono esserci USDT per il BUY e B già disponibile
  nello Spot per il SELL; non si aspetta l'arrivo del deposito dallo swap.
  La dichiarazione firmata del worker deve confermare anche che la chiave
  API può operare su tutti i simboli richiesti (`selfSymbols`).
- Quantità fissa: viene pubblicata per intero oppure si attende. Non viene
  ridotta silenziosamente quando manca copertura.
- Nessuna soglia percentuale quantità nelle nuove strategie TUI; soglia
  prezzo 0,25%, intervallo 60 secondi. Le vecchie strategie mantengono la
  soglia quantità salvata finché non vengono modificate.
  Gli aumenti richiedono tre snapshot distinti con quantità sufficientemente
  stabile. Snapshot ripetuti o un singolo picco non bastano.
- Riduzioni necessarie alla sicurezza non aspettano soglia/intervallo.
  Feed scaduto, coin inattiva, swap pendente o copertura assente ritirano
  l'ordine. La ripresa automatica attende nuovamente le condizioni stabili;
  la pausa manuale resta invece persistente.
- Gli ordini avanzati impongono anche un minimo di swap compatibile con i
  minimi MEXC osservati, con margine di precisione. I minimi possono cambiare:
  un hedge effettivo non valido si blocca per revisione.

Il vincolo 1% è una misura sul book osservato, non una garanzia futura.
Le coperture usano LIMIT aggressive, non market senza limite: un book che
si svuota può lasciare una copertura incompleta. I limiti non vengono
progressivamente allargati durante il recupero.

## Persistenza, esecuzione e recupero

Le strategie sono in `<KDF_MM_STATE_DB>.strategies.sqlite3`, file privato.
Ogni swap riserva subito il suo importo effettivamente venduto una sola volta.
Repricing, riconsegna di eventi, pause e riavvii non azzerano i consumi.
Con esaurimento si sottrae dal budget totale; con rifornimento rimangono i
limiti di liquidità e copertura, il massimo manuale se impostato e gli
eventuali limiti 24h delle configurazioni precedenti. Gli swap falliti trattengono il
budget conservativamente, anche se è previsto un rimborso: niente riaperture
basate su un timeout presunto.

Il trigger di copertura rimane `MakerPaymentSent`, non una semplice proposta
di match. L'evento firmato contiene ticker, route e quantità effettive KDF.
Per la nuova strategia, il worker pre-verifica tutte le gambe prima del primo
invio. Registra un'intenzione e un `clientOrderId` deterministico **prima**
della chiamata reale. In caso di risposta persa interroga quell'identificativo
e non ripete l'invio alla cieca. Le API di consultazione/cancellazione per
identificativo sono documentate nella
[documentazione ufficiale MEXC Spot](https://mexcdevelop.github.io/apidocs/spot_v3_en/#query-order).

Le gambe sono sequenziali, non atomiche. Un residuo dopo cancellazione, una
risposta incoerente o un errore della seconda gamba richiedono revisione; il
permesso di nuova quotazione non viene rinnovato. Gli arrotondamenti sono
registrati esplicitamente: BUY verso l'alto, SELL verso il basso, massimo
0,01 USDT di residuo per gamba; oltre tale soglia non si invia il basket.
La pagina Eventi/coperture mostra gli esiti delle singole gambe.

Il rifornimento riparte solo con esito KDF riuscito e tutte le gambe registrate
FILLED nel journal locale. L'ack automatico riguarda esclusivamente quello
swap, non gli altri swap del pool. Un riavvio durante una scrittura KDF di
esito incerto produce `REVIEW_REQUIRED`: non viene ripubblicato un ordine
potenzialmente già esistente. Pausa/avvio non eliminano questa protezione.

La coppia di configurazioni viene salvata in una transazione, in pausa.
L'avvio dei due lati è separato: non si promette atomicità delle due RPC KDF.
Prima di ciascuna scrittura viene ricontrollata la copertura aggregata.

## Compatibilità e limite economico ancora aperto

I vecchi target repricing e i loro file restano invariati. Non vengono
convertiti automaticamente in nuovi budget o rifornimenti. Per lo stesso
mercato/lato non si possono sovrapporre i due gestori: ritirare l'ordine e
rimuovere il vecchio target prima di configurare la nuova strategia.

Il ledger precedente non è ancora una contabilità multi-gamba: per questi
nuovi eventi mostra `BASKET_ACCOUNTING_PENDING`, senza profitto realizzato né
roll-forward inventato. Le quantità e i controvalori eseguiti sono nel journal;
resta da estendere l'importazione dei fill/commissioni a ciascuna gamba per
ottenere un P/L netto verificato. Non usare il vecchio totale P/L come misura
del guadagno di un basket.

## Collaudo prima del passaggio live

### Quantità fattibile e copertura

Durante la creazione/modifica, nei passi relativi alla quantità compare una
stima aggiornata del massimo fattibile e del minimo hedge MEXC, espressi nella
coin venduta. Include saldo KDF, profondità residua dopo gli altri ordini,
saldi Spot netti e commissioni. Se il massimo è sotto il minimo, il massimo
**pubblicabile è zero**: aumentare i fondi non risolve un book insufficiente.
Il riquadro mostra anche, per la quantità fissa inserita (oppure per il minimo
necessario quando non esiste capacità automatica), asset richiesti e mancanti.
Sono stime del mercato corrente, non prenotazioni: salvataggio e pubblicazione
rifanno i controlli. Budget/limiti manuali impostati dopo possono ridurle.

L'analisi MEXC del riequilibrio è distinta dall'autorizzazione a pubblicare:
per una strategia fissa conserva l'intera riserva configurata anche se il book
si è assottigliato, segnalando il limite di liquidità senza interrompere
l'analisi. Gli ordini MEXC suggeriti restano limitati dalla profondità e dai
minimi effettivi. Se non è possibile determinare tutti i target, non propone
trade o trasferimenti basandosi su una copertura incompleta.

### Attivazione EVM: recupero automatico dei nodi

L'attivazione di una piattaforma EVM (per esempio BNB) e dei token del profilo
prova un nodo configurato alla volta. Se KDF restituisce un errore definitivo,
il servizio passa al successivo, conservando gli stessi token e parametri.
Il recupero prosegue in background anche chiudendo la TUI. Solo dopo il
fallimento di tutti i nodi viene segnalato il fallimento complessivo.
Nella pagina KDF, **[S]** mostra un resoconto scorrevole con nodi e cause.

Un timeout della API KDF locale non prova che il nodo esterno sia guasto:
il servizio continua a verificare lo stesso task. Se si perde la risposta
all'avvio di un nuovo tentativo, mostra invece «esito da verificare» e non
reinvia l'attivazione. I tentativi sono conservati in memoria del servizio:
riaprire la TUI non li perde, riavviare il servizio sì. Non riavviare durante
un'attivazione incerta senza prima verificarne l'esito su KDF.
La modifica riguarda le attivazioni EVM; il recupero Electrum/ZHTLC resta
gestito da KDF. Non modifica nodi o coin già attivi né ordini live.

### Test automatici

Il riequilibrio MEXC dalla TUI usa **[E] → Invio**: «Esegui» è già
selezionato nella conferma del singolo ordine mostrato; Esc annulla.
Il worker locale aggiornato mantiene l'esclusione tra worker duplicati, ma
coordina ogni ciclo con il riequilibrio: nessun hedge può sovrapporsi
all'invio manuale. Se un ciclo è in corso, attendere il completamento e
ricalcolare la proposta. Restano obbligatori mercati/repricing in pausa,
assenza di ordini/swap e hedge pendenti, e verifica degli esiti incerti.
Un ordine rebalance aperto o incerto sospende i nuovi cicli operativi fino
alla riconciliazione tramite **[R]**. I worker di versioni precedenti o non
coordinati restano bloccanti: riavviare in modo controllato il servizio
aggiornato, non cancellare i file di blocco.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Sono inclusi dimensionamento su due book, quattro combinazioni auto/fisso,
pool condivisi, minimi, persistenza e idempotenza del budget, pausa/ripresa,
snapshot ripetuti, cali improvvisi, crescita transitoria, feed scaduto,
scrittura KDF incerta, ripartenza di un hedge con timeout, seconda gamba
parziale, validazione degli eventi e ripresa solo dopo doppio esito.
Un test PTY verifica input, `q` nei campi, ridimensionamenti e ripristino del
terminale. I test sono isolati dai wallet finanziati e dalle chiavi reali.

Passaggio successivo: prova visiva in sola anteprima, poi collaudo finanziato
esplicitamente autorizzato con piccoli importi, prima diretto e poi incrociato.

### Ordini sospesi e navigazione

Il database di proprietà ordini contiene anche `order_events`, uno storico
permanente append-only. Non viene ricostruito a posteriori per gli ordini vecchi:
le cause già perse restano sconosciute. Alla prima apertura della versione
aggiornata la tabella viene creata senza modificare ordini o strategie live.

Per ogni ritiro richiesto si registra `CANCEL_REQUESTED` **prima** della chiamata
KDF, con UUID, strategia quando disponibile, origine e spiegazione. Un errore
di invio produce `CANCEL_FAILED_OR_UNCERTAIN`, non una cancellazione confermata.
`STATE_CHANGED` conserva insieme stato, origine e motivo nella stessa scrittura
SQLite dello stato dell'ordine. `PUBLISHED` registra l'iscrizione di un nuovo
UUID nel registro locale; `STRATEGY_BOUND` ne registra l'associazione alla
strategia. La corrispondenza UUID/strategia resta consultabile anche nel registro
delle strategie per gli ordini antecedenti all'aggiornamento.

Le origini distinguono `strategy_safety`, `strategy_pause`, `strategy_budget`,
`hedge_depth`, `coverage`, `market_data`, `shared_inventory`, `shutdown`,
`manual` e `reconciliation`. La riconciliazione priva di un nuovo motivo non
svuota quello già presente; una cancellazione esterna senza spiegazione viene
indicata come «Causa non disponibile da KDF». Le successive modifiche alla
diagnostica corrente non cancellano gli eventi precedenti.

Nei controlli periodici usare `order_events` ordinato per `id` e il suo timestamp
UTC `observed_at`: contare i passaggi confermati a `CANCELLED`, non le richieste,
i cambi del solo motivo (`REASON_UPDATED`) o i tentativi incerti. Confrontare le
pubblicazioni della stessa strategia per stimare il tempo fuori mercato, senza
confonderlo con la conferma di propagazione sulla rete. L'aggiornamento non
aggiunge reinvii automatici e non cambia la logica dell'hedging.

La pagina **I miei ordini KDF** mantiene una riga per ogni strategia non
eliminata, anche durante pausa, attesa di liquidità, ripubblicazione o
esaurimento del budget. Una riga senza ordine pubblicato mostra lo stato e
il motivo; prezzo e quantità pubblicati sono `n/d`, non valori inventati
dall'ultima anteprima. I comandi M/C continuano a operare solo sugli UUID
realmente presenti nel registro degli ordini aperti.

Il repricing ordinario aggiorna l'ordine esistente. I controlli di sicurezza
possono invece ritirarlo (per esempio quando la profondità per l'hedge MEXC
si riduce) e ripubblicarlo dopo il ripristino delle condizioni di copertura
e stabilità. Non si tratta di una cancellazione della strategia.
Se la lettura fallisce, l'ultimo elenco resta visibile come **NON VERIFICATO**.
Da questa pagina, **[4]** apre i dettagli del repricing e **Esc** torna
agli ordini; se aperti dal menu principale, i dettagli tornano al menu.
Per il nuovo elenco aggiornare sia servizio sia TUI: la risposta `/v1/orders`
mantiene gli ordini azionabili separati dagli stati delle strategie.

### Ripresa prudente e diagnostica della copertura

In **Scala**, il campo quantità accetta `0` come scorciatoia esplicita per
copiare la quantità attualmente pubblicata dell'originale. Non significa
zero coin o nessun impegno aggiuntivo: il nuovo livello ha quantità fissa e
richiede copertura propria. L'anteprima mostra il numero effettivo e la
pubblicazione lo ricontrolla; se l'originale cambia quantità occorre una nuova
conferma. Se non è pubblicato, inserire un importo esplicito o attendere.
La percentuale custom auto non viene trasferita a questo nuovo livello fisso.
Le riduzioni confermate degli originali custom auto salvano un limite che
non applica nuovamente la percentuale alla quantità già ridotta.

La creazione/modifica offre `[max auto]`, `[custom auto]` e `[fixed]`.
Max auto usa il massimo copribile residuo; custom auto chiede una percentuale
maggiore di zero e fino al 100% di quel massimo, dopo tutti i limiti e le
prenotazioni degli altri ordini. Esempio: massimo residuo 40 ARRR, custom
auto 25% → 10 ARRR. Non è il 25% aggiuntivo del book totale e non modifica
il limite d'impatto dell'1% o la quota massima di profondità del 50%.
La percentuale si ricalcola a ogni aggiornamento; se l'importo risultante
è sotto i minimi MEXC, l'ordine resta sospeso senza arrotondarlo verso l'alto.
Fixed mantiene una quantità precisa e viene sospeso se non più copribile.
Le strategie già salvate senza percentuale mantengono il comportamento
precedente (max auto o fixed); nessuna migrazione dei parametri live.

Le quantità automatiche confermano un minimo prudente attraverso gli snapshot
distinti richiesti: un aumento temporaneo del massimo non obbliga più ad
aspettare tre quantità esattamente identiche. Ogni piano viene ricalcolato
integralmente e ricontrollato prima della pubblicazione; quantità fisse,
limite d'impatto, quota di profondità e intervallo minimo restano invariati.
Le riduzioni di sicurezza restano immediate.

Una carenza di profondità identificata per uno specifico ordine ritira solo
quell'ordine. Saldi scaduti, deficit complessivi e anomalie non classificate
continuano a bloccare globalmente. L'esito della cancellazione deve essere
confermato prima di considerare liberata la copertura.

Il worker misura separatamente sincronizzazione eventi, hedging, orario MEXC,
saldo Spot, simboli autorizzati e consegna locale del permesso. I cicli lenti
(almeno metà della durata del permesso) e gli errori riportano `timings_ms`
nei log; gli errori di rinnovo indicano anche la fase fallita. La cadenza è
calcolata dall'inizio del ciclo, senza aggiungere sempre altri tre secondi
al tempo già trascorso. Non si allunga la validità dei saldi: il permesso
è datato all'inizio della richiesta saldo e non viene inviato se è già scaduto.

### Correzioni concorrenza e attese — 18 settembre 2026

- Una lettura KDF iniziata prima di una cancellazione non può riportare OPEN
  l'ordine né sovrascriverne lo stato terminale/motivo. `note_seen` e
  `note_missing` tollerano una transizione concorrente; UUID inesistenti
  restano errori. I cambiamenti terminali della riconciliazione sono condizionali
  allo stato ancora OPEN.
- Pubblicazione e repricing controllano la profondità del piano proposto e
  riservano comunque i fondi di tutti gli altri ordini. Un deficit di profondità
  di un altro ordine non causa più il ritiro di quello corrente. Il controllo
  periodico resta selettivo; la verifica successiva dei saldi non ripete una
  verifica globale della profondità. Saldi insufficienti/scaduti e errori
  non classificati mantengono il blocco di sicurezza.
- Una cancellazione/attesa per errore azzera la vecchia quantità candidata.
  Se il precedente limite conservativo scende sotto il nuovo minimo MEXC,
  si ricomincia dal piano corrente valido, con nuove conferme su snapshot
  distinti. Non si aggirano i minimi, la percentuale custom o il limite d'impatto.
- Il feed legge statistiche 24h prima del book, così il book non invecchia
  aspettando la risposta del ticker. Lo stato feed espone tempi separati per
  regole, ticker e profondità. Il worker rinnova la sincronizzazione oraria
  ogni 60 secondi (e dopo qualsiasi errore di rinnovo), verifica ogni volta
  le capacità della chiave e legge i saldi per ultimi. Nessuna cache dei saldi
  né estensione della durata dei permessi.
- `strategy_events` nel database strategie conserva cambiamenti di stato e
  motivo, conferme e preview. `runtime_samples` nel journal desktop conserva
  tempi/errori almeno ogni minuto, inoltre per cicli lenti o falliti, anche
  se il processo è avviato da un terminale senza log su file. Non contiene
  chiavi, token o saldi. Le nuove tabelle sono additive; lo storico finanziario
  resta intatto. Nessuna ricostruzione artificiale degli eventi antecedenti.

Le modifiche entrano in funzione al riavvio dei processi Python agent e worker
desktop. Non richiedono di cambiare seed, premi, quantità o configurazione KDF.

#### Limite dell'analisi storica delle due assenze prolungate

Il 17 settembre (orari UTC) ARRR→USDT-BEP20 è stato cancellato alle
21:33:41.467 e ripubblicato alle 22:04:18.749 (1837,282 s);
ARRR→LTC alle 21:34:10.318 e 22:04:15.402 (1805,084 s).
Le cause iniziali conservate indicano BUY ARRR richiesti rispettivamente
15,74180829 e 15,67587889, contro una capacità consentita di 7,2200.
Con custom auto 40%, quella capacità produce al massimo 2,888 ARRR, prima
di ulteriori riserve/limiti: se inferiore al minimo corrente, non si può
pubblicare. Le strategie non avevano ancora una cronologia dei tentativi
intermedi e l'output worker era su terminale, non su file. Non è dimostrabile
che tutto l'intervallo dipendesse dalla liquidità, né dal blocco della vecchia
quantità candidata ora corretto. I nuovi registri servono a distinguere
queste cause nei controlli successivi senza rilassare la copertura.

### Pubblicazione completata dopo timeout — recupero persistente

Il 18 settembre, alle 12:01:01 UTC, la strategia ARRR→USDT-BEP20 ha
iniziato una pubblicazione. Alle 12:01:16 è passata in REVIEW_REQUIRED;
l'ordine KDF riporta creazione alle 12:01:18.852. La singola rilettura
immediata non basta a recuperare una creazione tardiva o una rilettura fallita.
L'UUID `676974c5-c950-40f3-87a6-e0a9dcc6e0fb`, 25,86859401 ARRR a
0,3023064 USDT-BEP20, è stato riconciliato con autorizzazione esplicita,
attribuito prima della cancellazione e cancellato con conferma. Nessuno swap
associato risultava presente. Le quattro strategie sono state lasciate in
pausa durante l'intervento; nessun nuovo ordine o hedge inviato dal recupero.

Nuovo comportamento per le pubblicazioni effettuate dopo il caricamento del fix:

- `publication_intents` nel registro ownership salva prima di setprice il
  piano esatto, la strategia, il minimo, l'istante e gli UUID preesistenti.
  Un'intenzione irrisolta impedisce un secondo invio della stessa strategia.
- In REVIEW_REQUIRED il worker rilegge KDF al massimo ogni 10 secondi.
  Recupera solo un nuovo UUID univoco, creato nella finestra del tentativo
  (tolleranza oraria 5 s, creazione entro 120 s), con coppia, prezzo, volume
  totale/disponibile e minimo corrispondenti, senza matches o swap avviati.
  Il registro sopravvive al riavvio. Nessuna setprice viene reinviata.
- Dopo l'attribuzione a ownership e strategia, RECOVERING attende il normale
  riconciliatore prima di riprendere i controlli correnti e il repricing.
  Copertura, minimi, fondi e feed non vengono derogati. Le pause manuali
  sospendono il recupero automatico delle intenzioni incerte (HELD).
- Assenza, ambiguità, consumi parziali o dati diversi richiedono ancora verifica
  manuale. Non si considera mai l'assenza dello UUID prova di scrittura fallita.
  Le pubblicazioni storiche prive d'intenzione persistente non vengono adottate.
- Errori nel preflight, prima dell'invio, sono distinti dai timeout della
  scrittura: non bloccano permanentemente una strategia per una scrittura mai
  partita. Un preflight troppo lungo fa ricalcolare il piano; prima di inviare
  sono nuovamente verificati feed e copertura.

L'errore di trasporto distingue timeout e tipo di errore di connessione senza
stampare URL firmati o credenziali; gli errori KDF identificano il metodo RPC.
Le letture saldi/capacità MEXC del worker usano un timeout I/O massimo di 3 s
(inferiore con lease più breve), la sincronizzazione oraria al massimo 2 s.
I timeout del client usato per gli ordini non vengono cambiati, né sono aggiunti
retry alle scritture finanziarie. TTL delle lease e recvWindow restano invariati.

I tempi osservati prima dell'intervento arrivavano a 11 s e MEXC rifiutava
alcune richieste con codice 700003 (timestamp fuori recvWindow). Congestione
durante un download è compatibile con questi dati, ma non dimostrata: i tempi
includono rete e servizio remoto. Il timeout setprice riguarda invece l'RPC
locale KDF; può coinvolgere attese interne a KDF e non va attribuito direttamente
a MEXC. Non erano disponibili dati storici di banda/accodamento per separare
le cause. In caso di rallentamento persistente rimane corretto sospendere
ordini non copribili invece di usare dati scaduti.

Caricare le modifiche riavviando agent e worker desktop, poi verificare
riconciliazione pronta, feed e saldi freschi prima di riattivare le strategie.
