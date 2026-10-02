# MEXC nella TUI

Dal menu principale aprire **[8] MEXC: saldi e riequilibrio**.
Le credenziali provengono dal portachiavi Linux; `tui --mexc-profile default`
seleziona il profilo (default se omesso). Non inserire le chiavi nei file di configurazione.

- **B — Saldi**: disponibile, impegnato e totale per tutte le coin con saldo Spot positivo.
  Non sono inclusi Futures o bonus. L'orario della lettura è visibile; B rilegge i saldi.
- **M — MEXC rebalance**: calcola i target e gli ordini suggeriti.
- **K — MEXC - KDF rebalance**: propone trasferimenti MEXC → KDF, esclusivamente manuali.
- **E — Conferma primo ordine**: apre `[Indietro] [Esegui]`, con Indietro preselezionato.
- **R — Stato ordini**: rilegge gli esiti degli ordini rebalance precedentemente inviati.
- Frecce verticali scorrono i testi lunghi. Esc chiude prima l'eventuale conferma;
  dalle analisi o dallo stato ordini torna ai saldi della pagina [8] MEXC,
  conservando la lettura e lo scorrimento precedenti. Solo da [8] torna al menu
  principale. Q non chiude dalle sottopagine.

## Obiettivo confermato: copertura ordini + 20%

Si considerano le strategie configurate, comprese quelle in pausa, escluse quelle eliminate
o con budget esaurito. Le quantità fisse rispettano il budget residuo; quelle automatiche
sono stimate con le regole di liquidità del motore (metà profondità entro l'1%, limiti
utente e volume giornaliero). Il target è per il prossimo insieme di ordini, non per
un numero illimitato di rifornimenti futuri.

Il prezzo e le due gambe hedge delle coppie cross derivano dai book asset/USDT.
Su MEXC le necessità di tutte le gambe si sommano e ricevono un margine del 20%,
oltre a commissioni e riserva di prezzo. Su KDF i livelli della stessa coppia si
sommano; coppie alternative che vendono la stessa coin condividono il fondo,
quindi si usa il massimo fra i totali delle coppie, non la somma di tutti i mercati.

Non si mira a una divisione 50/50. Gli asset estranei alle strategie non vengono venduti.
Le vendite riguardano solo eccedenze rispetto al target, quando servono USDT per la
copertura. Gli acquisti non anticipano mai i ricavi di vendite ancora da eseguire.
Se mancano fondi o le quantità sono sotto il minimo MEXC, viene mostrato un avviso.
USDT è l'unità di valorizzazione: non viene garantita la parità con USD.

## Eseguire un rebalance MEXC

1. Mettere manualmente in pausa tutte le strategie e il repricing. Attendere la
   conclusione di swap e hedge e la loro riconciliazione. Non devono restare ordini KDF.
2. Fermare il worker MEXC di hedging. Il servizio Agent/KDF deve restare disponibile
   per le verifiche: se il worker era integrato con `--with-mexc`, occorre usare una
   sessione del servizio senza tale opzione, dopo l'arresto controllato precedente.
3. TUI e Agent devono usare lo **stesso journal Desktop** e il medesimo account/profilo
   MEXC. L'esecuzione è supportata soltanto per Agent locale, non per una VPS.
   Per inviare ordini, la TUI deve avere `KDF_MM_LIVE_TRADING=true`; in prova può solo proporli.
4. Premere M, controllare le proposte, poi E. La conferma autorizza **solo il primo
   ordine mostrato** con quantità e prezzo limite esatti. Non una sequenza autonoma.
5. Premere R e verificare l'esito. Dopo un'esecuzione completa, ricalcolare con M
   prima dell'eventuale ordine successivo. Infine riavviare il worker e verificare
   la nuova copertura prima di riprendere i mercati KDF.

Sono ordini LIMIT: il limite impedisce di inseguire il prezzo oltre l'1% del book
osservato, ma non garantisce l'esecuzione. Possono restare aperti o parziali.
La proposta scade dopo 120 secondi e viene ricalcolata prima dell'invio. Cambiamenti
di prezzo, quantità, saldo o strategie richiedono una nuova conferma. Sono controllati
anche regole del simbolo, autorizzazioni API, commissioni effettive e altri ordini MEXC aperti.

L'ID viene salvato **prima** dell'invio in `<desktop-journal>.rebalance.sqlite3`.
Un timeout non provoca reinvii: l'ordine rimane da verificare. Finché il suo esito
non è terminale, nuove pubblicazioni KDF e l'avvio del worker hedge sono bloccati.
Pausa, cancellazione e verifiche di sicurezza rimangono disponibili.
Gli ordini rimasti aperti possono essere cancellati manualmente su MEXC; poi usare R.
Se MEXC non trova un ID con invio incerto, il blocco resta: serve un controllo manuale
degli ordini e dei trade, non cancellare il journal per aggirarlo.

## Trasferimenti manuali

K mostra la quantità netta mancante su KDF e quella trasferibile da MEXC senza usare
la riserva di copertura +20%. La disponibilità MEXC condivisa fra reti diverse non
viene contata due volte. Un saldo KDF non leggibile è dichiarato indisponibile,
mai interpretato come zero. Si usa il volume maker spendibile KDF, che tiene conto
dei vincoli del wallet, non il saldo lordo.

L'analisi è consultabile anche con strategie abilitate e nessun ordine aperto.
In tal caso vengono mostrati i target e il motivo del blocco, con identificativo
e stato della strategia, anziché interrompere la pagina con un errore generico.
Gli importi trasferibili vengono suggeriti solo quando i mercati sono in pausa,
senza swap/hedge da regolare; se MEXC ha ancora deficit di copertura, prima si
completa il rebalance Spot. Non si suggerisce di trasferire un'eccedenza in una
coin quando potrebbe servire a coprire un deficit in un'altra.

L'app **non effettua prelievi**, non sceglie la rete e non certifica un indirizzo.
Verificare manualmente ticker/rete (es. USDT BEP20), indirizzo, eventuale memo,
commissione, minimo di prelievo e fondi gas. Le quantità proposte sono al netto
delle necessità di copertura, ma non delle commissioni di trasferimento ancora
da verificare. Non eseguire insieme una vecchia proposta di ordini e una di
trasferimento: dopo ogni operazione ricalcolare con i nuovi saldi.

Le quotazioni legacy del vecchio repricer devono essere rimosse o convertite in
strategie prima di usare il pianificatore. La funzione non sostituisce i controlli
di copertura live, né assicura liquidità futura.

Riferimento API: [documentazione ufficiale MEXC Spot](https://mexcdevelop.github.io/apidocs/spot_v3_en/),
consultata per ordini LIMIT, clientOrderId, interrogazione dello stato e commissioni per simbolo.

## Verifiche di sviluppo

Test con API simulate: target e riserva, coppie cross, livelli e fondi condivisi,
quantità automatiche, saldi non disponibili, conto insufficiente, commissioni,
conferma singola, proposta scaduta, variazione prezzi, lock concorrenti,
timeout persistente e riconciliazione senza reinvio. Test TUI su pseudoterminale
con default sicuro, invii ripetuti, resize e ripristino del terminale.
Nessun test di questa funzione richiede credenziali o fondi reali.
