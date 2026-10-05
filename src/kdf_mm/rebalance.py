"""Local, user-confirmed Spot rebalance. Never submits withdrawals.

Each confirmation authorizes ONE bounded LIMIT order. An uncertain/open order
blocks further orders and KDF publication until read-only reconciliation succeeds.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from .market_data import MarketSnapshot
from .models import HedgeSide
from .strategy import (
    AUTO_REENTRY_MIN_FACTOR,
    StrategySpec,
    preview_strategy,
    number,
)

RESERVE = D('1.20')
TERMINAL = {'FILLED', 'CANCELED', 'PARTIALLY_CANCELED', 'REJECTED', 'EXPIRED'}


def agent_context(controller, strategies, reconciliation, repricing, *, include_wallet=True,
                  include_transfers=True):
    if strategies is None or reconciliation is None or not controller.rebalance_lock_path:
        raise ValueError('Contesto rebalance locale non configurato')
    rows = strategies.status()['strategies']
    tickers = sorted({s['spec'][r]['ticker'] for s in rows for r in ('base', 'quote')})
    if not include_transfers:
        # Spot targets still need the real spendable amount for auto sizing.
        # Fixed quantities are explicit; no receiving-coin balance is needed.
        tickers = sorted({spec.sold.ticker for row in rows
                          if (spec := StrategySpec.from_payload(row['spec'])).quantity_mode == 'auto'})
    from .vps_controller import _numeric_decimal
    wallet = {}
    for ticker in tickers if include_wallet else ():
        try:
            wallet[ticker] = {'free': str(_numeric_decimal(controller.kdf.max_maker_volume(ticker), 'volume'))}
        except Exception:
            wallet[ticker] = {'error': 'Coin non attiva o saldo spendibile non disponibile'}
    orders = controller.kdf.my_orders()
    swaps = controller.kdf.active_swaps()
    if not isinstance(orders.get('maker_orders'), dict) or not isinstance(orders.get('taker_orders'), dict):
        raise ValueError('Risposta ordini KDF incompleta')
    if not isinstance(swaps.get('uuids'), list):
        raise ValueError('Risposta swap KDF incompleta')
    refunded_reader = getattr(strategies, 'refunded_swap_uuids', None)
    refunded_swap_uuids = list(refunded_reader()) if callable(refunded_reader) else []
    return {'strategies': rows, 'wallet': wallet, 'include_transfers': include_transfers,
            'journal': controller.rebalance_lock_path.removesuffix('.rebalance.lock'),
            'refunded_swap_uuids': refunded_swap_uuids,
            'reconciliation': reconciliation.payload(),
            'orders_present': bool(orders['maker_orders'] or orders['taker_orders']),
            'swaps_present': bool(swaps['uuids']),
            'repricing': repricing.payload() if repricing else {'state': 'DISABLED'},
            'fee': str(controller.cex_taker_fee), 'buffer': str(controller.risk_buffer),
            'daily_fraction': str(controller.max_daily_volume_fraction)}


def free_balances(account, *, venue='CEX'):
    if not isinstance(account.get('balances'), list):
        raise ValueError(f'Saldo Spot {venue} non disponibile')
    result = {}
    for row in account['balances']:
        asset = str(row['asset'])
        if asset in result:
            raise ValueError('Saldo Spot duplicato')
        result[asset] = {k: number(row[k], 'saldo ' + asset, positive=False) for k in ('free', 'locked')}
    return result


def fingerprint(context):
    # Ignore volatile confirmations, previews and log details, not quantities/budgets.
    rows = [{'spec': r['spec'], 'remaining': r['remaining_sold'],
             'daily_remaining': r.get('daily_remaining_sold'), 'enabled': r['enabled'],
             'state': r['state']} for r in context['strategies']]
    return hashlib.sha256(json.dumps({'rows': rows, 'open_quotes': context.get('open_quotes', []),
        'wallet': context.get('wallet', {}),
        'fee': context['fee'], 'buffer': context['buffer'],
        'daily_fraction': context['daily_fraction']}, sort_keys=True).encode()).hexdigest()


def _auto_strategy_exclusion(spec, diagnostic):
    """Describe an auto strategy that cannot currently produce a stable quote.

    Auto sizing uses the spendable KDF balance and market/strategy limits.
    A target below the stable minimum does not imply missing CEX funds.
    """
    maximum = D(diagnostic['maximum'])
    minimum = D(diagnostic['minimum'])
    stable_minimum = minimum * AUTO_REENTRY_MIN_FACTOR
    if maximum >= stable_minimum:
        return None

    limits = {
        key: D(value) for key, value in diagnostic.get('limits', {}).items()
        if key != 'percentuale_auto'
    }
    raw_capacity = min(limits.values(), default=D(0))
    current_percent = spec.auto_fraction * 100
    recommended = None
    if raw_capacity > 0:
        required = stable_minimum / raw_capacity
        if required <= 1:
            recommended = (required * 100).to_integral_value(rounding=ROUND_CEILING)

    if recommended is not None:
        action = (
            f"con questo book, aumentare la percentuale custom auto dal "
            f"{current_percent.normalize():f}% ad almeno circa {recommended}% "
            "oppure attendere maggiore profondità"
        )
    else:
        action = (
            "verificare il saldo KDF spendibile e i limiti/budget, oppure attendere "
            "maggiore profondità; anche il 100% auto non raggiunge ora il margine stabile"
        )
    return {
        'strategy_id': spec.strategy_id,
        'market_id': spec.market_id,
        'side': spec.side.value,
        'route': f'{spec.sold.ticker} → {spec.bought.ticker}',
        'candidate': str(maximum),
        'minimum': str(minimum),
        'stable_minimum': str(stable_minimum),
        'configured_auto_percent': str(current_percent),
        'recommended_auto_percent': None if recommended is None else str(recommended),
        'action': action,
        'reason': (
            f"quantità automatica {maximum} {spec.sold.ticker}; minimo {spec.cex} "
            f"{minimum}, soglia di rientro stabile (+25%) {stable_minimum}"
        ),
    }


def coverage_targets(context, snapshots, *, warnings=None, excluded=None, sizing=None):
    """Fund published quotes, or bounded next quotes when none is published.

    Only CEX balances are synthetic: rebalance asks what inventory is needed
    to hedge the wallet, not how much unlimited wallet inventory could trade.
    KDF shares funds across different pairs, while same-pair levels add up.
    """
    cex = defaultdict(D)
    pools = defaultdict(lambda: defaultdict(D))
    routes = {}
    reserved_depth = defaultdict(D)
    fee = number(context['fee'], 'fee', positive=False)
    specs = {}
    rows = {}
    for row in context['strategies']:
        if row['state'] == 'DELETED':
            continue
        spec = StrategySpec.from_payload(row['spec'])
        sid = row.get('id', spec.strategy_id)
        if sid != spec.strategy_id or sid in specs:
            raise ValueError('Identità della strategia non valida o duplicata')
        specs[sid], rows[sid] = spec, row
        for route in (spec.sold, spec.bought):
            if route.ticker in routes and routes[route.ticker] != route.asset:
                raise ValueError(f"Mapping {spec.cex} ambiguo: " + route.ticker)
            routes[route.ticker] = route.asset

    def record(spec, quantity, price, basis, requirements, limits=None):
        if sizing is not None:
            sizing.append({'strategy_id': spec.strategy_id,
                'number': rows[spec.strategy_id].get('creation_number', spec.strategy_id),
                'sell': spec.sold.ticker, 'buy': spec.bought.ticker,
                'quantity': str(quantity), 'price': str(price), 'basis': basis,
                'limits': limits or {},
                'targets': {a: str(q * RESERVE) for a, q in requirements.items()}})

    published = set()
    # Current liabilities take priority over future configurations. Never add
    # an unlimited replacement estimate on top of the same published maker.
    for quote in context.get('open_quotes', []):
        sid = quote['strategy_id']
        spec = specs.get(sid)
        if spec is None:
            raise ValueError('Configurazione di un ordine pubblicato non disponibile')
        sold = number(quote['volume'], 'quantità pubblicata')
        price = number(quote['price'], 'prezzo pubblicato')
        requirements = defaultdict(D)
        for route, side, amount in ((spec.sold, 'BUY', sold), (spec.bought, 'SELL', sold * price)):
            if not route.symbol:
                continue
            snapshot = snapshots[route.symbol]
            reserved_depth[(route.symbol, side)] += amount
            amount = (amount / snapshot.quantity_step).to_integral_value(rounding=ROUND_CEILING) * snapshot.quantity_step
            asset = 'USDT' if side == 'BUY' else route.asset
            if side == 'BUY':
                amount *= snapshot.order_book().asks[0].price * (1 + spec.impact)
            requirements[asset] += amount * (1 + fee)
        for asset, amount in requirements.items():
            cex[asset] += amount
        pools[spec.sold.ticker][spec.bought.ticker] += sold
        published.add(sid)
        record(spec, sold, price, 'published', requirements)

    for sid, spec in specs.items():
        if sid in published:
            continue
        row = rows[sid]
        remaining = spec.max_sold if spec.replenish else number(row['remaining_sold'], 'budget', positive=False)
        daily = (number(row.get('daily_remaining_sold', spec.daily_sold_cap), 'budget giornaliero', positive=False)
                 if spec.daily_sold_cap is not None else None)
        if remaining == 0 or daily == 0:
            continue
        # Fixed funding has an explicit finite quantity, not a wallet estimate.
        kdf_free = spec.fixed_sold
        if spec.quantity_mode == 'auto':
            wallet = context.get('wallet', {}).get(spec.sold.ticker, {})
            if 'free' not in wallet:
                raise ValueError(f'{spec.sold.ticker}: saldo KDF spendibile non disponibile; '
                                 'impossibile dimensionare il target automatico, aggiornare e riprovare')
            available = number(wallet['free'], 'saldo KDF', positive=False)
            committed = pools[spec.sold.ticker][spec.bought.ticker]
            kdf_free = max(D(0), available - committed)
        # Budget exhaustion may leave a smaller last order than fixed_sold.
        from dataclasses import replace
        if remaining is not None and spec.fixed_sold is not None:
            spec = replace(spec, fixed_sold=min(spec.fixed_sold, remaining))
        preview_args = dict(
            kdf_free=kdf_free,
            cex_free={r.asset: D('1e30') for r in (spec.base, spec.quote)} | {'USDT': D('1e30')},
            remaining_budget=remaining,
            daily_remaining=daily,
            fee=fee,
            buffer=D(context['buffer']),
            daily_volume_fraction=D(context['daily_fraction']),
            reserved_hedges=reserved_depth,
        )
        if spec.quantity_mode == 'auto':
            diagnostic = preview_strategy(spec, snapshots, diagnostics_only=True, **preview_args)
            omitted = _auto_strategy_exclusion(spec, diagnostic)
            if omitted is not None:
                if excluded is not None:
                    excluded.append(omitted)
                continue
        preview = preview_strategy(
            spec,
            snapshots,
            funding_target=spec.quantity_mode == 'fixed',
            **preview_args,
        )
        if warnings is not None and spec.quantity_mode == 'fixed':
            try:
                preview_strategy(spec, snapshots, kdf_free=spec.fixed_sold,
                    cex_free={r.asset: D('1e30') for r in (spec.base, spec.quote)} | {'USDT': D('1e30')},
                    remaining_budget=remaining, daily_remaining=daily,
                    fee=fee, buffer=D(context['buffer']),
                    daily_volume_fraction=D(context['daily_fraction']), reserved_hedges=reserved_depth)
            except ValueError:
                warnings.append(f"{spec.sold.ticker} → {spec.bought.ticker}: liquidità/minimi {spec.cex} non compatibili con la quantità fissa {preview.plan.kdf_volume}. "
                                "La riserva è calcolata sull'intera quantità configurata, senza ridurla. Riequilibrare i saldi non risolve un limite del book.")
        pools[spec.sold.ticker][spec.bought.ticker] += preview.plan.kdf_volume
        requirements = defaultdict(D)
        for leg in preview.hedge_legs:
            qty = D(leg['quantity'])
            reserved_depth[(leg['symbol'], leg['side'])] += qty
            step = D(leg['quantity_step'])
            qty = (qty / step).to_integral_value(rounding=ROUND_CEILING) * step
            asset = 'USDT' if leg['side'] == 'BUY' else leg['asset']
            requirements[asset] += qty * (D(leg['limit_price']) if asset == 'USDT' else 1) * (1 + fee)
        for asset, amount in requirements.items():
            cex[asset] += amount
        record(spec, preview.plan.kdf_volume, preview.plan.kdf_price,
               'wallet_bounded_auto' if spec.quantity_mode == 'auto' else 'configured_fixed',
               requirements, {k: str(v) for k, v in preview.caps_sold.items()})
    return ({a: q * RESERVE for a, q in cex.items()},
            {a: max(pairs.values()) for a, pairs in pools.items()}, routes)


def propose(context, snapshots, balances):
    venue = str(context.get('venue') or 'CEX')
    notes, excluded, sizing = [], [], []
    try:
        targets, kdf, routes = coverage_targets(
            context, snapshots, warnings=notes, excluded=excluded, sizing=sizing
        )
    except ValueError as exc:
        explanation = 'Analisi copertura incompleta: ' + str(exc) + ' Nessun trade o trasferimento suggerito; verificare le strategie indicate e aggiornare il mercato.'
        return {'targets': {}, 'kdf_targets': {}, 'orders': [], 'transfers': [],
                'notes': [explanation], 'strategy_actions': excluded, 'sizing': [],
                'transfer_blockers': [explanation], 'fingerprint': fingerprint(context)}
    for item in sizing:
        basis = {'published': 'quantità effettivamente pubblicata',
                 'wallet_bounded_auto': 'prossimo ordine auto, limitato dal saldo spendibile KDF e dai limiti della strategia/mercato',
                 'configured_fixed': 'quantità fissa configurata'}[item['basis']]
        amounts = ', '.join(f'{q} {a}' for a, q in item['targets'].items())
        notes.append(f"Maker #{item['number']} {item['sell']} → {item['buy']}: "
                     f"{item['quantity']} {item['sell']} ({basis}); target {amounts}, già inclusi commissioni e riserva del 20%.")
    free = {a: b['free'] for a, b in balances.items()}
    orders, transfers = [], []
    fee = D(context['fee'])
    if not fee.is_finite() or not D(0) <= fee < D('.1'):
        raise ValueError('Commissione fuori intervallo prudenziale')
    usdt = free.get('USDT', D(0)) - targets.get('USDT', D(0))

    def order(asset, side, wanted, *, maximum=None, round_up=False):
        snap = snapshots[asset + 'USDT']
        book = snap.order_book()
        levels = book.asks if side == 'BUY' else book.bids
        boundary = levels[0].price * (D('1.01') if side == 'BUY' else D('.99'))
        boundary = (boundary / snap.price_step).to_integral_value(
            rounding=ROUND_FLOOR if side == 'BUY' else ROUND_CEILING) * snap.price_step
        depth = sum((l.quantity for l in levels if
                     (l.price <= boundary if side == 'BUY' else l.price >= boundary)), D(0)) / 2
        capacity = depth if maximum is None else min(depth, maximum)
        requested = min(wanted, capacity)
        rounding = ROUND_CEILING if round_up else ROUND_FLOOR
        qty = (requested / snap.quantity_step).to_integral_value(rounding=rounding) * snap.quantity_step
        # Ceiling is appropriate when a sale must close a quote-asset deficit,
        # but it must never exceed real book depth or the available surplus.
        if qty > capacity:
            qty = (capacity / snap.quantity_step).to_integral_value(rounding=ROUND_FLOOR) * snap.quantity_step
        if qty <= 0 or qty * boundary < snap.min_quote_amount:
            notes.append(f'{asset}: quantità sotto il minimo {venue} o profondità insufficiente')
            return None
        return {'asset': asset, 'symbol': snap.symbol, 'side': side, 'quantity': str(qty),
                'price': str(boundary), 'notional': str(qty * boundary)}

    deficits = {a: max(D(0), q - free.get(a, D(0))) / (1 - fee) for a, q in targets.items() if a != 'USDT'}
    cost = sum((q * snapshots[a + 'USDT'].order_book().asks[0].price * D('1.01') * (1 + fee)
                for a, q in deficits.items()), D(0))
    shortage = max(D(0), cost - usdt)
    # Sell only strategy assets, only the excess over coverage, and only if needed.
    for asset in sorted(set(routes.values()) - {'USDT'}):
        if shortage <= 0:
            break
        surplus = max(D(0), free.get(asset, D(0)) - targets.get(asset, D(0)))
        bid = snapshots[asset + 'USDT'].order_book().bids[0].price * D('.99') * (1 - fee)
        item = order(
            asset,
            'SELL',
            shortage / bid,
            maximum=surplus / (1 + fee),
            round_up=True,
        )
        if item:
            orders.append(item)
            shortage -= D(item['notional']) * (1 - fee)
    # Buy proposals NEVER rely on unfilled sales. Recalculate after each fill.
    spend = max(D(0), usdt)
    for asset, missing in sorted(deficits.items()):
        if missing <= 0:
            continue
        ask = snapshots[asset + 'USDT'].order_book().asks[0].price * D('1.01') * (1 + fee)
        item = order(asset, 'BUY', min(missing, spend / ask))
        if item:
            orders.append(item)
            spend -= D(item['notional']) * (1 + fee)
    if shortage > D('.000001'):
        notes.append(f'Fondi insufficienti per il target: mancano circa {shortage:.6f} USDT (prima degli arrotondamenti)')
    remaining = {a: max(D(0), free.get(a, D(0)) - targets.get(a, D(0))) for a in routes.values()}
    if any(free.get(a, D(0)) < required for a, required in targets.items()):
        remaining = {a: D(0) for a in remaining}
        notes.append(f'Prima completare la copertura +20% su {venue}; nessuna eccedenza trasferibile finché esistono deficit.')
    for ticker, needed in sorted(kdf.items()) if context.get('include_transfers', True) else ():
        wallet = context['wallet'].get(ticker, {})
        if 'free' not in wallet:
            notes.append(f'{ticker}: saldo KDF non disponibile; trasferimento non calcolabile')
            continue
        deficit = max(D(0), needed - number(wallet['free'], 'saldo KDF', positive=False))
        amount = min(deficit, remaining[routes[ticker]])
        remaining[routes[ticker]] -= amount
        if deficit:
            transfers.append({'ticker': ticker, 'asset': routes[ticker], 'quantity': str(amount),
                              'uncovered': str(deficit - amount)})
    return {'targets': {a: str(q) for a, q in targets.items()}, 'kdf_targets': {a: str(q) for a, q in kdf.items()},
            'funding': {a: {'required': str(q), 'available': str(free.get(a, D(0))),
                           'missing': str(max(D(0), q - free.get(a, D(0))))} for a, q in targets.items()},
            'orders': orders, 'transfers': transfers, 'notes': notes,
            'strategy_actions': excluded,
            'sizing': sizing,
            'fingerprint': fingerprint(context)}


def assert_idle(context, journal_path):
    if Path(context['journal']).resolve() != Path(journal_path).resolve():
        raise ValueError('Journal TUI diverso dal servizio locale: correggere --desktop-journal')
    if context['orders_present'] or context['swaps_present']:
        raise ValueError('Prima mettere in pausa tutti i mercati KDF e attendere gli swap')
    blocking = [r for r in context['strategies']
                if r['enabled'] or r['state'] not in {'PAUSED', 'EXHAUSTED', 'DELETED'}]
    if blocking:
        details = '; '.join(f"{r.get('id') or r['spec']['strategy_id']}: {r['state']}"
                            + (' (abilitata)' if r['enabled'] else '') for r in blocking)
        raise ValueError('Nessun ordine aperto, ma queste strategie possono ripubblicare o richiedono verifica: '
                         + details + '. Metterle in pausa o risolverne gli avvisi prima di operare.')
    rec = context['reconciliation']
    if not 0 <= time.time() * 1000 - rec.get('last_success_ms', 0) <= 60000:
        raise ValueError('Riconciliazione KDF non aggiornata: attendere una lettura recente')
    if not rec.get('ready') or any(rec.get(k, 1) for k in
            ('active_owned_swaps', 'terminal_swaps_to_acknowledge', 'problem_orders')) or rec.get('unowned_order_uuids'):
        raise ValueError('Riconciliazione KDF incompleta: risolvere swap/ordini prima del rebalance')
    if context['repricing'].get('state') not in {'PAUSED', 'DISABLED', 'STOPPED', 'IDLE'}:
        raise ValueError('Mettere in pausa anche il repricing KDF')
    if context['repricing'].get('auto_resume', {}).get('eligible'):
        raise ValueError('Usare la pausa MANUALE del repricing, non la pausa automatica')
    from .desktop_status import DesktopJournalStatus
    health = DesktopJournalStatus(journal_path).payload()
    if (not health['available'] or health['alarms'] or health['delivery']['pending_acknowledgement']
            or any(health['hedges'][k] for k in ('attention', 'in_progress', 'pending_validation', 'validated'))):
        raise ValueError('Journal hedge non pronto o operazioni ancora pendenti')
    from urllib.parse import quote
    db = sqlite3.connect('file:' + quote(str(Path(journal_path).resolve()), safe='/') + '?mode=ro', uri=True)
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='basket_legs'").fetchone():
            if db.execute("SELECT 1 FROM basket_legs WHERE state != 'FILLED' LIMIT 1").fetchone():
                raise ValueError('Copertura multi-coin non conclusa: verificare tutte le gambe hedge')
    finally:
        db.close()


def _venue_context(context, venue, fee):
    selected = str(venue).upper()
    result = dict(context)
    result['strategies'] = [
        row for row in context.get('strategies', [])
        if str(row.get('spec', {}).get('cex', 'MEXC')).upper() == selected
    ]
    result['fee'] = str(fee)
    result['venue'] = selected
    result['open_quotes'] = [r for r in context.get('open_quotes', []) if r['cex'] == selected]
    return result


def _commission_payload(payload):
    data = payload.get('data', payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        raise ValueError('Commissioni CEX non disponibili')
    return data


class CexRebalanceService:
    def __init__(self, api, settings, journal_path, *, venue='MEXC', profile='default', client=None,
                 strategy_ids=None, public_client_factory=None):
        from .venues import normalize_cex
        self.api, self.settings, self.journal_path = api, settings, str(Path(journal_path).resolve())
        self.venue, self.profile, self._client = normalize_cex(venue), profile, client
        self.strategy_ids = None if strategy_ids is None else frozenset(strategy_ids)
        # Injected clients remain caller-owned and sequential. Production uses
        # independent public-only readers, never parallel signing clients.
        self._public_client_factory = public_client_factory
        if client is None and public_client_factory is None:
            from .exchanges import create_public_reader
            self._public_client_factory = lambda: create_public_reader(self.venue,
                base_url=getattr(self.settings, self.venue.lower() + '_base_url', None))

    @property
    def client(self):
        if self._client is None:
            from .credentials import LinuxSecretService
            secrets = LinuxSecretService(profile=self.profile)
            from .exchanges import private_client
            self._client = private_client(self.venue, secrets,
                base_url=getattr(self.settings, self.venue.lower() + '_base_url', None),
                trading_enabled=self.settings.live_trading)
        return self._client

    @property
    def configured_fee(self):
        from .exchanges.plugin_catalog import installed_plugins
        if installed_plugins() is not None:
            from .exchanges import load_config
            return D(load_config(self.venue).taker_fee)
        return (getattr(self.settings, 'gate_taker_fee', D('0.002')) if self.venue == 'GATE'
                else getattr(self.settings, 'cex_taker_fee', D('0.001')))

    def _context(self, full_context):
        context = _venue_context(full_context, self.venue, self.configured_fee)
        if self.strategy_ids is not None:
            context['strategies'] = [r for r in context['strategies'] if r['id'] in self.strategy_ids]
            if {r['id'] for r in context['strategies']} != self.strategy_ids:
                raise ValueError('Ordini maker cambiati o eliminati: ricalcolare')
            context['open_quotes'] = [r for r in context['open_quotes'] if r['strategy_id'] in self.strategy_ids]
        return context

    def _synchronize(self):
        from .exchanges import load_config
        self.client.synchronize_time(max_round_trip_ms=load_config(self.venue).time_sync_budget_ms)

    def balances(self):
        self._synchronize()
        result = free_balances(self.client.account(), venue=self.venue)
        return {'at': time.strftime('%H:%M:%S'), 'balances': {
            a: {k: str(v) for k, v in b.items()} for a, b in result.items() if any(b.values())}}

    def _refunded_swap_uuids(self, context):
        """Support a controlled rolling update of the local agent and TUI.

        New agents include durable refund proof in the context.  If the local
        agent is still on the preceding process image, the local TUI may read
        the same strategy database directly.  Failure is conservative: an
        inverse trade remains blocked rather than treating a refund as proven.
        """
        supplied = context.get('refunded_swap_uuids')
        if supplied is not None:
            return tuple(str(value) for value in supplied)
        state_db = getattr(self.settings, 'state_db', None)
        if not state_db:
            return ()
        path = Path(str(state_db) + '.strategies.sqlite3').resolve()
        if not path.is_file():
            return ()
        try:
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_consumption'"
                ).fetchone():
                    return ()
                return tuple(str(row[0]) for row in db.execute(
                    "SELECT swap_uuid FROM strategy_consumption WHERE outcome='REFUNDED'"
                ).fetchall())
        except sqlite3.Error:
            return ()

    def _market_snapshots(self, rules_by_symbol, deadline, identity):
        from .network_diagnostics import emit as diagnostic
        symbols = sorted(rules_by_symbol)
        if not symbols:
            return {}

        def read(reader, method, symbol, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f'Lettura {self.venue} troppo lenta: riprovare, nessun piano valido')
            if getattr(reader, 'supports_read_deadlines', False):
                kwargs.update(timeout=min(4.0, remaining), total_timeout=min(4.0, remaining))
            started, observed = time.monotonic(), time.time_ns() // 1_000_000
            outcome, kind = 'received', None
            diagnostic('rebalance_read_start', request_id=identity, venue=self.venue,
                       method=method, symbol=symbol, started_at_ms=observed)
            try:
                return getattr(reader, method)(symbol, **kwargs), observed
            except Exception as exc:
                outcome, kind = 'error', type(exc).__name__
                raise
            finally:
                diagnostic('rebalance_read_end', request_id=identity, venue=self.venue,
                           method=method, symbol=symbol, outcome=outcome, failure_kind=kind,
                           elapsed_ms=round((time.monotonic() - started) * 1000, 2))

        def original_time(value, requested):
            if value is None:
                return requested
            if type(value) is not int or value <= 0 or value > time.time_ns() // 1_000_000:
                raise ValueError('Timestamp di mercato non valido durante l’analisi')
            # Preserve older observations; a response must never rejuvenate data.
            return min(value, requested)

        with ExitStack() as cleanup:
            if self._public_client_factory is None:
                readers = [self.client]
            else:
                readers = []
                for _ in range(min(4, len(symbols))):
                    reader = self._public_client_factory()
                    readers.append(reader)
                    close = getattr(reader, 'close', None)
                    if callable(close):
                        cleanup.callback(close)
            lanes = [(reader, symbols[index::len(readers)])
                     for index, reader in enumerate(readers)]

            def phase(lane, method):
                reader, assigned = lane
                return {symbol: read(reader, method, symbol,
                        **({'limit': self.settings.mexc_depth_limit} if method == 'order_book' else {}))
                        for symbol in assigned}

            if len(readers) == 1:
                tickers = phase(lanes[0], 'ticker_24h')
                books = phase(lanes[0], 'order_book')
            else:
                with ThreadPoolExecutor(max_workers=len(readers), thread_name_prefix='rebalance-public') as pool:
                    # All slow metadata precedes every book. Each lane has its
                    # own plugin host: its RPC lock cannot serialize the markets.
                    tickers = {symbol: value for part in pool.map(
                        lambda lane: phase(lane, 'ticker_24h'), lanes) for symbol, value in part.items()}
                    books = {symbol: value for part in pool.map(
                        lambda lane: phase(lane, 'order_book'), lanes) for symbol, value in part.items()}
            snapshots = {}
            for symbol in symbols:
                book, requested = books[symbol]
                ticker, ticker_requested = tickers[symbol]
                rules = rules_by_symbol[symbol]
                if not book.bids or not book.asks:
                    raise ValueError(f'{symbol}: book incompleto durante l’analisi')
                snapshots[symbol] = MarketSnapshot(0, symbol,
                    original_time(book.observed_at_ms, requested),
                    tuple((str(l.price), str(l.quantity)) for l in book.bids),
                    tuple((str(l.price), str(l.quantity)) for l in book.asks),
                    number(ticker['volume'], 'volume', positive=False), D(0), D(0),
                    rules.quantity_step, rules.price_step, rules.min_quote_amount, 'local-read',
                    volume_observed_at_ms=original_time(ticker.get('_observed_at_ms'), ticker_requested))
        return snapshots

    def _check_market_freshness(self, snapshots, deadline, identity):
        from .network_diagnostics import emit as diagnostic
        now_ms = time.time_ns() // 1_000_000
        stale = []
        for symbol, snapshot in snapshots.items():
            book_age = now_ms - snapshot.observed_at_ms
            volume_age = now_ms - snapshot.volume_observed_at_ms
            reason = ('stale_book' if not 0 <= book_age <= 10000 else
                      'stale_volume' if not 0 <= volume_age <= 15000 else None)
            diagnostic('rebalance_market_check', request_id=identity, venue=self.venue,
                       symbol=symbol, now_ms=now_ms, book_age_ms=book_age, volume_age_ms=volume_age,
                       book_max_age_ms=10000, volume_max_age_ms=15000, reason=reason,
                       outcome='error' if reason else 'fresh')
            if reason:
                stale.append(symbol)
        if time.monotonic() > deadline:
            raise ValueError(f'Lettura {self.venue} troppo lenta: riprovare, nessun piano valido')
        if stale:
            raise ValueError('Dati di mercato scaduti durante l’analisi (' + ', '.join(stale) + '): riprovare')

    def preview(self):
        self._synchronize()
        full_context = self.api.get('/v1/rebalance/context')
        if full_context['repricing'].get('quotes'):
            raise ValueError('Sono presenti quotazioni legacy: rimuoverle o convertirle in strategie prima del rebalance')
        context = self._context(full_context)
        started = time.time()
        deadline, identity = time.monotonic() + 30, uuid.uuid4().hex
        balances = free_balances(self.client.account(), venue=self.venue)
        rules_by_symbol = {}
        symbols = sorted({r['spec'][key]['symbol'] for r in context['strategies']
                          for key in ('base', 'quote')} - {None})
        for symbol in symbols:
            rules = self.client.symbol_rules(symbol)
            if rules.base_asset + 'USDT' != symbol or rules.quote_asset != 'USDT':
                raise ValueError(f'Mapping {self.venue} non valido: ' + symbol)
            commissions = _commission_payload(self.client.trade_fee(symbol))
            actual_fee = max(number(commissions[k], f'commissione {self.venue}', positive=False)
                             for k in ('makerCommission', 'takerCommission'))
            if actual_fee > D(context['fee']):
                raise ValueError(f'{symbol}: commissione {self.venue} maggiore di quella configurata; aggiornare il margine commissioni')
            rules_by_symbol[symbol] = rules
        snapshots = self._market_snapshots(rules_by_symbol, deadline, identity)
        self._check_market_freshness(snapshots, deadline, identity)
        result = propose(context, snapshots, balances)
        from .hedge_unwind import protect_failed_hedge_inventory
        permitted = []
        for item in result['orders']:
            item['cex'] = self.venue
            try:
                protect_failed_hedge_inventory(
                    self.journal_path,
                    self.client,
                    item,
                    D(context['fee']),
                    refunded_swap_uuids=self._refunded_swap_uuids(context),
                )
            except ValueError as exc:
                result['notes'].append(str(exc))
                # A protected position is not surplus available for transfer.
                result['transfers'] = [row for row in result['transfers'] if row['asset'] != item['asset']]
            else:
                permitted.append(item)
        result['orders'] = permitted
        self._check_market_freshness(snapshots, deadline, identity)
        for item in result['orders']:
            item['cex'] = self.venue
        result['cex'] = self.venue
        result['market_observed_ms'] = min((s.observed_at_ms for s in snapshots.values()), default=0)
        result.update(id=uuid.uuid4().hex, expires=started + 120, at=time.strftime('%H:%M:%S'))
        return result

    def _db(self):
        path = Path(self.journal_path + '.rebalance.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL)')
        db.commit()
        return db

    def transfer_preview(self):
        # Analysis remains readable while trading is enabled. Never present an
        # actionable withdrawal amount when there may be unsettled obligations.
        plan = self.preview()
        from .rebalance_guard import assert_no_pending
        try:
            full_context = self.api.get('/v1/rebalance/context')
            assert_idle(full_context, self.journal_path)
            assert_no_pending(self.journal_path)
            context = self._context(full_context)
            if fingerprint(context) != plan['fingerprint']:
                raise ValueError('Strategie cambiate durante l’analisi: ricalcolare')
        except ValueError as exc:
            plan.setdefault('transfer_blockers', []).append(str(exc))
            plan['transfers'] = []
        return plan

    def reconcile(self):
        """Read-only recovery. Unknown submissions are never resent or forgotten."""
        db = self._db()
        try:
            for oid, raw, state in db.execute('SELECT id,payload,state FROM orders').fetchall():
                if state in TERMINAL:
                    continue
                order = json.loads(raw)
                if str(order.get('cex', 'MEXC')).upper() != self.venue:
                    continue
                result = self.client.query_order(symbol=order['symbol'], client_order_id=oid)
                if (not isinstance(result, dict) or result.get('clientOrderId') != oid
                        or result.get('symbol') != order['symbol']
                        or result.get('side') != order['side']
                        or number(result.get('origQty'), 'quantità ordine') != D(order['quantity'])):
                    raise ValueError('Identità rebalance CEX non verificabile: conservare il blocco e aggiornare lo stato')
                state = str(result.get('status', 'UNKNOWN'))
                filled = number(result.get('executedQty'), 'quantità eseguita', positive=False)
                if filled > D(order['quantity']) or (state == 'FILLED' and filled != D(order['quantity'])):
                    raise ValueError('Esito rebalance discordante: conservare il blocco e aggiornare lo stato')
                db.execute('UPDATE orders SET state=?,result=? WHERE id=?', (state, json.dumps(result), oid))
                db.commit()
            return [{'id': row[0], 'order': json.loads(row[1]), 'state': row[2]} for row in
                    db.execute('SELECT id,payload,state FROM orders ORDER BY rowid DESC LIMIT 100')
                    if str(json.loads(row[1]).get('cex', 'MEXC')).upper() == self.venue][:20]
        finally:
            db.close()

    def execute_first(self, plan):
        from urllib.parse import urlparse
        if urlparse(self.api.base_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Rebalance eseguibile solo con il servizio KDF locale')
        if not self.settings.live_trading:
            raise ValueError(f'Modalità prova: esecuzione {self.venue} disabilitata')
        if not plan['orders'] or time.time() > plan['expires']:
            raise ValueError('Proposta assente o scaduta: ricalcolare')
        from .rebalance_guard import rebalance_guard
        from .local_worker import rebalance_worker_access
        with rebalance_guard(self.journal_path + '.rebalance.lock', exclusive=True, wait_seconds=20,
                busy_message='Attesa rebalance terminata: un ciclo CEX o una pubblicazione KDF non si è ancora concluso. '
                             'Questa richiesta non ha inviato ordini. Attendere, poi eseguire una nuova analisi.'), rebalance_worker_access(self.journal_path):
            full_context = self.api.get('/v1/rebalance/context')
            assert_idle(full_context, self.journal_path)
            context = self._context(full_context)
            if fingerprint(context) != plan['fingerprint']:
                raise ValueError('Strategie cambiate: ricalcolare la proposta')
            fresh = self.preview()
            item = plan['orders'][0]
            if fresh['fingerprint'] != plan['fingerprint'] or not fresh['orders'] or item != fresh['orders'][0]:
                raise ValueError('Prezzo, saldo o quantità cambiati: ricalcolare e confermare di nuovo')
            account = self.client.account()
            if not account.get('canTrade') or self.client.open_orders():
                raise ValueError(f'Trading non disponibile o ordini {self.venue} ancora aperti')
            allowed = self.client.self_symbols().get('data', [])
            if item['symbol'] not in allowed:
                raise ValueError(f'Coppia {self.venue} non autorizzata per questa API')
            rules = self.client.symbol_rules(item['symbol'])
            if (not rules.allows(HedgeSide(item['side'])) or 'LIMIT' not in rules.order_types
                    or (rules.max_quote_amount is not None and D(item['notional']) > rules.max_quote_amount)):
                raise ValueError(f'Ordine non consentito dalle regole {self.venue}')
            balances = free_balances(account, venue=self.venue)
            fee = D(context['fee'])
            spending_asset = 'USDT' if item['side'] == 'BUY' else item['asset']
            required = (D(item['notional']) * (1 + fee) if item['side'] == 'BUY' else D(item['quantity']) * (1 + fee))
            if balances.get(spending_asset, {}).get('free', D(0)) < required + D(fresh['targets'].get(spending_asset, '0')):
                raise ValueError('Saldo cambiato: la riserva di copertura non può essere utilizzata')
            if time.time() > plan['expires'] or time.time() > fresh['expires'] - 90:
                raise ValueError('Verifiche troppo lente: ricalcolare la proposta')
            # Recheck after all other preflight reads and just before the
            # durable order intent. No losing unwind is ever submitted.
            from .hedge_unwind import protect_failed_hedge_inventory
            protect_failed_hedge_inventory(
                self.journal_path,
                self.client,
                item,
                fee,
                refunded_swap_uuids=self._refunded_swap_uuids(context),
            )
            if (time.time() > plan['expires'] or time.time() > fresh['expires'] - 90
                    or time.time_ns() // 1000000 - fresh.get('market_observed_ms', 0) > 10000):
                raise ValueError('Mercato scaduto durante le verifiche finali: ricalcolare e confermare di nuovo')
            db = self._db()
            oid = 'rb' + plan['id'][:28]
            try:
                if db.execute('SELECT 1 FROM orders WHERE id=?', (oid,)).fetchone():
                    raise ValueError('Questa proposta è già stata inviata: aggiornare lo stato')
                if any(r[0] not in TERMINAL for r in db.execute('SELECT state FROM orders')):
                    raise ValueError('Ordine rebalance pendente: aggiornare lo stato, non reinviare')
                db.execute('INSERT INTO orders VALUES (?,?,?,?)', (oid, json.dumps(item), 'SUBMITTING', '{}'))
                db.commit()  # durable identity BEFORE the remote write
                try:
                    response = self.client.place_limit_order(symbol=item['symbol'], side=HedgeSide(item['side']),
                        quantity=D(item['quantity']), price=D(item['price']), client_order_id=oid)
                    # An ACK is not fill proof. Only a later identity-checked
                    # query may release the durable publication/worker guard.
                    state = 'SUBMITTED'
                    db.execute('UPDATE orders SET state=?,result=? WHERE id=?', (state, json.dumps(response), oid))
                    db.commit()
                except Exception:
                    # SUBMITTING remains blocking even for an ambiguous transport error.
                    raise ValueError('Esito da verificare: usare Aggiorna stato. NON reinviare la proposta.') from None
                return f'{oid}: {state}. Ricalcolare dopo esecuzione completa; nessun trasferimento effettuato.'
            finally:
                db.close()


class MexcRebalanceService(CexRebalanceService):
    def __init__(self, api, settings, journal_path, *, profile='default', client=None):
        super().__init__(api, settings, journal_path, venue='MEXC', profile=profile, client=client)


class GateRebalanceService(CexRebalanceService):
    def __init__(self, api, settings, journal_path, *, profile='default', client=None):
        super().__init__(api, settings, journal_path, venue='GATE', profile=profile, client=client)
