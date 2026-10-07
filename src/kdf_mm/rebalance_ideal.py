"""Local maker obligations, independent of current CEX balances/depth."""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from .strategy import StrategySpec, number
from .rebalance import RESERVE


def maker_key(rows):
    data = [{'spec': r['spec'], 'remaining': r.get('remaining_sold'),
             'daily_remaining': r.get('daily_remaining_sold'), 'deleted':r.get('state')=='DELETED', 'enabled':bool(r.get('enabled'))} for r in rows]
    return hashlib.sha256(json.dumps(sorted(data, key=lambda r:r['spec']['strategy_id']), sort_keys=True).encode()).hexdigest()


def covers_open(ideal, quotes):
    """A frozen reference must still cover every currently advertised liability."""
    makers = {m['strategy_id']: m for m in ideal['makers']}
    quantities, received_amounts = defaultdict(D), defaultdict(D)
    for quote in quotes:
        quantities[quote['strategy_id']] += number(quote['volume'], 'obbligo maker aperto')
        received_amounts[quote['strategy_id']] += D(quote['volume']) * number(quote['price'], 'prezzo maker aperto')
    for sid, quantity in quantities.items():
        maker = makers.get(sid)
        if maker is None:
            return False
        if quantity > D(maker['quantity']) or received_amounts[sid] > D(maker['quantity']) * D(maker['price']):
            return False
    return True


def build_ideal(rows, quotes, fee):
    """Uses local persisted quotes only. No KDF/CEX request or synthetic funds."""
    makers, targets, missing_valuation = [], defaultdict(D), []
    for row in rows:
        spec = StrategySpec.from_payload(row['spec'])
        if not spec.hedging_enabled: continue
        quote = quotes.get(spec.strategy_id, {})
        preview = row.get('preview', {}).get('plan', {})
        if (preview.get('kdf_base'),preview.get('kdf_rel'))!=(spec.sold.ticker,spec.bought.ticker):
            preview = {}
        if spec.quantity_mode == 'fixed':
            quantity,basis = spec.fixed_sold,'configured_fixed'
        elif spec.max_sold is not None:
            quantity,basis = spec.max_sold,'configured_auto_maximum'
        elif not spec.replenish:
            quantity,basis = row['remaining_sold'],'remaining_maker_budget'
        else:
            # No limitless wallet/book sizing: the declared initial budget is
            # the nominal funding reference for a replenishing auto maker.
            quantity,basis = spec.total_sold_budget,'configured_auto_budget_reference'
        quantity = number(quantity, 'quantità maker ideale')
        caps = [quantity]
        if spec.max_sold is not None: caps.append(spec.max_sold)
        if not spec.replenish: caps.append(number(row['remaining_sold'], 'budget residuo', positive=False))
        if spec.daily_sold_cap is not None: caps.append(number(row['daily_remaining_sold'], 'limite giornaliero', positive=False))
        quantity = min(caps)
        if spec.quantity_mode=='auto': quantity *= spec.auto_fraction
        if quote.get('basis')=='open_maker':
            quantity = max(quantity, number(quote['volume'],'obbligo maker aperto'))
        if quantity == 0:
            continue
        rate = (spec.fixed_price if spec.side.value == 'SELL_ARRR' else 1/spec.fixed_price) if spec.fixed_price is not None else quote.get('price') or preview.get('kdf_price')
        if rate is None:
            raise ValueError(f'Maker #{row.get("creation_number", spec.strategy_id)}: prezzo di riferimento assente; creare una preview maker prima del rebalance')
        if quote.get('basis')=='open_maker': rate = quote['price']
        rate = number(rate, 'prezzo maker')
        legs = []
        saved = row.get('preview', {}).get('hedge_legs', [])
        for route, side, amount in ((spec.sold, 'BUY', quantity), (spec.bought, 'SELL', quantity*rate)):
            if not route.symbol: continue
            reference = next((leg.get('limit_price') for leg in saved if leg.get('symbol')==route.symbol), None)
            if side == 'BUY' and spec.bought.asset == 'USDT': reference = str(rate)
            if side == 'SELL':
                targets[route.asset] += amount*(1+fee)*RESERVE
            elif reference is not None:
                # Saved hedge limits already contain their impact margin.
                margin = 1+spec.impact if spec.bought.asset=='USDT' else D(1)
                targets['USDT'] += amount*number(reference, 'riferimento USDT')*margin*(1+fee)*RESERVE
            else:
                missing_valuation.append(route.asset)
            legs.append({'asset':route.asset,'symbol':route.symbol,'side':side,
                         'quantity':str(amount),'reference_usdt':str(reference) if reference is not None else None,
                         'impact':str(spec.impact),'depth_fraction':str(spec.depth_fraction)})
        makers.append({'strategy_id':spec.strategy_id,'number':row.get('creation_number',spec.strategy_id),
                       'sell':spec.sold.ticker,'buy':spec.bought.ticker,'quantity':str(quantity),
                       'price':str(rate),'basis':basis,'quote_observed_utc':quote.get('observed_at'),
                       'hedge_legs':legs})
    if not makers: raise ValueError('Nessun target maker residuo da coprire')
    return {'schema':1,'maker_key':maker_key(rows),'makers':makers,'reserve_percent':20,
            'fee':str(fee),'indicative_targets':{a:str(q) for a,q in targets.items()},
            'unvalued_buy_assets':sorted(set(missing_valuation)),
            'basis':'local_maker_reference','cex_queried':False}


def funding_targets(ideal, snapshots):
    """Same native obligations, valued at fresh executable CEX prices."""
    targets, sizing = defaultdict(D), []
    fee = number(ideal['fee'], 'commissione', positive=False)
    for maker in ideal['makers']:
        required = defaultdict(D)
        for leg in maker['hedge_legs']:
            snapshot = snapshots[leg['symbol']]
            qty = number(leg['quantity'], 'quantità hedge ideale')
            qty = (qty/snapshot.quantity_step).to_integral_value(rounding=ROUND_CEILING)*snapshot.quantity_step
            asset = 'USDT' if leg['side']=='BUY' else leg['asset']
            amount = qty*(snapshot.order_book().asks[0].price*(1+D(leg['impact'])) if leg['side']=='BUY' else 1)
            required[asset] += amount*(1+fee)*RESERVE
        for asset,q in required.items(): targets[asset] += q
        sizing.append({**{k:maker[k] for k in ('strategy_id','number','sell','buy','quantity','price','basis')},
                       'targets':{a:str(q) for a,q in required.items()}})
    return dict(targets), sizing


def hedge_limits(context, snapshots, rules):
    """Report physical hedge capacity separately from inventory funding.

    Same-symbol makers share depth/volume. Respect each maker's actual impact
    and depth fraction, subtract outside obligations, and never shrink targets.
    """
    from .models import HedgeSide
    selected, protected = defaultdict(D), defaultdict(D)
    makers = (context.get('ideal') or {}).get('makers', [])
    for ideal, totals in ((context.get('ideal'),selected),(context.get('protected_ideal'),protected)):
        for maker in (ideal or {}).get('makers', []):
            for leg in maker['hedge_legs']:
                snap = snapshots[leg['symbol']]
                qty = (D(leg['quantity'])/snap.quantity_step).to_integral_value(rounding=ROUND_CEILING)*snap.quantity_step
                totals[(leg['symbol'],leg['side'])] += qty
    result = []
    for maker in makers:
        cap, reasons = D(1), []
        for leg in maker['hedge_legs']:
            snap,rule = snapshots[leg['symbol']],rules[leg['symbol']]
            side = leg['side']
            if not rule.allows(HedgeSide(side)) or 'LIMIT' not in rule.order_types:
                cap = D(0); reasons.append('Lato hedge non consentito dalle regole attuali'); continue
            levels = snap.order_book().asks if side=='BUY' else snap.order_book().bids
            boundary = levels[0].price*(1+D(leg['impact']) if side=='BUY' else 1-D(leg['impact']))
            boundary = (boundary/snap.price_step).to_integral_value(
                rounding=ROUND_FLOOR if side=='BUY' else ROUND_CEILING)*snap.price_step
            if boundary<=0:
                cap = D(0); reasons.append('Prezzo hedge non valido'); continue
            key = leg['symbol'],side
            depth = sum((l.quantity for l in levels if (l.price<=boundary if side=='BUY' else l.price>=boundary)),D(0))
            depth = max(D(0),depth*D(leg.get('depth_fraction','.50'))-protected[key])
            ratio = min(D(1),depth/selected[key])
            cap = min(cap,ratio)
            if ratio<1: reasons.append(f'Profondità condivisa {side} {leg["asset"]} insufficiente')
            selected_volume = sum((q for (symbol,_),q in selected.items() if symbol==leg['symbol']),D(0))
            protected_volume = sum((q for (symbol,_),q in protected.items() if symbol==leg['symbol']),D(0))
            volume = max(D(0),snap.base_volume_24h*D(context['daily_fraction'])-protected_volume)
            cap = min(cap,volume/selected_volume)
            if volume<selected_volume: reasons.append(f'Limite volume 24h {leg["asset"]} insufficiente')
            qty = (D(leg['quantity'])/snap.quantity_step).to_integral_value(rounding=ROUND_CEILING)*snap.quantity_step
            if qty*boundary<rule.min_quote_amount:
                cap = D(0); reasons.append('Quantità di riferimento sotto il minimo hedge')
            if rule.max_quote_amount is not None and qty*boundary>rule.max_quote_amount:
                cap = min(cap,rule.max_quote_amount/(qty*boundary))
                reasons.append('Quantità di riferimento sopra il massimo hedge')
        result.append({'number':maker['number'],'strategy_id':maker['strategy_id'],
            'maximum_percent':str((cap*100).quantize(D('.01'),rounding=ROUND_FLOOR)),
            'reason':'; '.join(reasons)})
    return result
