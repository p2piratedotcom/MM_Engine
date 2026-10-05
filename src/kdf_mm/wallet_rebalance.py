"""Wallet-facing, server-owned rebalance proposals using the TUI trade policy."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import replace

from .rebalance import CexRebalanceService, agent_context, assert_idle
from .rebalance_guard import assert_no_pending
from .strategy import StrategySpec
from .venues import normalize_cex


class _LocalContext:
    base_url = 'http://127.0.0.1'

    def __init__(self, read):
        self.read = read

    def get(self, path):
        if path != '/v1/rebalance/context':
            raise ValueError('Contesto rebalance non valido')
        return self.read()


class WalletRebalance:
    def __init__(self, controller, strategies, reconciliation, repricing, settings, *, profile):
        self.controller = controller
        self.strategies = strategies
        self.reconciliation = reconciliation
        self.repricing = repricing
        self.settings = settings
        self.profile = profile
        self.journal = str(settings.desktop_journal_db)
        self._lock = threading.Lock()
        self._plans = {}
        self._api = _LocalContext(self._context)

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise ValueError('Ribilanciamento già in corso: attendere e aggiornare lo stato')
        try:
            yield
        finally:
            self._lock.release()

    def _context(self, *, include_wallet=True):
        # Auto targets need spendable KDF balances even for Spot-only analysis.
        # Scope and execution blockers do not need the additional balance reads.
        with self.strategies.lock, self.controller._order_lock:
            result = agent_context(self.controller, self.strategies,
                self.reconciliation, self.repricing, include_wallet=include_wallet,
                include_transfers=False)
            result['open_quotes'] = []
            by_id = {r['id']: r for r in result['strategies']}
            for order in self.controller.ownership.active():
                sid = self.strategies.store.strategy_for_order(order.order_uuid)
                if sid not in by_id or order.status.value != 'OPEN':
                    continue
                result['open_quotes'].append({'strategy_id': sid,
                    'cex': by_id[sid]['spec']['cex'],
                    'volume': str(self.strategies._coverage_volume(order)),
                    'price': str(order.kdf_price)})
            return result

    def _service(self, venue, ids=None, *, execution=False, asset_percentages=None, allocation_id=None):
        # Analysis/status share the read-only credential lane, leaving the
        # live hedge client's pool free. Execution runs under the exclusive
        # gate, where operational worker cycles cannot overlap it.
        settings = self.settings if execution else replace(self.settings, live_trading=False)
        return CexRebalanceService(self._api, settings, self.journal,
            venue=venue, profile=self.profile, strategy_ids=ids,
            asset_percentages=asset_percentages, allocation_id=allocation_id)

    def store_credentials(self, venue, store):
        # A proposal belongs to the account whose balance was analyzed.
        # Serialize replacement with analysis/execution and discard its plans.
        with self._operation():
            self._plans = {pid: value for pid, value in self._plans.items()
                           if value[0]['cex'] != venue}
            from .rebalance_allocation import invalidate
            invalidate(self._service(venue))
            store()

    def _blockers(self, context):
        result = []
        try:
            assert_idle(context, self.journal)
            assert_no_pending(self.journal)
        except ValueError as exc:
            result.append(str(exc))
        if not self.settings.live_trading:
            result.append('Attivare la modalità live per eseguire operazioni Spot; i maker devono restare in pausa.')
        return result

    def analyze(self, payload):
        with self._operation():
            venue = normalize_cex(payload.get('venue'))
            context = self._context(include_wallet=False)
            candidates = {r['id']: r for r in context['strategies']
                if r['spec']['cex'] == venue and r['state'] != 'DELETED'}
            supplied = payload.get('strategy_ids')
            if supplied is None:
                opened = {r['strategy_id'] for r in context['open_quotes'] if r['cex'] == venue}
                ids = {sid for sid, row in candidates.items() if row['enabled'] or sid in opened}
                # After a manual pause the same configurations can be funded.
                if not ids:
                    ids = set(candidates)
            else:
                if (not isinstance(supplied, list) or not 1 <= len(supplied) <= 200
                        or any(not isinstance(s, str) or s not in candidates for s in supplied)
                        or len(set(supplied)) != len(supplied)):
                    raise ValueError('Selezione maker cambiata: avviare una nuova analisi')
                ids = set(supplied)
            if not ids:
                raise ValueError('Nessun ordine maker configurato per questo CEX')
            policy = None
            allocation_id = payload.get('allocation_id')
            if 'asset_percentages' in payload:
                from .rebalance_allocation import percentages
                policy = percentages(payload['asset_percentages'])
                if supplied is None:
                    raise ValueError('Selezionare esplicitamente gli ordini maker')
                if allocation_id is not None and (not isinstance(allocation_id, str) or len(allocation_id) != 32):
                    raise ValueError('Identità budget CEX non valida')
            elif allocation_id is not None:
                raise ValueError('Percentuali del budget CEX mancanti')
            if policy is not None and allocation_id is None:
                self._plans = {key: value for key, value in self._plans.items() if value[0]['cex'] != venue}
            service = self._service(venue, ids, asset_percentages=policy, allocation_id=allocation_id)
            plan = service.preview()
            plan['strategy_ids'] = sorted(ids)
            plan['maker_orders'] = []
            for sid in sorted(ids):
                spec = StrategySpec.from_payload(candidates[sid]['spec'])
                plan['maker_orders'].append({'number': candidates[sid]['creation_number'],
                    'sell': spec.sold.ticker, 'buy': spec.bought.ticker})
            plan['reserve_percent'] = 20
            plan['scope'] = 'open_and_enabled' if supplied is None and any(candidates[s]['enabled'] for s in ids) else 'selected_configurations'
            plan['execution_blockers'] = self._blockers(self._context(include_wallet=False))
            plan['pause_required'] = any(r['enabled'] for r in self.strategies.status()['strategies'])
            plan['can_execute'] = bool(plan['orders']) and not plan['execution_blockers']
            plan.pop('transfers', None)
            # No client-provided prices, quantities, fee settings or account IDs
            # are accepted by execute. Proposals expire and are single-use.
            now = time.time()
            self._plans = {key: value for key, value in self._plans.items() if value[0]['expires'] > now}
            if len(self._plans) >= 8:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan['id']] = (plan, ids)
            return plan

    def execute(self, payload):
        with self._operation():
            venue = normalize_cex(payload.get('venue'))
            pid = payload.get('id')
            if not isinstance(pid, str) or payload.get('confirmation') != 'EXECUTE REBALANCE ' + pid:
                raise ValueError('Confermare l’operazione mostrata prima dell’esecuzione')
            stored = self._plans.get(pid)
            if stored is None or stored[0]['cex'] != venue:
                raise ValueError('Proposta assente, già utilizzata o servizio riavviato: ricalcolare')
            plan, ids = self._plans.pop(pid)
            allocation = plan.get('allocation')
            service = self._service(venue, ids, execution=True,
                asset_percentages=allocation['percentages'] if allocation else None,
                allocation_id=allocation['id'] if allocation else None)
            message = service.execute_first(plan)
            return {'message': message, 'orders': self._history(service), 'reanalyze_required': True}

    @staticmethod
    def _history(service):
        db = service._db()
        try:
            import json
            rows = []
            for oid, raw, state in db.execute('SELECT id,payload,state FROM orders ORDER BY rowid DESC'):
                item = json.loads(raw)
                if item.get('cex', 'MEXC') == service.venue:
                    rows.append({'id': oid, 'order': item, 'state': state})
                if len(rows) == 20:
                    break
            return rows
        finally:
            db.close()

    def status(self, payload):
        with self._operation():
            venue = normalize_cex(payload.get('venue'))
            service = self._service(venue)
            errors = []
            try:
                from .rebalance import TERMINAL
                if any(r['state'] not in TERMINAL for r in self._history(service)):
                    service._synchronize()
                    service.reconcile()
            except Exception:
                # Signed URLs/exchange payloads must never enter wallet errors.
                errors.append('Esito CEX non verificabile ora. Non reinviare: riprovare Aggiorna stato.')
            from .rebalance_allocation import latest
            try:
                allocation = latest(service)
            except Exception:
                allocation = None
                errors.append('Budget CEX non verificabile: risolvere lo stato prima di una nuova esecuzione.')
            return {'venue': venue, 'orders': self._history(service), 'errors': errors,
                    'allocation': allocation}
