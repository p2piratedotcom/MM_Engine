"""Wallet workflow: no raw signed bytes, asynchronous RPC, explicit confirmation."""
import curses
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor


def transfer_lines(row):
    labels = {'PREPARING': 'PREPARAZIONE FIRMA', 'READY': 'PRONTA — NON INVIATA',
              'SUBMITTING': 'INVIO DA VERIFICARE', 'UNKNOWN': 'ESITO INCERTO — NON REINVIARE',
              'SENT': 'TRASMESSA — ATTENDERE CONFERME', 'CONFIRMED': 'CONFERMATA',
              'FAILED': 'NON INVIATA', 'CANCELLED': 'ANNULLATA'}
    return [labels.get(row.get('state'), row.get('state', '')),
            f"Coin: {row.get('coin')} | Rete: {row.get('network', 'da verificare')}",
            f"Destinatario: {row.get('to')}", f"Quantità: {row.get('amount')} {row.get('coin')}",
            f"Commissione: {row.get('fee', 'in calcolo')} {row.get('fee_coin', '')}",
            'Controllare che il destinatario accetti esattamente questa rete.',
            'Memo/tag non supportati: non usare destinatari che li richiedono.',
            row.get('message', 'Aggiornare lo stato; nessun invio automatico.'),
            f"TXID: {row.get('tx_hash', 'non ancora disponibile')}",
            f"Richiesta: {row.get('id', '')}"]


def run_send_page(screen, api, wallet, theme):
    from .tui import _write, _footer, _safe_text
    coins = [coin for coin, item in wallet.get('balances', {}).items() if item.get('available')]
    mode, selected, field, coin, destination = 'coins', 0, '', '', ''
    row, entries, message, offset = None, [], '', 0
    pending, action = None, ''
    pool = ThreadPoolExecutor(max_workers=1)
    def post(name, payload):
        return pool.submit(api.post, '/v1/wallet/send/' + name, payload)
    try:
        while True:
            if pending is not None and pending.done():
                try:
                    result = pending.result()
                    if action == 'history':
                        entries, selected, mode = result['sends'], 0, 'history'
                        message = '' if entries else 'Nessun invio registrato.'
                    else:
                        row, mode, message = result, 'review', ''
                        curses.flushinp()  # buffered form input cannot confirm money movement
                except Exception as exc:
                    message = str(exc)
                    mode = 'review' if row else 'coins'
                pending = None
            height, width = screen.getmaxyx()
            screen.erase()
            _write(screen, 0, 1, 'PORTAFOGLIO KDF — INVIA', theme.title)
            if mode == 'coins':
                lines = ['Scegli una coin attiva (↑/↓ e Invio).',
                         'Richiesti mercati in pausa, nessun ordine o swap aperto.',
                         *[('> [X] ' if i == selected else '  [ ] ') + c for i, c in enumerate(coins)]]
                if not coins: lines += ['Nessuna coin attiva: tornare alla pagina KDF e attivarla.']
            elif mode in {'address', 'quantity'}:
                lines = [f'Coin: {coin}', 'Incolla solo l’indirizzo destinatario:' if mode == 'address' else
                         f'Destinatario: {destination}',
                         'Quantità da inviare (commissione aggiuntiva):' if mode == 'quantity' else
                         'Verificare rete. Destinatari con memo/tag non supportati.', '> ' + field,
                         'Invio prosegue; non trasmette ancora fondi.']
            elif mode == 'history':
                lines = ['Scegli un invio e premi Invio per aggiornarne lo stato.',
                         *[('> ' if i == selected else '  ') + f"{r['coin']} {r['amount']} — {r['state']} — {r['id']}"
                           for i, r in enumerate(entries)]]
            else:
                lines = transfer_lines(row) if row else []
                if mode == 'confirm':
                    lines += ['', 'CONFERMA INVIO REALE', '[Invio] trasmette questa transazione. [Esc] torna senza inviare.']
            if message: lines += ['', '[AVVISO] ' + message]
            if pending:
                lines += ['', 'Operazione in corso. Non ripetere l’invio.']
                if action in {'prepare', 'confirm'}:
                    lines += ['Se un controllo CEX/KDF occupa il blocco, attendo fino a 8 secondi.',
                              'Si attende solo il blocco: nessun reinvio automatico della transazione.']
            wrapped = [part for line in lines for part in (textwrap.wrap(_safe_text(line), max(1, width - 4)) or [''])]
            visible = max(1, height - 5)
            offset = min(offset, max(0, len(wrapped) - visible))
            for i, line in enumerate(wrapped[offset:offset + visible], 2):
                _write(screen, i, 1, line, theme.warning if 'AVVISO' in line else 0)
            _footer(screen, '[H] storico  [R] aggiorna  [X] annulla bozza  [Pg↑/Pg↓] scorri',
                    '[Invio] Conferma invio reale  [Esc] Indietro' if mode == 'confirm' else
                    '[C] conferma invio' if mode == 'review' and row and row['state'] == 'READY' else '[Esc] indietro', theme)
            screen.refresh()
            key = screen.getch()
            if key == 3: raise KeyboardInterrupt
            if key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                offset = max(0, offset + (visible if key == curses.KEY_NPAGE else -visible))
            if pending is not None:
                continue  # do not orphan a request; request identity is retained
            if key == 27:
                if mode == 'confirm': mode = 'review'
                else: return
                continue
            if mode in {'address', 'quantity'}:
                if key in (10, 13, curses.KEY_ENTER):
                    if mode == 'address':
                        destination, field, mode = field.strip(), '', 'quantity'
                    else:
                        rid = uuid.uuid4().hex
                        row = {'id': rid, 'coin': coin, 'to': destination, 'amount': field, 'state': 'PREPARING'}
                        action, mode = 'prepare', 'review'
                        pending = post('prepare', {'id': rid, 'coin': coin, 'to': destination, 'amount': field})
                elif key in (curses.KEY_BACKSPACE, 127, 8): field = field[:-1]
                elif 32 <= key < 127 and len(field) < 256: field += chr(key)
                continue
            if mode == 'confirm':
                if key in (10, 13, curses.KEY_ENTER):
                    action, mode = 'confirm', 'review'
                    pending = post('confirm', {'id': row['id'], 'confirmation': 'INVIA ' + row['id']})
                continue
            if mode in {'coins', 'history'}:
                items = coins if mode == 'coins' else entries
                if key in (curses.KEY_DOWN, curses.KEY_UP) and items:
                    selected = (selected + (1 if key == curses.KEY_DOWN else -1)) % len(items)
                if key in (10, 13, curses.KEY_ENTER) and items:
                    if mode == 'coins': coin, field, mode = coins[selected], '', 'address'
                    else:
                        row, action = entries[selected], 'status'
                        pending = post('status', {'id': row['id']})
            if key in (ord('h'), ord('H')):
                action = 'history'
                pending = pool.submit(api.get, '/v1/wallet/sends')
            elif key in (ord('r'), ord('R')) and row:
                action = 'status'
                pending = post('status', {'id': row['id']})
            elif key in (ord('x'), ord('X')) and row:
                action = 'cancel'
                pending = post('cancel', {'id': row['id']})
            elif key in (ord('c'), ord('C')) and mode == 'review' and row and row['state'] == 'READY':
                curses.flushinp()
                mode, offset = 'confirm', 0
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
