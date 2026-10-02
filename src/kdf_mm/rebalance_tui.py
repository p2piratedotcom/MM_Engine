"""One input owner, asynchronous reads/writes, explicit per-order confirmation."""
import curses
import textwrap
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor


def balance_lines(result, width):
    """Column widths use the largest displayed value, in terminal cells."""
    from .tui import _cell_width, _safe_text
    header = ['Coin', 'Disponibile', 'Impegnato', 'Totale']
    rows = [[_safe_text(a), b['free'], b['locked'],
             str(Decimal(b['free']) + Decimal(b['locked']))]
            for a, b in sorted(result['balances'].items())]
    sizes = [max(_cell_width(row[i]) for row in [header, *rows]) for i in range(4)]
    lines = [f"Saldi Spot aggiornati alle {result['at']}", '']
    if sum(sizes) + 9 <= width:
        def padded(row):
            return ' | '.join((value + ' ' * (size - _cell_width(value))) if i == 0
                              else (' ' * (size - _cell_width(value)) + value)
                              for i, (value, size) in enumerate(zip(row, sizes)))
        lines += [padded(header), '-+-'.join('-' * size for size in sizes)]
        lines += [padded(row) for row in rows]
    else:
        # Preserve exact amounts on narrow terminals instead of wrapping a table.
        for row in rows:
            lines += [row[0], *[f'  {label}: {value}' for label, value in zip(header[1:], row[1:])], '']
    if not rows:
        lines.append('Nessun saldo Spot positivo.')
    return lines


def analysis_outcome(plan, *, transfers=False):
    if transfers and plan.get('transfer_blockers'):
        return 'TRASFERIMENTI SOSPESI — VERIFICA RICHIESTA'
    items = plan.get('transfers' if transfers else 'orders', [])
    if items:
        return 'RIEQUILIBRIO NECESSARIO'
    if plan.get('notes'):
        return 'ANALISI DA VERIFICARE — LEGGERE GLI AVVISI'
    if plan.get('strategy_actions'):
        return 'NESSUN RIEQUILIBRIO FONDI NECESSARIO — AZIONE STRATEGIA CONSIGLIATA'
    return 'NESSUN RIEQUILIBRIO NECESSARIO'


def plan_lines(plan, *, transfers=False, venue=None):
    venue = str(venue or plan.get('cex') or 'MEXC')
    lines = [analysis_outcome(plan, transfers=transfers), '',
             'OBIETTIVO', f'Copertura ordini KDF + 20% su {venue} per le strategie attualmente pubblicabili.', '',
             'SALDI SPOT RICHIESTI']
    lines += [f'  {a}: {q}' for a, q in sorted(plan['targets'].items())] or ['Nessuna copertura richiesta.']
    for asset, row in sorted(plan.get('funding', {}).items()):
        lines += [f"  {asset}: disponibili {row['available']}; mancanti {row['missing']}"]
    lines += ['']
    if transfers:
        lines += [f'TRASFERIMENTI {venue} -> KDF', 'SOLO MANUALE; nessun prelievo viene inviato.',
                  'Importi netti richiesti: verificare rete, indirizzo, memo, minimi e commissioni.',
                  'Non trasferire le riserve gas BNB/altre chain; ricalcolare dopo ogni invio.', '']
        for blocker in plan.get('transfer_blockers', []):
            lines += ['MOTIVO DEL BLOCCO', blocker, '', 'Nessun importo trasferibile suggerito finché il controllo non passa.', '']
        for row in plan['transfers']:
            lines += [f"Invio suggerito: {row['quantity']} {row['asset']} verso KDF {row['ticker']}",
                      f"  Scoperto residuo: {row['uncovered']} {row['ticker']}", '']
        if not plan['transfers'] and not plan.get('transfer_blockers') and not plan['notes']:
            lines.append('Nessun trasferimento necessario con i dati disponibili.')
    else:
        lines += ['ORDINI SUGGERITI', 'Ordini LIMIT, non market: possono restare aperti o essere eseguiti in parte.',
                  'Si esegue SOLO il primo ordine. Ricalcolare dopo ogni esecuzione.',
                  'Prima: strategie/repricing in pausa, nessun ordine/swap o hedge pendente.',
                  'Il worker locale aggiornato si coordina automaticamente con il riequilibrio.', '']
        for index, row in enumerate(plan['orders'], 1):
            lines += [f"{index}. {row['side']} {row['quantity']} {row['asset']} / {row['symbol']}",
                      f"   Limite: {row['price']} USDT | Controvalore: {row['notional']} USDT", '']
        if not plan['orders']:
            if plan.get('strategy_actions') and not plan['notes']:
                lines.append(f'I saldi coprono i target calcolabili; nessun ordine {venue} necessario.')
            else:
                lines.append('Nessun ordine proposto.' + (' Verificare gli avvisi sotto.' if plan['notes'] else ''))
    if plan.get('strategy_actions'):
        lines += ['', 'CONSIGLI STRATEGIA']
        for row in plan['strategy_actions']:
            lines += [
                f"{row['strategy_id']} — {row['route']} ({row['side']})",
                f"  {row['reason']}.",
                f"  Azione: {row['action']}.",
                f'  Strategia esclusa dai target correnti: aggiungere fondi {venue} non risolve questo limite.',
                '',
            ]
    if plan['notes']:
        lines += ['', 'AVVISI']
        for note in plan['notes']:
            lines += [note, '']
    return lines


def run_cex_page(screen, service, theme):
    from .tui import _write, _footer, _safe_text, _normalize_key
    executor = ThreadPoolExecutor(max_workers=1)
    pending = executor.submit(service.balances)
    action, mode, plan = 'balances', 'balances', None
    balance_result = None
    balance_offset = 0
    venue = str(getattr(service, 'venue', 'MEXC'))
    lines, offset, modal, choice = [f'Lettura saldi Spot {venue}...'], 0, False, 0
    try:
        while True:
            if pending is not None and pending.done():
                try:
                    result = pending.result()
                    if action == 'balances':
                        balance_result = result
                    elif action in {'rebalance', 'transfers'}:
                        plan = result
                        lines = [f"Proposta delle {plan['at']} (validità 120 secondi)"] + plan_lines(plan, transfers=action == 'transfers', venue=venue)
                    elif action == 'execute':
                        plan = None
                        lines = [result, 'Usare [R] per verificare lo stato; [M] per ricalcolare.']
                    else:
                        lines = ['Ultime operazioni rebalance:'] + [
                            f"{r['id']} {r['order']['side']} {r['order']['quantity']} {r['order']['asset']}: {r['state']}"
                            for r in result]
                        if not result:
                            lines.append('Nessun ordine rebalance registrato.')
                except Exception as exc:
                    plan = None
                    lines = ['[ERRORE] ' + str(exc), 'Nessun reinvio automatico. [R] verifica gli ordini già inviati.']
                pending, offset = None, 0
            height, width = screen.getmaxyx()
            if mode == 'balances' and balance_result is not None and pending is None:
                lines = balance_lines(balance_result, max(1, width - 4))
            screen.erase()
            _write(screen, 0, 1, f'{venue} — SALDI E RIEQUILIBRIO', theme.title)
            _write(screen, 1, 1, '[LIVE]' if service.settings.live_trading else '[PROVA: nessuna esecuzione]', theme.warning)
            display = lines
            if modal:
                order = plan['orders'][0]
                display = [f'CONFERMA ORDINE REALE SU {venue}',
                           f"{order['side']} {order['quantity']} {order['asset']}",
                           f"Mercato {order['symbol']} — limite {order['price']} USDT per coin",
                           f"Controvalore limite: {order['notional']} USDT + commissioni",
                           'Solo questo ordine: nessun prelievo, nessun altro ordine automatico.',
                           'Un ordine LIMIT può restare aperto. La proposta viene ricontrollata prima dell’invio.']
            wrapped = [part for line in display for part in
                       (textwrap.wrap(_safe_text(line), max(1, width - 4)) or [''])]
            visible = max(1, height - 6)
            offset = min(offset, max(0, len(wrapped) - visible))
            for i, line in enumerate(wrapped[offset:offset + visible], 3):
                emphasis = line.startswith(('NESSUN RIEQUILIBRIO', 'RIEQUILIBRIO NECESSARIO',
                                           'TRASFERIMENTI SOSPESI', 'ANALISI DA VERIFICARE'))
                _write(screen, i, 1, line, theme.selected if emphasis else
                       theme.section if line and line.isupper() else 0)
            if modal:
                _footer(screen, 'Inviare SOLO ordine 1 con quantità e limite mostrati?',
                        ('> [Indietro]   [Esegui]' if choice == 0 else '[Indietro]   > [Esegui]') + '  Frecce / Invio', theme)
            elif pending:
                _footer(screen, 'Operazione in corso... input e scorrimento disponibili',
                        'Invio in corso: attendere l’esito' if action == 'execute' else
                        '[Esc] scelta CEX' if mode == 'balances' else f'[Esc] torna a [8] {venue}', theme)
            else:
                _footer(screen, f'[B] saldi  [M] {venue} rebalance  [K] {venue} - KDF rebalance',
                        '[E] conferma  [R] stato ordini  [↑↓] scorri  ' +
                        ('[Esc] scelta CEX' if mode == 'balances' else f'[Esc] torna a [8] {venue}'), theme)
            screen.refresh()
            key = _normalize_key(screen, screen.getch())
            if key == 3:
                raise KeyboardInterrupt
            if modal:
                if key == 27:
                    modal = False
                elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
                    choice = 1 - choice
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    offset = max(0, offset + (-1 if key == curses.KEY_UP else 1))
                elif key in (10, 13, curses.KEY_ENTER):
                    modal = False
                    if choice == 1:
                        pending = executor.submit(service.execute_first, plan)
                        action = 'execute'
                continue
            if key == 27:
                if pending is not None and action == 'execute':
                    # Do not orphan a financial write or permit a second page write.
                    lines = ['Invio in corso: attendere l’esito prima di uscire. Nessun reinvio.']
                    continue
                if mode != 'balances':
                    # Detach read-only work: its late result must not reopen the
                    # subsection or overwrite the restored balance snapshot.
                    if pending is not None:
                        pending.cancel()
                    pending = None
                    mode, action, plan = 'balances', 'balances', None
                    offset = balance_offset
                    if balance_result is None:
                        lines = [f'Lettura saldi Spot {venue}...']
                        pending = executor.submit(service.balances)
                    continue
                return 'CEX_SELECT'
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                offset = max(0, offset + (-1 if key == curses.KEY_UP else 1))
            if pending is not None:
                continue
            if key in (ord('e'), ord('E')) and mode == 'rebalance' and plan and plan['orders']:
                modal, choice, offset = True, 1, 0
            else:
                callbacks = {'b': ('balances', service.balances), 'm': ('rebalance', service.preview),
                             'k': ('transfers', service.transfer_preview), 'r': ('reconcile', service.reconcile)}
                selected = callbacks.get(chr(key).lower()) if 0 <= key < 256 else None
                if selected:
                    if mode == 'balances':
                        balance_offset = offset
                    action, callback = selected
                    mode, plan = action, None
                    if action == 'balances':
                        balance_result = None
                    pending = executor.submit(callback)
                    lines, offset = ['Lettura in corso...'], 0
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_mexc_page(screen, service, theme):
    """Compatibility entry point for callers predating multi-CEX support."""
    result = run_cex_page(screen, service, theme)
    return 'HOME' if result == 'CEX_SELECT' else result
