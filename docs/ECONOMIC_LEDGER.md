# Ledger economico e P/L dei cicli

## Obiettivo

Il ledger locale collega tre fonti persistenti usando lo `swap_uuid`:

- termini economici dello swap KDF ricevuti in un evento firmato;
- esito finale dello swap KDF ricevuto in un secondo evento firmato;
- quantita e controvalore realmente eseguiti dalla copertura MEXC.

Il risultato appare nella scheda `Risultati` della GUI. La lettura della GUI
non modifica il database e non puo inviare ordini o trasferimenti.

## Due eventi distinti

`HEDGE_REQUIRED` continua a essere prodotto soltanto dopo
`MakerPaymentSent`. Serve per prenotare la copertura senza attendere la fine
dello swap.

`KDF_SWAP_OUTCOME` viene prodotto quando la riconciliazione KDF vede lo swap
terminato. Contiene esito riuscito/fallito, evento terminale e timestamp. I due
eventi sono firmati, idempotenti e confermati separatamente dal Desktop Agent.

Il ledger non classifica mai come realizzato un margine basato sul solo primo
evento.

## Calcolo ARRR-USDT-BEP20

Per una vendita ARRR su KDF seguita da un acquisto ARRR su MEXC:

```text
P/L lordo = USDT ricevuti su KDF - USDT spesi su MEXC
```

Per un acquisto ARRR su KDF seguito da una vendita ARRR su MEXC:

```text
P/L lordo = USDT ricevuti su MEXC - USDT spesi su KDF
```

Il P/L diventa `REALIZED` soltanto se l'esito KDF e noto e il residuo ARRR del
ciclo e zero. Le commissioni note in USDT vengono sottratte dal lordo. Ogni
commissione e registrata con una chiave idempotente, sede, asset, importo,
fonte e timestamp opzionale.

Il prezzo medio MEXC usa i totali effettivi:

```text
prezzo medio = controvalore eseguito / ARRR eseguiti
```

## Esposizioni residue

Se lo swap KDF fallisce dopo che una copertura MEXC e stata eseguita, oppure la
copertura e solo parziale, il ciclo viene indicato come `OPEN_EXPOSURE`. Quando
e disponibile il midpoint pubblico `ARRRUSDT`, il residuo viene valorizzato e
mostrato come P/L non realizzato.

Senza prezzo corrente il valore resta non disponibile. Non viene sostituito
con zero.

## Commissioni e completezza

Le commissioni USDT e USDT-BEP20 sono direttamente confrontabili. Una
commissione in ARRR modifica il residuo ARRR. Commissioni in BNB, LTC o altri
asset restano visibili ma non vengono convertite senza un prezzo esplicito; in
questo caso il totale viene marcato come incompleto.

Le commissioni MEXC vengono ora importate dal dettaglio dei trade del conto e
registrate con una chiave derivata da simbolo e ID del trade. Un secondo import
dello stesso ordine non duplica ne fill ne commissioni. La modalita MEXC
`/api/v3/order/test` non produce fill o commissioni reali.

Il comando dedicato e:

```bash
kdf-mm mexc-import-fills \
  --swap-uuid UUID_DELLO_SWAP \
  --journal runtime/PERCORSO/desktop-agent.sqlite3
```

Prima di scrivere nel journal, il comando verifica ordine terminale, simbolo,
`clientOrderId`, `orderId`, lato, somma delle quantita e somma del controvalore.
Usa soltanto richieste MEXC autenticate di lettura. Se la risposta arriva al
limite di 100 trade o non coincide con i totali dell'ordine, l'importazione
viene rifiutata. MEXC rende disponibili con questo endpoint solo i trade
dell'ultimo mese, quindi un ordine piu vecchio non viene dichiarato completo.
Un ciclo che possiede quantita MEXC aggregate ma non i relativi fill verificati
puo mostrare il dettaglio provvisorio, ma rende incompleti i totali del ledger.

## Baseline dell'inventario ARRR

Il costo iniziale degli ARRR gia posseduti puo essere registrato manualmente:

```bash
kdf-mm inventory-baseline \
  --baseline-key apertura-2026-09-10 \
  --quantity QUANTITA_ARRR_TOTALE \
  --total-cost-usdt COSTO_STORICO_TOTALE_USDT \
  --note "Inventario KDF e MEXC verificato" \
  --journal runtime/PERCORSO/desktop-agent.sqlite3
```

`--total-cost-usdt` e il costo storico totale, non il prezzo unitario. La
chiave rende la scrittura idempotente e immutabile. Per evitare una stima
ingannevole, il P/L dell'inventario appare solo quando il saldo ARRR corrente
letto dalla GUI coincide con la quantita calcolata e il prezzo corrente e
disponibile.

Il costo viene ora portato avanti automaticamente attraverso gli swap conclusi
con fill verificati e le commissioni ARRR/USDT. Ingressi e uscite esterni si
registrano con `kdf-mm inventory-adjustment`; i trasferimenti interni KDF/MEXC
non cambiano l'inventario complessivo e non vanno registrati. Metodo, formule e
barriere sono descritti in
[Roll-forward del costo inventario](INVENTORY_COST_ROLL_FORWARD.md).

## Limiti intenzionali

- una baseline inventario deve essere inserita dall'utente con quantita e costo
  storico verificati;
- il P/L dell'intero portafoglio non viene dedotto dai soli saldi;
- i cicli `ARRR-LTC` richiedono anche la conversione LTC/USDT del momento;
- nessuna commissione assente viene stimata;
- nessun ciclo pendente viene contato come profitto;
- nessun dato del ledger abilita automaticamente trading o trasferimenti.

Il prossimo ampliamento sara la conversione storica degli swap ARRR-LTC e la
registrazione assistita dei movimenti, seguite dalla copertura MEXC live
limitata a un canary con conferma esplicita.
