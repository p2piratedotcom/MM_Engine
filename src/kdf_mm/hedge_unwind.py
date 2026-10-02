"""Fail-closed price protection for reversing a filled hedge after a KDF refund.

This module never submits an order.  A MEXC rebalance may only use a failed
swap's hedge inventory after the maker refund is recorded and the proposed
limit is no worse than the verified, fee-inclusive original execution.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path


def _amount(value, name: str, *, positive: bool = False) -> D:
    try:
        amount = D(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f'{name} non valido') from exc
    if not amount.is_finite() or (amount <= 0 if positive else amount < 0):
        raise ValueError(f'{name} non valido')
    return amount


def _original_net_unit_price(client, plan: dict, result: dict) -> D:
    """Return the verified all-in BUY cost or net SELL proceeds per base unit."""
    symbol = str(plan['symbol'])
    side = str(plan['side'])
    if side not in {'BUY', 'SELL'}:
        raise ValueError(f'{symbol}: lato hedge originale non valido')
    order_id = result.get('orderId')
    client_id = str(plan['client_order_id'])
    if not order_id or result.get('clientOrderId') != client_id or result.get('status') != 'FILLED':
        raise ValueError(f'{symbol}: identità o esito hedge originale non verificabile')
    expected_qty = _amount(result.get('executedQty'), 'quantità hedge', positive=True)
    expected_quote = _amount(result.get('cummulativeQuoteQty'), 'controvalore hedge', positive=True)
    if expected_qty != _amount(plan.get('quantity'), 'quantità piano', positive=True):
        raise ValueError(f'{symbol}: quantità hedge originale discordante')
    try:
        trades = client.account_trades(symbol=symbol, order_id=str(order_id), limit=100)
    except Exception as exc:
        raise ValueError(f'{symbol}: esecuzioni MEXC non verificabili ora; inversione bloccata') from exc
    if not isinstance(trades, list) or not trades or len(trades) >= 100:
        raise ValueError(f'{symbol}: esecuzioni MEXC originali incomplete; inversione bloccata')
    quantity = quote = commission = D(0)
    seen = set()
    for trade in trades:
        if not isinstance(trade, dict) or str(trade.get('orderId')) != str(order_id):
            raise ValueError(f'{symbol}: esecuzione MEXC non corrispondente')
        trade_id = str(trade.get('id') or '')
        if not trade_id or trade_id in seen:
            raise ValueError(f'{symbol}: esecuzioni MEXC duplicate o senza ID')
        seen.add(trade_id)
        if 'isBuyer' in trade and (not isinstance(trade['isBuyer'], bool)
                                   or trade['isBuyer'] != (side == 'BUY')):
            raise ValueError(f'{symbol}: lato esecuzione MEXC discordante')
        qty = _amount(trade.get('qty'), 'quantità eseguita', positive=True)
        price = _amount(trade.get('price'), 'prezzo eseguito', positive=True)
        traded_quote = _amount(trade.get('quoteQty'), 'controvalore eseguito', positive=True)
        if abs(traded_quote - qty * price) > max(D('0.00000001'), traded_quote * D('0.000001')):
            raise ValueError(f'{symbol}: controvalore esecuzione discordante')
        fee = _amount(trade.get('commission'), 'commissione eseguita')
        if fee and trade.get('commissionAsset') != 'USDT':
            raise ValueError(f'{symbol}: commissione originale non in USDT; conversione da verificare')
        quantity += qty
        quote += traded_quote
        commission += fee
    if quantity != expected_qty or abs(quote - expected_quote) > max(D('0.00000001'), quote * D('0.000001')):
        raise ValueError(f'{symbol}: esecuzioni MEXC non complete o discordanti')
    net_quote = quote + commission if side == 'BUY' else quote - commission
    if net_quote <= 0:
        raise ValueError(f'{symbol}: controvalore netto originale non valido')
    return net_quote / quantity


def protect_failed_hedge_inventory(
    journal_path: str,
    client,
    item: dict,
    fee: D,
    *,
    refunded_swap_uuids=(),
) -> None:
    """Reject an unfavorable inverse rebalance order; absence of evidence blocks.

    The comparison is per leg, not basket-net: a gain on another asset cannot
    silently subsidize a losing reversal.  This is deliberately stricter than
    an aggregate break-even test, and leaves all execution user-confirmed.
    """
    fee = _amount(fee, 'commissione prevista')
    if fee >= D('0.1'):
        raise ValueError('Commissione prevista fuori limite')
    symbol, side = str(item['symbol']), str(item['side'])
    if side not in {'BUY', 'SELL'}:
        raise ValueError('Lato rebalance non valido')
    proposed = _amount(item['price'], 'prezzo limite rebalance', positive=True)
    path = Path(journal_path).resolve()
    if not path.is_file():
        raise ValueError('Journal hedge assente: inversione non verificabile')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        event_columns = ({row[1] for row in db.execute("PRAGMA table_info(received_hedge_events)")}
                         if 'received_hedge_events' in tables else set())
        rows = (db.execute("""
            SELECT b.swap_uuid,b.plan,b.result,b.state,o.terminal_event,o.kdf_success
              FROM basket_legs AS b
              JOIN received_swap_outcomes AS o USING (swap_uuid)
             WHERE o.kdf_success=0
        """).fetchall() if {'basket_legs', 'received_swap_outcomes'}.issubset(tables) else [])
        # Legacy one-leg hedges use a different journal representation. Until
        # their full execution/fee basis is verified, never let a generic
        # rebalance silently reverse them.
        event_payload = 'e.payload_json' if 'payload_json' in event_columns else "'{}'"
        legacy = (db.execute(f"""
            SELECT e.hedge_symbol,a.hedge_side,a.executed_quantity,o.terminal_event,{event_payload}
              FROM hedge_attempts AS a
              JOIN received_hedge_events AS e USING (swap_uuid)
              JOIN received_swap_outcomes AS o USING (swap_uuid)
             WHERE o.kdf_success=0
        """).fetchall() if {'hedge_attempts', 'received_hedge_events', 'received_swap_outcomes'}.issubset(tables) else [])
    venue = str(item.get('cex', 'MEXC')).upper()
    for old_symbol, old_side, old_quantity, _, raw_event in legacy:
        try:
            old_venue = str(json.loads(raw_event).get('cex', 'MEXC')).upper()
        except (TypeError, ValueError, AttributeError):
            old_venue = 'MEXC'
        if old_venue != venue:
            continue
        if old_symbol == symbol and old_side != side and _amount(old_quantity, 'quantità hedge') > 0:
            raise ValueError(f'{symbol}: hedge precedente di swap fallito; base e commissioni da verificare '
                             'prima di qualsiasi inversione. Conservare i fondi.')
    verified_refunds = {str(value) for value in refunded_swap_uuids}
    for swap_uuid, raw_plan, raw_result, state, terminal_event, success in rows:
        plan = json.loads(raw_plan)
        if str(plan.get('cex', 'MEXC')).upper() != venue:
            continue
        if plan.get('symbol') != symbol or plan.get('side') == side:
            continue
        if success != 0 or state != 'FILLED':
            raise ValueError(f'{symbol}: copertura originale non conclusa; inversione bloccata')
        if terminal_event != 'MakerPaymentRefunded' and str(swap_uuid) not in verified_refunds:
            raise ValueError(f'{symbol}: rimborso KDF maker non verificato; inversione bloccata')
        result = json.loads(raw_result or 'null')
        if not isinstance(result, dict):
            raise ValueError(f'{symbol}: esito hedge originale mancante')
        original = _original_net_unit_price(client, plan, result)
        inverse = proposed * (1 + fee if side == 'BUY' else 1 - fee)
        if (side == 'BUY' and inverse > original) or (side == 'SELL' and inverse < original):
            raise ValueError(
                f'{symbol}: inversione in perdita dopo le commissioni '
                f'(limite netto {inverse:.8f}, hedge originale {original:.8f} USDT/unità). '
                'Conservare i fondi e ricalcolare la copertura degli ordini.'
            )
