"""Maximize common maker coverage within selected inventory/debit envelopes.

The existing Spot contract routes each asset through USDT. No exchange-specific
protocol, arbitrary pair routing, transfer, market order or automatic execution.
"""
from __future__ import annotations
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
from .rebalance import coverage_targets, fingerprint, number


def propose_selected(context, snapshots, balances, rules):
    notes, excluded, sizing = [], [], []
    targets, kdf, _ = coverage_targets(context, snapshots, warnings=notes, excluded=excluded, sizing=sizing)
    if notes:
        raise ValueError('Ordine fisso incompatibile con i minimi o la liquidità hedge attuale: ' + '; '.join(notes))
    if excluded:
        raise ValueError('Uno o più maker selezionati non sono copribili per limiti KDF/mercato: '
                         + '; '.join(item['reason'] for item in excluded))
    if not targets:
        raise ValueError('Nessun target hedge disponibile: verificare budget residui e limiti giornalieri dei maker')
    protected = {a: D(q) for a, q in context.get('protected_targets', {}).items()}
    available = {a: max(D(0), b['free'] - protected.get(a, D(0))) for a, b in balances.items()}
    budget = {a: D(q) for a, q in context['allocation']['remaining'].items()}
    fee = number(context['fee'], 'commissione', positive=False)
    if not D(0) <= fee < D('.1'):
        raise ValueError('Commissione fuori intervallo prudenziale')
    missing_symbols = context.get('unavailable_sources', [])
    notes.extend(f'{a}: non utilizzabile, coppia Spot USDT o regole/commissioni non disponibili.' for a in missing_symbols)

    @lru_cache(maxsize=64)
    def parameters(asset, side):
        symbol = asset + 'USDT'
        rule = rules.get(symbol)
        snap = snapshots.get(symbol)
        from .models import HedgeSide
        if rule is None or snap is None or not rule.allows(HedgeSide(side)) or 'LIMIT' not in rule.order_types:
            return None
        levels = snap.order_book().asks if side == 'BUY' else snap.order_book().bids
        price = levels[0].price * (D('1.01') if side == 'BUY' else D('.99'))
        price = (price / snap.price_step).to_integral_value(
            rounding=ROUND_FLOOR if side == 'BUY' else ROUND_CEILING) * snap.price_step
        if price <= 0:
            return None
        depth = sum((l.quantity for l in levels if
                     (l.price <= price if side == 'BUY' else l.price >= price)), D(0)) / 2
        if rule.max_quote_amount is not None:
            depth = min(depth, rule.max_quote_amount / price)
        return snap, price, depth

    def order(asset, side, desired, capacity=None):
        info = parameters(asset, side)
        if not info:
            return None
        snap, price, depth = info
        cap = depth if capacity is None else min(depth, max(D(0), capacity))
        rounding = ROUND_CEILING if side == 'BUY' else ROUND_FLOOR
        qty = (desired / snap.quantity_step).to_integral_value(rounding=rounding) * snap.quantity_step
        # Minimum lots may overfund a tiny deficit, but never exceed the
        # user's envelope, real depth or venue maximum notional.
        minimum = (snap.min_quote_amount / price / snap.quantity_step).to_integral_value(rounding=ROUND_CEILING) * snap.quantity_step
        qty = max(qty, minimum) if desired > 0 else D(0)
        if qty <= 0 or qty > cap:
            return None
        return {'asset': asset, 'symbol': snap.symbol, 'side': side,
                'quantity': str(qty), 'price': str(price), 'notional': str(qty * price),
                'allocation_id': context['allocation']['id'], 'budget_fee': str(fee)}

    def estimate(fraction):
        desired = {a: q * fraction for a, q in targets.items()}
        buys = []
        for asset, target in sorted(desired.items()):
            if asset == 'USDT':
                continue
            deficit = max(D(0), target - available.get(asset, D(0)))
            if deficit:
                item = order(asset, 'BUY', deficit / (1 - fee))
                if item is None:
                    return None
                buys.append(item)
        cash = available.get('USDT', D(0)) - desired.get('USDT', D(0))
        spend = min(budget.get('USDT', D(0)), max(D(0), cash))
        cost = sum((D(i['notional']) * (1 + fee) for i in buys), D(0))
        need = max(D(0), cost - spend) + max(D(0), -cash)
        sales = []
        # Lowest spread first; stable asset tie-break avoids arbitrary turnover.
        sources = []
        for asset in budget:
            if asset == 'USDT' or budget[asset] <= 0:
                continue
            info = parameters(asset, 'SELL')
            if info:
                snap, price, depth = info
                spread = snap.order_book().asks[0].price / snap.order_book().bids[0].price - 1
                sources.append((spread, asset, price, depth, snap.quantity_step))
        for _, asset, price, depth, step in sorted(sources):
            if need <= 0:
                break
            surplus = max(D(0), available.get(asset, D(0)) - desired.get(asset, D(0)))
            capacity = min(budget[asset], surplus) / (1 + fee)
            capacity = min(capacity, depth)
            maximum = (capacity / step).to_integral_value(rounding=ROUND_FLOOR) * step
            wanted = min(maximum, (need / (price * (1 - fee)) / step).to_integral_value(rounding=ROUND_CEILING) * step)
            item = order(asset, 'SELL', wanted, capacity=capacity)
            if item:
                sales.append(item)
                need -= D(item['notional']) * (1 - fee)
        return (need <= 0, sales, buys, desired)

    best = estimate(D(1))
    fraction = D(1)
    if best is None or not best[0]:
        low, high = D(0), D(1)
        best = estimate(low)
        for _ in range(64):
            middle = (low + high) / 2
            result = estimate(middle)
            if result is not None and result[0]:
                low, best = middle, result
            else:
                high = middle
        fraction = low
    _, sales, buys, covered = best
    # The first trade is actually funded now. Later buys are indicative and
    # require confirmed sale proceeds + new analysis; never spend an ACK.
    ready = list(sales)
    cash = min(budget.get('USDT', D(0)), max(D(0), available.get('USDT', D(0)) - covered.get('USDT', D(0))))
    if not sales:
        for item in buys:
            debit = D(item['notional']) * (1 + fee)
            if debit <= cash:
                ready.append(item)
                cash -= debit
    pct = fraction * 100
    notes.append(f'Copertura comune massima stimata: {pct:.2f}% dei target selezionati, inclusa riserva del 20%. '
                 'Non modifica quantità o stato dei maker; non autorizza maker sotto i minimi hedge.')
    if fraction < 1:
        notes.append('Copertura completa non raggiungibile con questi budget, fondi protetti, minimi e profondità. '
                     'Aumentare le percentuali o scegliere altri fondi/meno maker; i controlli live restano obbligatori.')
    for item in sizing:
        notes.append(f"Maker #{item['number']} {item['sell']} → {item['buy']}: target su {item['quantity']} {item['sell']} ({item['basis']}).")
    return {'targets': {a: str(q) for a, q in covered.items()},
            'full_targets': {a: str(q) for a, q in targets.items()},
            'kdf_targets': {a: str(q) for a, q in kdf.items()},
            'coverage_percent': str(pct.quantize(D('.01'), rounding=ROUND_FLOOR)), 'allocation': context['allocation'],
            'funding': {a: {'required': str(covered[a]), 'full_required': str(q),
                           'available': str(available.get(a, D(0))),
                           'missing': str(max(D(0), covered[a] - available.get(a, D(0))))} for a, q in targets.items()},
            'protected_targets': {a: str(q) for a, q in protected.items()},
            'projected_orders': sales + buys, 'orders': ready, 'transfers': [],
            'notes': notes, 'strategy_actions': excluded, 'sizing': sizing,
            'fingerprint': fingerprint(context)}
