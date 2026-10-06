"""Maker invariants across strategies and interrupted publication; offline only."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import patch
import threading
import unittest

from kdf_mm.models import QuotePlan, DexSide, HedgeSide
from kdf_mm.ownership import OrderOwnershipStore
from kdf_mm.publication_recovery import PublicationRecovery
from kdf_mm.cancellation_recovery import CancellationRecovery
from kdf_mm.strategy_store import StrategyStore
from kdf_mm.strategy_service import StrategyService
from kdf_mm.vps_controller import VpsController, InventoryLimitError


def quote(**overrides):
    fields = dict(dex_side=DexSide.SELL_ARRR, hedge_side=HedgeSide.BUY,
        arrr_quantity=D(7), reference_vwap=D(1), cex_limit_price=D(1),
        human_price_usdt_per_arrr=D(1), kdf_base='ARRR', kdf_rel='LTC',
        kdf_price=D(1), kdf_volume=D(7), effective_edge=D(0),
        market_id='ARRR-LTC', inventory_pool='ARRR', strategy_id='maker-a')
    return QuotePlan(**(fields | overrides))


class MakerIntentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.ownership = OrderOwnershipStore(':memory:')
        self.store = StrategyStore(':memory:')
        self.addCleanup(self.ownership.close)
        self.addCleanup(self.store.close)
        self.publications = PublicationRecovery(self.ownership)
        self.controller = NS(ownership=self.ownership, publications=self.publications,
            cancellations=CancellationRecovery(self.ownership),
            kdf=NS(max_maker_volume=lambda coin: {'volume':'10'}))
        self.service = StrategyService.__new__(StrategyService)
        self.service.controller, self.service.store = self.controller, self.store
        self.service.lock = threading.RLock()
        self.service._recovery_checked = {}
        self.service._specs = {'maker-a':NS(strategy_id='maker-a')}
        self.service.status = lambda: {'strategies': self.store.rows()}
        # Cancellation itself is covered separately. This seam exercises real
        # bulk selection, individual pause, journal hold and readback recovery.
        self.service._cancel = lambda *args, **kwargs: None

    def test_publication_guard_sums_different_pairs_in_same_pool(self):
        self.ownership.register('existing', quote())
        for strategy_id in ('maker-b', ''):
            with self.subTest(strategy_id=strategy_id), self.assertRaises(InventoryLimitError):
                VpsController._assert_pool_capacity(self.controller,
                    quote(kdf_rel='DOGE', market_id='ARRR-DOGE', strategy_id=strategy_id))

    def test_update_excludes_only_replaced_uuid_not_other_pair(self):
        self.ownership.register('existing', quote())
        self.ownership.register('replaced', quote(kdf_rel='DOGE', kdf_volume=D(2)))
        candidate = quote(kdf_rel='DOGE', kdf_volume=D(3))
        VpsController._assert_pool_capacity(self.controller, candidate, excluding_order_uuid='replaced')
        with self.assertRaises(InventoryLimitError):
            VpsController._assert_pool_capacity(self.controller, replace(candidate, kdf_volume=D(4)), excluding_order_uuid='replaced')

    def test_different_sold_pool_does_not_consume_candidate_inventory(self):
        self.ownership.register('other', quote(kdf_base='DASH', inventory_pool='DASH'))
        VpsController._assert_pool_capacity(self.controller, quote())

    def test_preview_and_publication_use_same_pool_scope(self):
        self.ownership.register('existing', quote())
        spec = NS(strategy_id='maker-b', cex='MEXC', base=NS(asset='ARRR',symbol=None),
            quote=NS(asset='USDT',symbol=None), sold=NS(ticker='ARRR'),
            bought=NS(ticker='DOGE'), quantity_mode='auto', replenish=False)
        self.controller.coverage = NS(status=lambda _: {'lease_fresh':True,
            'hedge_symbols':[], 'free_balances':{'ARRR':'10','USDT':'10'}})
        self.controller.market_data_by_symbol = {}
        self.controller.risk_buffer = D(0)
        self.controller.max_daily_volume_fraction = D('0.01')
        self.service.store.remaining = lambda _: (None, None)
        self.service.coverage_requirement = lambda *args, **kwargs: {}
        self.service._for_quote = lambda _: spec
        self.service._legs = lambda *args: []
        self.service._coverage_key = lambda _, asset: asset
        self.service._fee = lambda _: D(0)
        with patch('kdf_mm.strategy_service.preview_strategy', return_value=NS(plan=quote())) as build:
            self.service._preview(spec, diagnostics_only=True)
        self.assertEqual(build.call_args.kwargs['kdf_free'], D(3))

    def pending_publication(self, *, minimum=None, registered=False):
        self.store.db.execute("INSERT INTO strategies(id,spec,enabled,state) VALUES ('maker-a','{}',0,'REVIEW_REQUIRED')")
        plan = quote()
        identity = self.publications.begin(plan, {}, minimum)
        intent = self.publications.pending('maker-a')[0]
        uid = 'late-order'
        snapshot = {uid: {'uuid':uid, 'base':'ARRR', 'rel':'LTC',
            'created_at':intent['requested_at_ms'], 'price':'1', 'max_base_vol':'7',
            'available_amount':'7', 'min_base_vol':str(minimum or D(0)),
            'matches':{}, 'started_swaps':[]}}
        self.controller.kdf._maker_order_snapshot = lambda **kwargs: snapshot
        if registered:
            self.ownership.register(uid, plan, min_volume=minimum)
            self.publications.state(identity, 'REGISTERED', order_uuid=uid)
        return uid, identity

    def test_pause_all_holds_disabled_pending_publication_before_late_readback(self):
        uid, identity = self.pending_publication()
        result = self.service.set_all_enabled(False)
        self.assertEqual(result['changed_strategy_ids'], ['maker-a'])
        state = self.ownership.connection.execute('SELECT state FROM publication_intents WHERE id=?',(identity,)).fetchone()[0]
        self.assertEqual(state, 'HELD')
        self.assertFalse(self.service._recover_publication(NS(strategy_id='maker-a'), self.store.get('maker-a')))
        self.assertEqual(self.store.get('maker-a')['enabled'], 0)
        self.assertIsNone(self.store.strategy_for_order(uid))

    def test_recovery_preserves_minimum_after_crash_between_register_and_bind(self):
        uid, identity = self.pending_publication(minimum=D(1), registered=True)
        self.assertTrue(self.service._recover_publication(NS(strategy_id='maker-a'), self.store.get('maker-a')))
        self.assertEqual(self.store.get('maker-a')['state'], 'RECOVERING')
        self.assertEqual(self.store.strategy_for_order(uid), 'maker-a')
        self.assertEqual(self.ownership.get(uid).kdf_min_volume, D(1))
        self.assertEqual(self.publications.pending('maker-a'), [])

    def test_first_readback_registration_preserves_minimum(self):
        uid, _ = self.pending_publication(minimum=D(1))
        self.service._recover_publication(NS(strategy_id='maker-a'), self.store.get('maker-a'))
        self.assertEqual(self.ownership.get(uid).kdf_min_volume, D(1))


if __name__ == '__main__':
    unittest.main()
