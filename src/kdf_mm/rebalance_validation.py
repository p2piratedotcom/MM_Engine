"""Validate the exact user-approved LIMIT step; never substitute a new quote."""
from decimal import Decimal as D, ROUND_CEILING
from .strategy import number
from .models import HedgeSide


def approved_step(item, fresh, snapshots, balances, rules):
    if fresh.get('coverage_percent') is None:
        raise ValueError('La copertura aggiornata non è verificabile: risolvere il blocco e ricalcolare')
    asset, side, symbol = item['asset'],item['side'],item['symbol']
    if side not in {'BUY','SELL'} or symbol != asset+'USDT':
        raise ValueError('Identità del trade approvato non valida: nuova analisi richiesta')
    qty = number(item['quantity'],'quantità approvata')
    price = number(item['price'],'prezzo limite approvato')
    snap,rule = snapshots[symbol],rules[symbol]
    if rule.base_asset!=asset or rule.quote_asset!='USDT' or not rule.allows(HedgeSide(side)) or 'LIMIT' not in rule.order_types:
        raise ValueError('Le regole CEX non consentono più il trade approvato')
    if qty % rule.quantity_step or price % rule.price_step or qty*price < rule.min_quote_amount or (rule.max_quote_amount is not None and qty*price>rule.max_quote_amount):
        raise ValueError('Quantità o prezzo approvati non rispettano i minimi/passi CEX attuali')
    levels = snap.order_book().asks if side=='BUY' else snap.order_book().bids
    best = levels[0].price
    if side=='BUY' and (price<best or price>best*D('1.01')):
        raise ValueError('Il limite BUY approvato non è più eseguibile entro l’impatto consentito: analizzare e confermare il nuovo prezzo')
    if side=='SELL' and (price>best or price<best*D('.99')):
        raise ValueError('Il limite SELL approvato non è più eseguibile entro l’impatto consentito: analizzare e confermare il nuovo prezzo')
    depth = sum((v.quantity for v in levels if (v.price<=price if side=='BUY' else v.price>=price)),D(0))/2
    if qty>depth:
        raise ValueError('Profondità attuale insufficiente per la quantità approvata: nessun ordine inviato')
    fee = number(item['budget_fee'],'commissione approvata',positive=False)
    if fee!=D(fresh['ideal']['fee']):
        raise ValueError('Commissione configurata cambiata: nuova conferma richiesta')
    available = {a:max(D(0),v['free']-D(fresh.get('protected_targets',{}).get(a,'0'))) for a,v in balances.items()}
    if side=='BUY':
        missing = max(D(0),D(fresh['full_targets'].get(asset,'0'))-available.get(asset,D(0)))
        # Rounding up to one minimum order is permissible, but arbitrary
        # excess above the remaining ideal deficit is not a useful rebalance.
        needed = missing/(1-fee)
        needed = (needed/rule.quantity_step).to_integral_value(rounding=ROUND_CEILING)*rule.quantity_step
        minimum = (rule.min_quote_amount/price/rule.quantity_step).to_integral_value(rounding=ROUND_CEILING)*rule.quantity_step
        if missing<=0 or qty>max(needed,minimum)+rule.quantity_step:
            raise ValueError('Il deficit dell’asset è già coperto o la quantità approvata è eccessiva: ricalcolare')
    else:
        # Selling an unrelated surplus is useful only if a verified shortfall
        # remains. Do not liquidate more capital after the target is covered.
        deficit = any(available.get(a,D(0))<D(q) for a,q in fresh['full_targets'].items())
        if not deficit:
            raise ValueError('Copertura ideale già finanziata: vendita non più necessaria')
        cost = D(0)
        for target_asset,target in fresh['full_targets'].items():
            if target_asset=='USDT': continue
            missing = max(D(0),D(target)-available.get(target_asset,D(0)))
            cost += missing/(1-fee)*snapshots[target_asset+'USDT'].order_book().asks[0].price*D('1.01')*(1+fee)
        cash = available.get('USDT',D(0))-D(fresh['full_targets'].get('USDT','0'))
        allowed_cash = min(D(fresh['allocation']['remaining'].get('USDT','0')),max(D(0),cash))
        needed_quote = max(D(0),cost-allowed_cash)+max(D(0),-cash)
        needed = (needed_quote/(price*(1-fee))/rule.quantity_step).to_integral_value(rounding=ROUND_CEILING)*rule.quantity_step
        minimum = (rule.min_quote_amount/price/rule.quantity_step).to_integral_value(rounding=ROUND_CEILING)*rule.quantity_step
        if needed_quote<=0 or qty>max(needed,minimum)*D('1.01')+rule.quantity_step:
            raise ValueError('Il fabbisogno da finanziare si è ridotto: la vendita approvata eccede quanto serve, ricalcolare')
    return qty,price
