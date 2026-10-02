# Configurazione generica di coin e mercati

ARRR, LTC e USDT-BEP20 sono i valori predefiniti del primo collaudo, non una
lista chiusa. Il servizio gestisce un asset base configurabile e uno o piu
mercati KDF che condividono quell'inventario.

Ogni mercato mantiene separate tre identita:

- il ticker dell'asset base KDF, per esempio `BTC`;
- il ticker quote KDF, per esempio `USDT-BEP20` o `BCH`;
- la rotta Spot MEXC usata per prezzo, profondita e copertura, per esempio
  `BTCUSDT`.

## Esempi

Configurazione ARRR attuale:

```text
KDF_MM_BASE_TICKER=ARRR
KDF_MM_KDF_QUOTE_TICKER=USDT-BEP20
KDF_MM_MARKETS=ARRR-USDT-BEP20,ARRR-LTC
KDF_MM_PAIR=ARRRUSDT
KDF_MM_MEXC_BASE_ASSET=ARRR
KDF_MM_MEXC_QUOTE_ASSET=USDT
```

Lo stesso servizio configurato per BTC:

```text
KDF_MM_BASE_TICKER=BTC
KDF_MM_KDF_QUOTE_TICKER=USDT-BEP20
KDF_MM_MARKETS=BTC-USDT-BEP20,BTC-BCH
KDF_MM_PAIR=BTCUSDT
KDF_MM_MEXC_BASE_ASSET=BTC
KDF_MM_MEXC_QUOTE_ASSET=USDT
```

Se `KDF_MM_MARKETS` non viene indicato, viene creato il solo mercato
`<BASE>-<QUOTE_PRIMARIA>`. L'eccezione e il profilo storico ARRR, che mantiene
anche `ARRR-LTC` come default per compatibilita.

Quando il ticker KDF della quote non coincide con quello MEXC, le rotte si
specificano con un oggetto JSON su una sola riga:

```text
KDF_MM_MEXC_QUOTE_SYMBOLS={"BTC-BEP20":"BTCUSDT","BCH":"BCHUSDT"}
```

Se una rotta non esiste o non e negoziabile su MEXC, il feed resta non
disponibile e il motore non pubblica la quota. Una coin puo quindi essere
attivabile e utilizzabile nel wallet KDF senza essere automaticamente adatta a
questa strategia di copertura.

## Attivazione con un solo comando

L'attivazione e indipendente dai mercati configurati. Nella pagina KDF della
TUI, il tasto `A` chiede un ticker del registro ufficiale: per esempio `ARRR`,
`BTC`, `BCH`, `DASH` o `USDC-BEP20`. Il servizio sceglie l'attivatore dal
protocollo; per un token EVM attiva anche la piattaforma richiesta, oppure il
solo token se la piattaforma e gia attiva.

Il comando e idempotente: una coin gia abilitata viene segnalata come tale e
non viene riattivata. `S` aggiorna i task asincroni.

## Profilo persistente

Il tasto `V` salva nel file indicato da `KDF_MM_COIN_PROFILE` l'insieme dei
ticker attivi che il servizio sa riattivare. Eventuali protocolli non ancora
gestiti vengono elencati come esclusi, invece di produrre un profilo che
fallirebbe al successivo avvio. `P` riattiva in blocco lo stesso profilo. Il
file contiene solo ticker pubblici, mai seed, password o chiavi API, ed e
scritto con permessi privati e sostituzione atomica.

Il registro fissato contiene 782 definizioni ufficiali. Il comando automatico
accetta le coin `mm2=1`, non `wallet_only`, dei protocolli per cui esiste un
handler verificato: attualmente 699 ticker UTXO, ZHTLC ed EVM/ERC20. Gli altri
protocolli vengono rifiutati con un messaggio esplicito; non vengono trattati
come UTXO per approssimazione.

## Feed e ordini

I feed pubblici vengono sottoscritti solo per i mercati configurati le cui coin
risultano abilitate da `get_enabled_coins`. Un ordine o swap posseduto ancora
aperto mantiene la propria rotta attiva, cosi controllo, copertura e recupero
continuano anche dopo una disconnessione temporanea.

Un processo gestisce un solo asset base e piu quote che condividono lo stesso
pool. Questa scelta mantiene coerenti limite di inventario, copertura MEXC e
ledger. Per eseguire contemporaneamente strategie con basi indipendenti si
avviano servizi separati, con porte, database e profili distinti.

I nomi storici `SELL_ARRR`, `BUY_ARRR` e alcuni campi `arrr_*` restano nello
schema persistente per leggere journal e database gia creati. Nell'interfaccia
e nei nuovi eventi sono accompagnati dai campi neutrali `base_ticker` e
`base_quantity`; non limitano l'asset selezionato.
