# Mercati multipli e inventario condiviso

## Configurazione di esempio

La configurazione di esempio gestisce due mercati KDF:

- `ARRR-USDT-BEP20`, prezzato direttamente con `ARRRUSDT` su MEXC;
- `ARRR-LTC`, prezzato in modo incrociato con `ARRRUSDT` e `LTCUSDT`.

La lista e configurabile con:

```text
KDF_MM_MARKETS=ARRR-USDT-BEP20,ARRR-LTC
```

`LTC` non e codificato nel motore. Per esempio `ARRR-DOGE` usa automaticamente
`ARRRUSDT` e `DOGEUSDT`. I dettagli e i ticker wrapped sono descritti in
[Configurazione generica dei mercati](MARKET_CONFIGURATION.md).

Per ogni mercato incrociato il prezzo SELL usa il lato ask di ARRR/USDT e il bid
della quote/USDT; il prezzo BUY usa il bid di ARRR/USDT e l'ask della
quote/USDT. Il cap considera la
profondita di entrambi i book e il calcolo include due commissioni CEX, perche
la copertura completa richiede due gambe.

## Pool di fondi

Ogni ordine maker spende la coin `base` di KDF:

| Quotazione | Ordine KDF | Pool |
|---|---|---|
| SELL ARRR/USDT-BEP20 | `ARRR -> USDT-BEP20` | `ARRR` |
| SELL ARRR/LTC | `ARRR -> LTC` | `ARRR` |
| BUY ARRR/USDT-BEP20 | `USDT-BEP20 -> ARRR` | `USDT-BEP20` |
| BUY ARRR/LTC | `LTC -> ARRR` | `LTC` |

Il servizio somma tutti i volumi aperti che spendono lo stesso pool. Se KDF
indica `max_maker_vol = 10 ARRR`, la somma delle quote SELL sui due mercati non
puo superare `10 ARRR`: sono validi, per esempio, `5 + 5`, ma non `10 + 10`.
Questa regola e stata introdotta dopo che il test finanziato ha dimostrato che
due match contemporanei possono essere accettati prima della cancellazione
OCO.

`GET /v1/inventory` mostra per ciascun pool il saldo, `max_maker_vol`, la parte
bloccata dagli swap, il totale pubblicizzato, la capacita non ancora
pubblicizzata e gli UUID aperti. Prima di ogni pubblicazione o aggiornamento il
servizio rifiuta l'operazione se il nuovo totale supererebbe il valore corrente
di `max_maker_vol`.

## OCO applicato dal servizio

KDF non offre un OCO atomico fra coppie diverse. Per questo il servizio applica
la seguente regola:

1. l'evento KDF sveglia immediatamente la riconciliazione;
2. lo swap viene salvato nel database prima di qualsiasi altra azione;
3. il pool interessato viene bloccato per tutti i mercati;
4. vengono cancellati uno per uno soltanto gli altri UUID posseduti che
   spendono lo stesso pool;
5. il repricing non li ricrea finche lo swap non e terminato, verificato e
   confermato manualmente.

Se KDF ha gia cancellato un ordine fratello con motivo `InsufficientBalance`,
il servizio legge `order_status` e registra questo esito come terminale atteso.
Se invece un secondo ordine risulta gia in matching, la cancellazione puo
fallire: la riconciliazione va in errore e il repricing si mette in pausa. E il
comportamento corretto perche nessun OCO gestito fuori da KDF puo eliminare
completamente la finestra di gara fra due match contemporanei.

## Eventi e recupero

Gli eventi SSE KDF sono un acceleratore, non l'unica fonte di verita. Il polling
persistente di `my_orders`, `active_swaps`, `my_recent_swaps`, `my_swap_status`
e `order_status` rimane sempre attivo e ricostruisce lo stato dopo interruzioni
o riavvii.

Per abilitare SSE, la configurazione `MM2.json` deve contenere:

```json
"event_streaming_configuration": {
  "access_control_allow_origin": "http://127.0.0.1"
}
```

e l'Agent deve avere:

```text
KDF_MM_KDF_EVENT_STREAM=true
KDF_MM_KDF_EVENT_STREAM_CLIENT_ID=4107
```

Lasciare l'opzione `false` usa soltanto il polling ogni due secondi. Tutte le
scritture ordine restano comunque disabilitate finche
`KDF_MM_KDF_ORDER_WRITES=false`.

## Risultato del test finanziato e limite operativo

Il test finanziato multi-coppia e stato completato il 9-10 settembre 2026. Due
richieste concorrenti sono state entrambe accettate da KDF. Una si e regolata;
l'altra e terminata prima del pagamento principale del Taker perche la rete
ARRR ha rifiutato la seconda transazione Maker in conflitto. I fondi principali
sono rimasti al sicuro, ma il Taker ha pagato la commissione DEX dello swap
abortito.

Il meccanismo OCO riduce la finestra di esposizione, ma non trasforma KDF in un
OCO atomico. Il profilo operativo usa quindi sempre il limite aggregato del
pool. Per dati, UUID e saldi vedere
[Test finanziato multi-coppia ARRR](ARRR_MULTI_PAIR_FUNDED_TEST.md).
