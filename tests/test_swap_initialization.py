import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from kdf_mm.models import DexSide, HedgeSide, QuotePlan
from kdf_mm.ownership import (
    OrderOwnershipStore,
    OwnedOrderStatus,
    OwnedSwapState,
)
from kdf_mm.reconciliation import KdfReconciler, KdfReconciliationError, KdfSwapInitializationPending
from kdf_mm.kdf import KdfError
from unittest.mock import Mock
from types import SimpleNamespace
from kdf_mm.strategy_service import StrategyService
from kdf_mm.strategy import AssetRoute


D = Decimal


def plan(side: DexSide = DexSide.SELL_ARRR) -> QuotePlan:
    if side is DexSide.SELL_ARRR:
        base, rel, price, volume = "ARRR", "USDT-BEP20", D("1.1"), D("5")
        hedge_side = HedgeSide.BUY
    else:
        base, rel, price, volume = "USDT-BEP20", "ARRR", D("0.9"), D("5.5")
        hedge_side = HedgeSide.SELL
    return QuotePlan(
        dex_side=side,
        hedge_side=hedge_side,
        arrr_quantity=D("5"),
        reference_vwap=D("1"),
        cex_limit_price=D("1.01"),
        human_price_usdt_per_arrr=D("1.1"),
        kdf_base=base,
        kdf_rel=rel,
        kdf_price=price,
        kdf_volume=volume,
        effective_edge=D("0.1"),
    )


def observed_order(*, started_swaps=None, price="1.2", available="4"):
    return {
        "uuid": "order-1",
        "base": "ARRR",
        "rel": "USDT-BEP20",
        "price": price,
        "available_amount": available,
        "started_swaps": [] if started_swaps is None else started_swaps,
    }


def maker_swap(*, finished=False, success=None, event="MakerPaymentSent"):
    result = {
        "type": "Maker",
        "uuid": "swap-1",
        "my_order_uuid": "order-1",
        "maker_coin": "ARRR",
        "taker_coin": "USDT-BEP20",
        "maker_amount": "2",
        "taker_amount": "2.4",
        "is_finished": finished,
        "events": [{"event": {"type": event}}],
    }
    if success is not None:
        result["is_success"] = success
    return result


class FakeKdf:
    def __init__(self):
        self.orders = {"order-1": observed_order()}
        self.active = {"uuids": [], "statuses": {}}
        self.recent = {"swaps": []}
        self.statuses = {}
        self.status_calls = []
        self.order_history = {}

    def my_orders(self):
        return {"maker_orders": self.orders, "taker_orders": {}}

    def active_swaps(self, *, include_status=True):
        assert include_status is True
        return self.active

    def recent_swaps(self, *, limit):
        assert limit == 100
        return self.recent

    def swap_status(self, swap_uuid):
        self.status_calls.append(swap_uuid)
        return self.statuses[swap_uuid]

    def order_status(self, order_uuid):
        if order_uuid not in self.order_history:
            raise RuntimeError("order not found")
        return self.order_history[order_uuid]


class InitializationTests(TestCase):
    def setUp(self):
        self.store = OrderOwnershipStore(":memory:")
        self.addCleanup(self.store.close)
        self.order = self.store.register("order-1", plan())
        self.kdf = FakeKdf()
        self.clock = 100000
        self.reconciler = KdfReconciler(kdf=self.kdf, ownership=self.store,
                                      clock_ms=lambda: self.clock)
        self.reconciler.reconcile_once()
        self.pause = Mock()
        self.reconciler.set_pause_callback(self.pause)
        self.kdf.active = {"uuids": ["new-swap"], "statuses": {}}
        self.kdf.swap_status = Mock(side_effect=KdfError(
            "HTTP 500", payload={"error": "swap data is not found"}))

    def test_unknown_initializing_swap_freezes_writes_without_withdrawal(self):
        with self.assertRaises(KdfSwapInitializationPending):
            self.reconciler.reconcile_once()
        self.pause.assert_not_called()
        self.assertFalse(self.reconciler.payload()["ready"])
        self.assertEqual(self.reconciler.payload()["initializing_swap_uuids"], ["new-swap"])
        self.assertTrue(self.reconciler.initialization_pending_for_quote(
            self.order.market_id, self.order.dex_side))
        self.assertEqual(self.store.get("order-1").status, OwnedOrderStatus.OPEN)
        self.kdf.active = {"uuids": ["new-swap"], "statuses": {
            "new-swap": {"uuid": "new-swap", "type": "Taker", "my_order_uuid": "wallet-taker"}}}
        self.assertTrue(self.reconciler.reconcile_once()["ready"])
        self.assertEqual(self.reconciler.payload()["initializing_swap_uuids"], [])
        self.assertEqual(self.store.swaps(), ())

    def test_persistent_missing_status_fails_closed(self):
        with self.assertRaises(KdfSwapInitializationPending): self.reconciler.reconcile_once()
        self.clock += 30000
        with self.assertRaisesRegex(KdfReconciliationError, "beyond 30 seconds"):
            self.reconciler.reconcile_once()
        self.pause.assert_called_once()
        self.assertFalse(self.reconciler.initialization_pending_for_quote(
            self.order.market_id, self.order.dex_side))

    def test_started_owned_maker_never_uses_initialization_grace(self):
        self.kdf.orders["order-1"]["started_swaps"] = ["new-swap"]
        with self.assertRaises(KdfReconciliationError): self.reconciler.reconcile_once()
        self.pause.assert_called_once()
        self.assertEqual(self.reconciler.payload()["initializing_swap_uuids"], [])

    def test_other_http_500_and_transport_failures_are_not_ignored(self):
        for payload in ({"error": "database disk image is malformed"}, None):
            with self.subTest(payload=payload):
                self.pause.reset_mock()
                self.kdf.swap_status.side_effect = KdfError("failed", payload=payload)
                with self.assertRaises(KdfReconciliationError): self.reconciler.reconcile_once()
                self.pause.assert_called_once()

    def test_known_taker_needs_no_canonical_status_lookup(self):
        self.kdf.recent = {"swaps": [{"uuid": "new-swap", "type": "Taker",
                                     "my_order_uuid": "wallet-taker"}]}
        self.assertTrue(self.reconciler.reconcile_once()["ready"])
        self.kdf.swap_status.assert_not_called()

    def test_unknown_resolving_to_owned_maker_is_reconciled_and_blocks(self):
        with self.assertRaises(KdfSwapInitializationPending): self.reconciler.reconcile_once()
        status = dict(maker_swap(), uuid="new-swap")
        self.kdf.active["statuses"] = {"new-swap": status}
        self.assertTrue(self.reconciler.reconcile_once()["ready"])
        self.assertEqual(self.store.get_swap("new-swap").state, OwnedSwapState.ACTIVE)
        self.assertIsNotNone(self.reconciler.block_quote(self.order.market_id, self.order.dex_side))
        self.assertFalse(self.reconciler.initialization_pending_for_quote(self.order.market_id, self.order.dex_side))

    def test_strategy_hold_checks_exposure_without_any_writes(self):
        service = object.__new__(StrategyService)
        service.controller = Mock()
        service.controller.enabled_tickers.return_value = ("ARRR", "USDT-BEP20")
        service.controller.coverage.block_reason.return_value = None
        service.repricing = Mock()
        service.repricing.payload.return_value = {"quotes": {}}
        service.reconciliation = Mock()
        service.reconciliation.block_quote.return_value = "initializing"
        service.reconciliation.initialization_pending_for_quote.return_value = True
        service.settlement = None
        service.store = Mock()
        service.orders_for = Mock(return_value=(self.order,))
        spec = SimpleNamespace(strategy_id="s", market_id=self.order.market_id, side=self.order.dex_side)
        service._cycle(spec, {})
        service.controller._assert_market_fresh.assert_called_once()
        service.controller._assert_pool_capacity.assert_called_once()
        service.controller.coverage_requirements.assert_called_once()
        service.controller.update_owned_quote.assert_not_called()
        service.controller.publish.assert_not_called()
        self.assertEqual(service.store.update.call_args.kwargs["state"], "STABILIZING")
        service.controller.coverage.block_reason.return_value = "hedge capacity expired"
        with self.assertRaisesRegex(ValueError, "hedge capacity expired"):
            service._cycle(spec, {})

    def test_wallet_network_ticker_retains_case_and_exact_identifier(self):
        route = AssetRoute('BTC-segwit', 'BTC', 'BTCUSDT')
        self.assertEqual(route.ticker, 'BTC-segwit')
        with self.assertRaises(ValueError): AssetRoute('BTC-segwit', 'btc', 'btcUSDT')

    def test_legacy_rpc_error_context_is_classified_without_ignoring_other_errors(self):
        self.kdf.swap_status.side_effect = KdfError('HTTP 500', payload={
            'error': 'rpc:198] RPC call failed: swap data is not found'})
        with self.assertRaises(KdfSwapInitializationPending): self.reconciler.reconcile_once()
        self.pause.assert_not_called()
        self.kdf.swap_status.side_effect = KdfError('HTTP 500', payload={
            'error': 'rpc:198] RPC call failed: database read failed; swap data is not found'})
        with self.assertRaises(KdfReconciliationError): self.reconciler.reconcile_once()
        self.pause.assert_called_once()

    def test_missing_index_grace_requires_matching_uuid(self):
        self.kdf.swap_status.side_effect = KdfError('HTTP 500', payload={
            'error': 'rpc:198] RPC call failed: lp_swap:1138] No swap with uuid new-swap'})
        with self.assertRaises(KdfSwapInitializationPending): self.reconciler.reconcile_once()
        self.pause.assert_not_called()
        self.kdf.swap_status.side_effect = KdfError('HTTP 500', payload={
            'error': 'No swap with uuid another-swap'})
        with self.assertRaises(KdfReconciliationError): self.reconciler.reconcile_once()
        self.pause.assert_called_once()
