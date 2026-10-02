# Copertura automatica e interblocco fondi

## Risultato operativo

Il Desktop Agent puo ora coprire automaticamente su MEXC uno swap KDF quando
riceve l'evento firmato `MakerPaymentSent`. Se il maker vende una unita
dell'asset base su KDF, il Desktop Agent prova a comprare esattamente una unita
sul simbolo MEXC configurato. Se il maker compra l'asset base, la copertura e
la vendita della stessa quantita.

L'ordine non e `MARKET`: e una `LIMIT` aggressiva calcolata sul book corrente e
vincolata da `KDF_MM_MAX_SLIPPAGE`. In questo modo tenta l'esecuzione immediata
senza accettare un prezzo illimitato.

## Sequenza della copertura

1. La VPS salva e firma l'evento KDF prima di consegnarlo.
2. Zorin verifica e salva l'evento nel journal locale.
3. Il motore legge regole, profondita e saldi MEXC Spot.
4. La stessa richiesta passa prima da `/api/v3/order/test`.
5. Quantita, prezzo e `clientOrderId` vengono salvati prima dell'invio reale.
6. L'ordine `LIMIT` reale viene inviato una sola volta e poi interrogato.
7. Un residuo aperto viene annullato; soltanto la quantita mancante riceve un
   nuovo prezzo e un nuovo ID deterministico.
8. Dopo il numero massimo di tentativi, il ciclo passa a `FAILED` e richiede
   intervento umano.

Se una risposta si perde dopo l'invio, lo stato diventa `UNKNOWN`. Al ciclo o
riavvio successivo viene interrogato lo stesso `clientOrderId`: non viene mai
inviato un duplicato alla cieca.

## Interblocco preventivo degli ordini KDF

Il Desktop Agent invia alla VPS soltanto i saldi liberi degli asset MEXC
configurati. Il messaggio e firmato e scade dopo dieci secondi. Non contiene
chiavi API e dichiara separatamente se l'esecutore reale e attivo.

La VPS calcola il caso prudente in cui tutti gli ordini KDF posseduti vengano
presi:

- per ordini KDF che vendono l'asset base, riserva sul CEX la valuta quote
  necessaria a ricomprarlo, inclusi limite di prezzo e buffer commissione;
- per ordini KDF che comprano l'asset base, riserva sul CEX la quantita base
  necessaria a rivenderlo, incluso il buffer commissione;
- per ordini su piu coppie somma i fabbisogni: non presume che la cancellazione
  OCO riesca prima di un secondo match.

Se prova, saldo, profondita o esecutore mancano, la VPS:

- rifiuta nuove pubblicazioni e variazioni;
- cancella soltanto gli ordini KDF ancora aperti e appartenenti al bot;
- mette il repricing in pausa;
- mostra `MERCATO BLOCCATO` in TUI e GUI.

Non e tecnicamente possibile fermare con questo meccanismo uno swap atomic gia
iniziato. La copertura di un evento gia salvato continua anche se la VPS o il
suo ACK sono temporaneamente irraggiungibili.

## Attivazione: interruttori distinti

Gli esempi distribuiti mantengono tutti gli interruttori su `false`.

- `KDF_MM_MEXC_COVERAGE=true` abilita la lettura e l'invio firmato dei saldi,
  ma non autorizza ordini MEXC e quindi non sblocca ordini KDF reali;
- `KDF_MM_AUTO_HEDGE=true` abilita il motore automatico;
- `KDF_MM_LIVE_TRADING=true` e il secondo consenso indipendente richiesto dal
  client MEXC;
- `KDF_MM_KDF_ORDER_WRITES=true` abilita le mutazioni ordine sulla VPS e rende
  obbligatorio il permesso fresco proveniente dal Desktop Agent.

`KDF_MM_LIVE_TRANSFERS` rimane indipendente e non serve alla copertura. Va
lasciato `false`.

## Forzatura manuale

Nella pagina TUI **Eventi e coperture**, `O` apre la forzatura. Occorre digitare
esattamente `FORZA COPERTURA`. Dura al massimo cinque minuti, non sopravvive a
un riavvio e viene registrata nel database di audit con permessi `0600`.

Durante la forzatura la TUI mostra in rosso `FORZATURA ATTIVA` e i secondi
rimanenti. `N` la termina subito. La forzatura ignora il controllo preventivo:
non crea fondi e non garantisce che MEXC possa coprire uno swap.

## Esposizione incerta e ripristino

Uno stato `REVIEW_REQUIRED` o `FAILED` ferma le nuove coperture e lascia scadere
il permesso VPS. Prima si deve verificare su MEXC l'ordine tramite UUID,
`clientOrderId`, fill e saldo, quindi correggere manualmente l'esposizione.
Soltanto dopo si registra la risoluzione:

```bash
PYTHONPATH=src python3 -m kdf_mm hedge-resolve \
  --swap-uuid UUID_DELLO_SWAP \
  --note "descrizione precisa della verifica e della correzione" \
  --confirmation "ESPOSIZIONE RISOLTA"
```

Il comando non invia ordini: annota soltanto la verifica umana e consente al
ciclo successivo di rinnovare il permesso.

## Stato del collaudo

I test automatici coprono quantita esatta, riempimento parziale, nuovo ID per
il residuo, invio incerto, recupero senza duplicati, limite dei tentativi,
lease alterata/scaduta, saldo insufficiente, aggregazione multi-coppia e
override. Nessun ordine MEXC reale e stato inviato durante questa
implementazione. Il passo successivo sicuro e un canary locale esplicitamente
autorizzato, con un solo ordine KDF e importo minimo compatibile con MEXC.
