# Gate Spot per strategie KDF

Ogni strategia sceglie un solo CEX (`MEXC` oppure `GATE`). La scelta viene
salvata nella strategia e copiata nell'evento hedge firmato; uno swap già
iniziato non cambia venue se la configurazione viene modificata in seguito.
I livelli dello stesso mercato KDF, compreso l'eventuale lato opposto, devono
usare la stessa venue; per cambiarla si mettono in pausa e si ricreano i
livelli del mercato.

## Credenziali

Creare su Gate una chiave con lettura Spot e trading Spot, senza permessi di
prelievo. Salvarla nel portachiavi locale:

```text
PYTHONPATH=src python3 -m kdf_mm gate-keyring set
PYTHONPATH=src python3 -m kdf_mm gate-keyring status
```

La chiave Gate usa lo stesso profilo locale scelto per il worker. Se le
credenziali Gate non sono presenti, il worker continua a gestire MEXC ma una
strategia Gate resta bloccata prima della pubblicazione.

## Commissione

`KDF_MM_GATE_TAKER_FEE` imposta la commissione taker usata nel prezzo, nel
dimensionamento e nella riserva (default `0.001`, cioè 0,10%). Va allineata
alla commissione effettiva dell'account Gate restituita dall'endpoint privato
`/wallet/fee`.

La base API predefinita è `https://api.gateio.ws/api/v4` e può essere
sostituita con `GATE_BASE_URL`.
