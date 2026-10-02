# Prova locale di Supervisor, MEXC e TUI

Per la nuova procedura guidata, i budget e il worker MEXC integrato vedere
[Strategie locali](LOCAL_STRATEGIES.md). La GUI e la VPS sono opzionali.
Le istruzioni qui sotto restano valide per i controlli precedenti in simulazione.

Questa prova usa una sola configurazione KDF e nessuna chiave CEX. I feed MEXC
necessari vengono avviati in base alle coin KDF attivate. ARRR, LTC e
USDT-BEP20 sono soltanto i valori del profilo di esempio. Gli ordini restano
disabilitati.

## 1. Preparazione

Dalla cartella principale del progetto:

```bash
chmod 600 runtime/local-funded-test/maker/MM2.json
mkdir -p runtime/local-service
chmod 700 runtime/local-service
```

Usare tre segreti lunghi e differenti. I valori qui sotto sono solo esempi per
un test limitato a `127.0.0.1`:

```bash
export KDF_MM_AGENT_TOKEN='local-agent-token-change-this-2026'
export KDF_MM_SNAPSHOT_SECRET='local-feed-secret-change-this-2026'
export KDF_MM_EVENT_SECRET='local-event-secret-change-this-2026'
export KDF_MM_STATE_DB='runtime/local-service/agent.sqlite3'
export KDF_MM_OUTBOX_DB='runtime/local-service/outbox.sqlite3'
export KDF_MM_KDF_SUPERVISOR_STATE='runtime/local-service/supervisor.json'
export KDF_MM_KDF_LOG_PATH='runtime/local-service/kdf.log'
export KDF_MM_COIN_PROFILE='runtime/local-service/coin-profile.json'
export KDF_MM_KDF_ORDER_WRITES='false'
export KDF_MM_BASE_TICKER='ARRR'
export KDF_MM_KDF_QUOTE_TICKER='USDT-BEP20'
export KDF_MM_PAIR='ARRRUSDT'
export KDF_MM_MEXC_BASE_ASSET='ARRR'
export KDF_MM_MEXC_QUOTE_ASSET='USDT'
export KDF_MM_MARKETS='ARRR-USDT-BEP20,ARRR-LTC'
```

## 2. Avvio del servizio

Nello stesso terminale:

```bash
PYTHONPATH=src python3 -m kdf_mm local-service \
  --config runtime/local-funded-test/maker/MM2.json
```

Il servizio si mette in ascolto soltanto su `127.0.0.1:8765`. KDF non parte
ancora: si avvia dalla TUI con il tasto `K`.

## 3. Avvio della TUI

Aprire un secondo terminale nella cartella del progetto e impostare lo stesso
token:

```bash
export KDF_MM_AGENT_TOKEN='local-agent-token-change-this-2026'
PYTHONPATH=src python3 -m kdf_mm tui
```

La TUI apre un menu numerato, ispirato al flusso semplice di pytomicDEX. Ogni
pagina mostra una sola attivita: KDF, portafoglio, quotazione, ordini,
repricing, swap oppure eventi. Si puo scegliere con `↑`/`↓` e `Invio`, oppure
premendo direttamente un numero da `0` a `7`. `Esc` torna sempre al menu.

Nella TUI:

1. aprire `[0] Stato KDF e attivazione coin`, quindi premere `K` per avviare KDF;
2. attendere che compaiano `RUNNING` e `RPC: OK`;
3. nella stessa pagina premere `A`, inserire un ticker come `ARRR`, `BTC`,
   `BCH`, `DASH` o `USDC-BEP20` e confermare; le dipendenze EVM come `BNB`
   vengono incluse automaticamente;
4. ripetere `A` per le altre coin e premere `S` per aggiornare le attivazioni
   asincrone;
5. premere `V` per salvare tutte le coin attualmente attive nel profilo locale;
   ai prossimi avvii bastera premere `P` per riattivare l'intero profilo;
6. aprire `[2] Mercato e nuova quotazione`, premere `V` per i comandi precedenti, selezionare mercato con `←`/`→` e
   lato con `↑`/`↓`, poi impostare quantita e premium con `N` e `P`;
7. premere `Invio` (oppure `V`) per vedere la quotazione senza creare ordini.

Il profilo contiene soltanto ticker pubblici, mai seed, password o chiavi API.
Viene scritto con permessi privati e sostituzione atomica, così un arresto
improvviso non lascia un file parziale.

Per provare un altro asset base si cambiano le sei variabili di mercato. Per
esempio BTC usa `KDF_MM_BASE_TICKER=BTC`, `KDF_MM_PAIR=BTCUSDT` e mercati come
`BTC-USDT-BEP20,BTC-BCH`. I ticker da attivare continuano a essere scelti con
`A`; il profilo salvato non modifica automaticamente la strategia configurata.

Il piè di pagina cambia con la pagina e mostra soltanto i comandi utilizzabili
in quel momento. Premere `?` per la guida completa e `Tab` per aprire o
chiudere rapidamente Eventi/coperture. Gli stati
usano sia colore sia marcatori testuali come `[OK]`, quindi restano leggibili
anche con `NO_COLOR=1` o su terminali senza colori.

Per provare il ciclo automatico senza scritture reali:

1. aprire `[4] Repricing automatico` e premere `E` per configurare il lato selezionato;
2. usare `T` per la soglia minima e `F` per l'intervallo minimo;
3. premere `G` e confermare per avviare il ciclo;
4. osservare `SIMULATION_PUBLISH` o `SIMULATION_UPDATE` nella riga Repricing;
5. premere `H` per la pausa immediata.

La riga `Controllo KDF` mostra anche swap attivi, risultati da confermare e
ordini anomali. `J` forza una rilettura immediata. `Y` sblocca il lato
selezionato soltanto dopo una verifica manuale: non esegue coperture MEXC e non
sposta fondi. In simulazione si puo osservare il controllo, ma non vengono
creati swap.

I tasti `O`, `M` e `C` sono predisposti per pubblicare, aggiornare e cancellare
ordini, ma le scritture vengono rifiutate finche
`KDF_MM_KDF_ORDER_WRITES=false`.

## 4. Arresto

Dal menu principale premere `Q` per chiudere la TUI. Nel primo terminale usare
`Ctrl+C`: il servizio
arresta soltanto la propria istanza KDF, chiude il feed MEXC e conserva il
registro locale e l'outbox degli eventi. In alternativa, `X` arresta KDF
lasciando attivo il servizio.

Non serve usare i fondi durante questa prova: indirizzi e saldi sono soltanto
letti e mostrati.
