# Roll-forward del costo inventario ARRR

## Scopo

Il roll-forward mantiene il costo medio dell'inventario ARRR senza chiedere una
nuova baseline dopo ogni swap. Il calcolo parte dall'ultima baseline e applica,
in ordine temporale:

- swap KDF conclusi;
- fill MEXC verificati e relative commissioni;
- ingressi e uscite ARRR esterni al perimetro KDF/MEXC.

Il risultato resta una proiezione di sola lettura. Non produce ordini o
trasferimenti e non modifica la baseline.

## Metodo contabile

Per un'acquisizione:

```text
nuova quantita = quantita precedente + quantita acquisita
nuovo costo = costo precedente + costo effettivo dell'acquisizione
```

Per una disposizione viene usato il costo medio ponderato:

```text
costo rimosso = quantita disposta * costo precedente / quantita precedente
```

Una vendita ARRR su KDF seguita da un acquisto MEXC rimuove quindi il costo
medio degli ARRR venduti e aggiunge il costo effettivo del riacquisto. Nel verso
opposto, la vendita MEXC viene applicata prima dell'acquisto KDF.

Le commissioni MEXC in USDT aumentano il costo di un acquisto. Le commissioni
in ARRR riducono la quantita netta acquisita o aumentano la quantita disposta.
Una commissione di acquisizione pagata in un asset senza valore storico USDT
blocca il roll-forward.

Il P/L operativo dei cicli e il P/L dell'inventario rimangono separati: non
devono essere sommati, perche gli stessi flussi di acquisto e vendita sono gia
riflessi nel nuovo costo dell'inventario.

## Condizioni di completezza

Il P/L inventario viene mostrato soltanto se:

- non esiste uno swap pendente successivo o sovrapposto alla baseline;
- ogni copertura MEXC eseguita ha fill verificati e riconciliati;
- gli swap KDF riusciti hanno un cambio storico quote/USDT firmato;
- nessun movimento manuale cade durante la finestra temporale di uno swap;
- la quantita calcolata coincide con il saldo ARRR corrente complessivo;
- il prezzo corrente ARRR/USDT e disponibile.

Per ogni swap incrociato il VPS salva nell'evento firmato il cambio storico
quote/USDT, con simbolo, lato eseguibile e timestamp. Il ledger usa lo stesso
meccanismo per LTC, DOGE, KMD o qualsiasi altra quote configurata; un evento
precedente privo del cambio continua a fermare il calcolo invece di usare una
stima corrente. In caso di dati ambigui viene richiesta una nuova baseline.

## Movimenti esterni

Un ingresso da un wallet esterno richiede quantita e costo storico totale:

```bash
kdf-mm inventory-adjustment \
  --adjustment-key ingresso-esterno-001 \
  --kind ACQUIRE \
  --quantity QUANTITA_ARRR \
  --total-cost-usdt COSTO_TOTALE_USDT \
  --note "Origine e riferimento verificabili" \
  --journal runtime/PERCORSO/desktop-agent.sqlite3
```

Un'uscita verso un wallet esterno non accetta un costo manuale: il costo da
rimuovere viene calcolato automaticamente con la media ponderata.

```bash
kdf-mm inventory-adjustment \
  --adjustment-key uscita-esterna-001 \
  --kind DISPOSE \
  --quantity QUANTITA_ARRR \
  --note "Destinazione e riferimento verificabili" \
  --journal runtime/PERCORSO/desktop-agent.sqlite3
```

Non registrare come movimento un trasferimento interno tra KDF e MEXC: entrambe
le sedi fanno gia parte dello stesso inventario totale. Durante un trasferimento
il saldo libero puo non coincidere temporaneamente; in quel periodo il P/L resta
non disponibile senza alterare il costo.

Ogni `adjustment-key` e idempotente e immutabile. Per correggere un dato errato
si registra una nuova baseline verificata, senza sovrascrivere la cronologia.
