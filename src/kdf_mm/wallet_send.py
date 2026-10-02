"""Confirmed KDF wallet transfers. Signed bytes never leave the local service."""
import json
import os
import re
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from contextlib import contextmanager

from .rebalance_guard import rebalance_guard


def amount(value, label):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0:
            raise ValueError()
        return n
    except (ValueError, InvalidOperation):
        raise ValueError(f'{label} non valido: inserire un numero positivo, con punto decimale.') from None


class WalletSend:
    def __init__(self, controller, strategies, repricing, journal, *, enabled=False):
        self.c, self.strategies, self.repricing = controller, strategies, repricing
        self.enabled = enabled
        self.guard = str(Path(journal).resolve()) + '.rebalance.lock'
        self.path = str(Path(journal).resolve()) + '.wallet-send.sqlite3'
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS sends (id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL)')
        self.db.commit()
        self.lock = threading.RLock()
        self.lock_wait_seconds = 8.0

    def _save(self, rid, state, data):
        self.db.execute('INSERT OR REPLACE INTO sends VALUES (?,?,?)', (rid, state, json.dumps(data)))
        self.db.commit()

    def _row(self, rid):
        row = self.db.execute('SELECT * FROM sends WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise ValueError('Richiesta di invio non trovata: nessun esito disponibile. Controllare lo storico prima di ripetere.')
        return row['state'], json.loads(row['data'])

    def _public(self, rid, state, data):
        return {'id': rid, 'state': state, **{k: v for k, v in data.items()
            if k not in {'tx_hex', 'task_id'}}}

    def history(self):
        with self.lock:
            return {'sends': [self._public(r['id'], r['state'], json.loads(r['data']))
                             for r in self.db.execute('SELECT * FROM sends ORDER BY rowid DESC LIMIT 20')],
                    'enabled': self.enabled}

    @contextmanager
    def _exclusive(self):
        if not self.enabled:
            raise ValueError('Invii KDF disabilitati: avviare il servizio con KDF_MM_LIVE_TRANSFERS=true. Nessun trasferimento inviato.')
        with rebalance_guard(self.guard, exclusive=True, wait_seconds=self.lock_wait_seconds,
                busy_message='Attesa terminata: un controllo MEXC o un’altra operazione KDF occupa ancora il blocco di sicurezza. Questa richiesta non ha trasmesso fondi. Attendere il completamento e aggiornare lo stato prima di riprovare.'), self.strategies.lock, self.c._order_lock:
            from .rebalance_guard import assert_no_pending
            assert_no_pending(self.path.removesuffix('.wallet-send.sqlite3'))
            yield

    def _idle(self):
        rows = self.strategies.status()['strategies']
        blocked = [r['id'] for r in rows if r['enabled'] or r['state'] not in {'PAUSED', 'EXHAUSTED', 'DELETED'}]
        if blocked:
            raise ValueError('Fondi riservati al trading: mettere in pausa o risolvere le strategie ' + ', '.join(blocked) + '. Poi riprovare; nessun invio eseguito.')
        if self.repricing:
            state = self.repricing.payload()
            if state.get('state') not in {'PAUSED', 'DISABLED', 'STOPPED', 'IDLE'} or state.get('auto_resume', {}).get('eligible'):
                raise ValueError('Repricing ancora attivo o riavviabile: usare la pausa manuale prima di inviare fondi.')
        orders, swaps = self.c.kdf.my_orders(), self.c.kdf.active_swaps()
        if not isinstance(orders.get('maker_orders'), dict) or not isinstance(orders.get('taker_orders'), dict) or not isinstance(swaps.get('uuids'), list):
            raise ValueError('KDF non restituisce uno stato completo di ordini e swap: impossibile verificare i fondi impegnati. Attendere la connessione e riprovare.')
        if orders['maker_orders'] or orders['taker_orders']:
            raise ValueError('Ci sono ordini KDF aperti: i loro fondi sono esclusi dall’invio. Mettere in pausa i mercati e verificare che gli ordini siano ritirati.')
        if swaps['uuids']:
            raise ValueError('Fondi impegnati in swap KDF: attendere conclusione o rimborso prima di inviare.')
        if self.c.ownership.active():
            raise ValueError('Il registro locale contiene ordini non ancora riconciliati: aggiornare la riconciliazione prima di inviare.')

    def _funds(self, coin, required):
        b = self.c.kdf.balance(coin)
        if 'balance' not in b or 'unspendable_balance' not in b:
            raise ValueError(f'{coin}: KDF non indica il saldo non spendibile; impossibile escludere fondi bloccati. Attendere la sincronizzazione.')
        total, locked = amount(b['balance'], 'Saldo'), amount(b['unspendable_balance'], 'Saldo bloccato')
        available = max(Decimal(0), total - locked)
        if required > available:
            raise ValueError(f'{coin}: richiesti {required}, disponibili {available}, bloccati {locked}. Ridurre la quantità o rifornire il wallet; lasciare fondi per le commissioni.')
        return str(available)

    def prepare(self, payload):
        rid = str(payload.get('id', ''))
        if not re.fullmatch('[a-f0-9]{32}', rid):
            raise ValueError('Identificativo invio non valido: riaprire la schermata Invia.')
        coin, to = str(payload.get('coin', '')).upper(), str(payload.get('to', '')).strip()
        qty = amount(payload.get('amount'), 'Quantità')
        if qty <= 0 or not to or len(to) > 256 or any(c.isspace() or ord(c) < 32 for c in to):
            raise ValueError('Indirizzo o quantità non validi: incollare solo l’indirizzo, senza URI/spazi, e inserire una quantità maggiore di zero.')
        with self.lock:
            prior = self.db.execute('SELECT id FROM sends WHERE id=?', (rid,)).fetchone()
            if prior:
                state, data = self._row(rid)
                if (data['coin'], data['to'], data['amount']) != (coin, to, str(qty)):
                    raise ValueError('Questa richiesta appartiene a un altro importo/destinatario: non riutilizzare la conferma.')
                return self._public(rid, state, data)
            if self.db.execute("SELECT id FROM sends WHERE state IN ('PREPARING','READY','SUBMITTING','UNKNOWN')").fetchone():
                raise ValueError('Esiste un invio in preparazione o da verificare. Aprire lo storico; annullare solo le bozze non inviate. Non creare un duplicato.')
            with self._exclusive():
                if self.db.execute("SELECT id FROM sends WHERE state IN ('PREPARING','READY','SUBMITTING','UNKNOWN')").fetchone():
                    raise ValueError('Un altro invio è stato preparato nel frattempo: aprire lo storico prima di continuare.')
                self._idle()
                protocol = self.c.coin_registry.protocol_type(coin)
                if protocol not in {'UTXO', 'ZHTLC', 'ETH', 'ERC20'}:
                    raise ValueError(f'{coin}: protocollo {protocol} non ancora supportato dall’invio TUI; nessuna transazione creata.')
                if coin not in self.c.enabled_tickers():
                    raise ValueError(f'{coin} non attiva: attivarla e attendere la sincronizzazione prima di inviare.')
                data = {'coin': coin, 'to': to, 'amount': str(qty), 'protocol': protocol,
                        'network': ', '.join(self.c.coin_registry.dependency_tickers(coin)) or coin,
                        'available': self._funds(coin, qty), 'created': time.time()}
                self._save(rid, 'PREPARING', data)
                try:
                    params = {'coin': coin, 'to': to, 'amount': str(qty), 'max': False, 'broadcast': False}
                    if protocol == 'ZHTLC':
                        result = self.c.kdf.v2('task::withdraw::init', params)
                        data['task_id'] = int(result['task_id'])
                        self._save(rid, 'PREPARING', data)
                    else:
                        self._ready(rid, data, self.c.kdf.v2('withdraw', params))
                except Exception as exc:
                    self._failed(rid, data, exc)
                return self._public(rid, *self._row(rid))

    def _failed(self, rid, data, exc):
        # Never expose signed bytes, passwords or raw RPC request dumps.
        from .kdf import KdfError
        cause = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        if isinstance(exc, KdfError):
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            code = str(payload.get('error_type', ''))
            known = {'NotSufficientBalance': 'Saldo insufficiente dopo commissioni: ridurre la quantità',
                     'InsufficientBalance': 'Saldo insufficiente dopo commissioni: ridurre la quantità',
                     'InvalidAddress': 'Indirizzo non valido per questa coin: controllare destinatario e rete',
                     'NoSuchCoin': 'Coin non attiva: attivarla e attendere la sincronizzazione',
                     'Transport': 'Nodo esterno non raggiungibile: attendere il ripristino della connessione',
                     'CoinIsNotActivated': 'Coin non attiva: attivarla prima di preparare l’invio'}
            nested = json.dumps(payload).lower()
            code = next((key for key in known if key.lower() in nested), code)
            cause = known.get(code, f'KDF ha rifiutato la preparazione (tipo {code or "non specificato"}); controllare i log KDF per il dettaglio')
        elif isinstance(exc, (TimeoutError, OSError)):
            cause = 'Connessione a KDF interrotta o scaduta: ripristinare la connessione prima di preparare una nuova bozza'
        data['message'] = f'Preparazione non riuscita: {cause}. Nessun invio eseguito. Controllare indirizzo/rete, saldo spendibile e sincronizzazione KDF.'
        self._save(rid, 'FAILED', data)

    def _ready(self, rid, data, tx):
        if tx.get('coin') != data['coin'] or data['to'] not in tx.get('to', []):
            raise ValueError('Coin o destinatario della transazione KDF diversi dalla richiesta; preparazione rifiutata')
        raw, txid = tx.get('tx_hex', ''), tx.get('tx_hash', '')
        if not re.fullmatch('[a-fA-F0-9]+', raw) or len(raw) % 2 or not re.fullmatch('(0x)?[a-fA-F0-9]{64}', txid):
            raise ValueError('Transazione KDF incompleta: firma o identificativo mancanti')
        fees = tx.get('fee_details', {})
        fee_coin = fees.get('coin') or (data['coin'] if data['protocol'] in {'UTXO', 'ZHTLC'} else None)
        fee = amount(fees.get('total_fee', fees.get('amount')), 'Commissione KDF')
        if not fee_coin:
            raise ValueError('Coin della commissione non indicata da KDF: impossibile verificare il gas')
        qty = Decimal(data['amount'])
        # For UTXO the selected inputs may be larger: net debit includes change.
        debit = -Decimal(str(tx['my_balance_change']))
        expected = qty + (fee if fee_coin == data['coin'] else Decimal(0))
        if not debit.is_finite() or debit != expected:
            raise ValueError('Addebito KDF diverso da importo più commissione: invio a sé stessi o formato non supportato; nessuna trasmissione')
        self._funds(data['coin'], expected)
        if fee_coin != data['coin']:
            self._funds(fee_coin, fee)
        data.update(tx_hex=raw, tx_hash=txid, fee=str(fee), fee_coin=fee_coin,
                    expires=time.time() + 120, message='Pronta: controllare rete, destinatario e commissione prima di confermare.')
        self._save(rid, 'READY', data)

    def status(self, rid):
        with self.lock:
            state, data = self._row(rid)
            if state == 'PREPARING':
                if 'task_id' not in data:
                    self._failed(rid, data, ValueError('Preparazione interrotta prima della risposta KDF'))
                else:
                    try:
                        result = self.c.kdf.v2('task::withdraw::status', {'task_id': data['task_id'], 'forget_if_finished': False})
                        if result.get('status') == 'Ok':
                            self._ready(rid, data, result['details'])
                        elif result.get('status') in {'Error', 'Cancelled'}:
                            from .kdf import KdfError
                            self._failed(rid, data, KdfError('Preparazione rifiutata', payload=result.get('details', {})))
                    except ValueError as exc:
                        self._failed(rid, data, exc)
                    except Exception:
                        data['message'] = 'Stato della firma non disponibile: riprovare Aggiorna. La transazione non è stata trasmessa.'
                        self._save(rid, state, data)
            elif state in {'SUBMITTING', 'UNKNOWN', 'SENT'}:
                try:
                    history = (self.c.kdf.v2('z_coin_tx_history', {'coin': data['coin'], 'limit': 100})
                               if data['protocol'] == 'ZHTLC' else
                               self.c.kdf.legacy('my_tx_history', coin=data['coin'], limit=100))
                    target = data['tx_hash'].removeprefix('0x').lower()
                    matches = [tx for tx in history.get('transactions', [])
                               if str(tx.get('tx_hash', '')).removeprefix('0x').lower() == target
                               and int(tx.get('confirmations', 0)) > 0]
                    if matches:
                        self._funds(data['coin'], Decimal(0))
                        data['message'] = 'Transazione trovata nello storico KDF con conferme blockchain. Nessun reinvio effettuato.'
                        self._save(rid, 'CONFIRMED', data)
                except Exception:
                    data['message'] = 'Storico KDF non disponibile o non sincronizzato: verificare il TXID. Non ripetere l’invio; gli esiti incerti restano bloccanti.'
                    self._save(rid, state, data)
            return self._public(rid, *self._row(rid))

    def cancel(self, rid):
        with self.lock:
            state, data = self._row(rid)
            if state not in {'PREPARING', 'READY', 'FAILED', 'CANCELLED'}:
                raise ValueError('Invio già tentato: non è annullabile dalla TUI. Verificare il TXID, senza ripetere il trasferimento.')
            data.pop('tx_hex', None)
            data['message'] = 'Bozza annullata: nessuna transazione trasmessa.'
            self._save(rid, 'CANCELLED', data)
            return self._public(rid, 'CANCELLED', data)

    def confirm(self, rid):
        with self.lock:
            state, data = self._row(rid)
            if state in {'SUBMITTING', 'UNKNOWN', 'SENT', 'CONFIRMED'}:
                return self._public(rid, state, data)
            if state != 'READY':
                raise ValueError('Transazione non pronta: aggiornare lo stato prima di confermare.')
            if time.time() > data['expires']:
                raise ValueError('Preventivo commissioni scaduto: annullare la bozza e preparare un nuovo riepilogo. Nessun invio eseguito.')
            with self._exclusive():
                self._idle()
                qty, fee = Decimal(data['amount']), Decimal(data['fee'])
                self._funds(data['coin'], qty + (fee if data['fee_coin'] == data['coin'] else 0))
                if data['fee_coin'] != data['coin']:
                    self._funds(data['fee_coin'], fee)
                if time.time() > data['expires']:
                    raise ValueError('Il riepilogo è scaduto durante l’attesa o i controlli: annullare la bozza e prepararne una nuova. Nessun invio eseguito.')
                data['message'] = 'Trasmissione tentata: verificare il TXID. Non ripetere il trasferimento.'
                self._save(rid, 'SUBMITTING', data)  # durable before broadcast
                try:
                    result = self.c.kdf.legacy('send_raw_transaction', coin=data['coin'], tx_hex=data['tx_hex'])
                    if str(result.get('tx_hash', '')).removeprefix('0x') != data['tx_hash'].removeprefix('0x'):
                        raise ValueError('TXID non corrispondente')
                    state = 'SENT'
                    data['message'] = 'Transazione trasmessa, non ancora confermata sulla blockchain. Verificare il TXID prima di un altro invio.'
                except Exception:
                    state = 'UNKNOWN'
                    data['message'] = 'Risposta alla trasmissione mancante o incoerente: i fondi potrebbero essere già partiti. Verificare il TXID; nessun reinvio automatico.'
                self._save(rid, state, data)
                return self._public(rid, state, data)
