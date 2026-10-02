# Importazione verificata dei fill MEXC

## Cosa fa

L'importatore completa il journal economico dopo un vero ordine di copertura
MEXC. Recupera prima l'ordine tramite il `clientOrderId` deterministico gia
associato allo swap, poi legge i trade del conto filtrati per `orderId`.

Vengono salvati:

- ogni fill con ID trade, prezzo, quantita ARRR e controvalore USDT;
- commissione e asset in cui e stata addebitata;
- stato terminale e totali effettivi dell'ordine.

## Barriere di sicurezza

- il client MEXC viene costruito con trading e trasferimenti disabilitati;
- sono usati soltanto sincronizzazione oraria e endpoint autenticati `GET`;
- simbolo, lato e identificativi devono coincidere con il journal;
- i fill devono riconciliare esattamente i totali terminali dell'ordine;
- una pagina da 100 trade viene rifiutata perche potrebbe essere troncata;
- fill gia presenti ma assenti o diversi nella risposta MEXC causano un errore;
- la seconda esecuzione e idempotente.

La documentazione MEXC Spot v3 indica per `GET /api/v3/myTrades` il permesso
`SPOT_ACCOUNT_READ`, il limite massimo 100 e la disponibilita dei soli trade
dell'ultimo mese:
<https://mexcdevelop.github.io/apidocs/spot_v3_en/#account-trade-list>

## Uso

Il comando va eseguito soltanto per uno swap che possiede nel journal un vero
tentativo MEXC e un ordine terminale:

```bash
kdf-mm mexc-import-fills \
  --swap-uuid UUID_DELLO_SWAP \
  --sequence 1 \
  --profile default \
  --journal runtime/PERCORSO/desktop-agent.sqlite3
```

Una validazione fatta con `/api/v3/order/test` non e importabile: MEXC non crea
un ordine reale, un fill o una commissione. Il primo collaudo end-to-end dovra
quindi essere associato al futuro canary live minimo e richiedera autorizzazione
esplicita prima dell'invio.
