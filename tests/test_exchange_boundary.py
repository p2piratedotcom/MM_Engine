"""No real credentials, orders or exchange traffic."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from decimal import Decimal

from kdf_mm.exchanges import create_client, load_config, supported_venues
from kdf_mm.exchanges.balances import PreviewBalances
from kdf_mm.mexc import MexcError
from kdf_mm.strategy_service import StrategyService
from kdf_mm.venues import market_data_key
from kdf_mm.desktop_coverage import DesktopCoveragePublisher


class ExchangeBoundaryTests(unittest.TestCase):
    def client(self):
        client = Mock()
        client.self_symbols.return_value = {'data': ['ARRRUSDT']}
        client.account.return_value = {'canTrade': True, 'accountType': 'SPOT',
            'balances': [{'asset': 'USDT', 'free': '100'}]}
        return client

    def test_configs_create_read_only_adapters_and_preserve_keys(self):
        for venue in supported_venues():
            client = create_client(venue)
            self.assertFalse(client.trading_enabled)
            self.assertGreater(load_config(venue).private_read_timeout, 2)
        self.assertEqual(market_data_key('MEXC', 'ARRRUSDT'), 'ARRRUSDT')
        self.assertEqual(market_data_key('GATE', 'ARRRUSDT'), 'GATE:ARRRUSDT')

    def test_preview_reads_latest_credentials_and_caches_only_fresh_balances(self):
        now = [1.0]
        factory = Mock(return_value=self.client())
        preview = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=factory, clock=lambda: now[0])
        first = preview('MEXC')
        self.assertFalse(first['live_hedging_enabled'])
        self.assertEqual(first['free_balances']['USDT'], '100')
        preview('MEXC')
        self.assertEqual(factory.call_count, 1)
        now[0] += 4
        preview('MEXC')
        self.assertEqual(factory.call_count, 2)
        self.assertIs(factory.call_args.kwargs['trading_enabled'], False)

    def test_gate_preview_does_not_require_mexc_keys(self):
        factory = Mock(return_value=self.client())
        preview = PreviewBalances(keyring_factory=lambda: object(), client_factory=factory)
        result = preview('GATE')
        self.assertEqual(result['free_balances']['GATE:USDT'], '100')
        self.assertEqual(factory.call_args.args[0], 'GATE')

    def test_slow_balance_read_is_not_freshened_after_completion(self):
        now = [1.0]
        client = self.client()
        def slow(**kwargs):
            now[0] += 11
            return {'canTrade': True, 'accountType': 'SPOT', 'balances': []}
        client.account.side_effect = slow
        preview = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=lambda *a, **k: client, clock=lambda: now[0])
        with self.assertRaisesRegex(ValueError, 'saldo Spot non disponibile'):
            preview('MEXC')

    def test_exchange_payloads_and_credentials_are_never_echoed(self):
        client = self.client()
        client.account.side_effect = MexcError('secret signed URL', status=401,
            payload={'key': 'private-key'})
        preview = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=lambda *a, **k: client)
        with self.assertRaises(ValueError) as ctx:
            preview('MEXC')
        self.assertIn('HTTP 401', str(ctx.exception))
        self.assertNotIn('secret', str(ctx.exception))
        self.assertNotIn('private-key', str(ctx.exception))

    def test_read_only_provider_cannot_satisfy_live_publication_preview(self):
        service = object.__new__(StrategyService)
        service.controller = SimpleNamespace(coverage=Mock())
        service.controller.coverage.status.return_value = {'lease_fresh': False}
        service.preview_balances = Mock(side_effect=ValueError('read-only provider called'))
        spec = SimpleNamespace(cex='MEXC')
        with self.assertRaisesRegex(ValueError, 'aggiornato richiesto'):
            service._preview(spec)
        service.preview_balances.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'read-only provider called'):
            service._preview(spec, allow_read_only=True)

    def test_balance_display_needs_no_symbols_or_trade_permission(self):
        client = self.client()
        client.self_symbols.side_effect = AssertionError('must not read trading permissions')
        client.account.return_value['canTrade'] = False
        reader = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=lambda *a, **k: client)
        self.assertEqual(reader.read_account('MEXC'), {'USDT': Decimal('100')})
        client.self_symbols.assert_not_called()
        with self.assertRaises(Exception):
            DesktopCoveragePublisher._balances(client.account.return_value)

    def test_preview_filters_unsupported_pairs_without_discarding_valid_pairs(self):
        client = self.client()
        client.self_symbols.return_value = {'data': ['ARRRUSDT', '币USDT', 'BAD_PAIR']}
        reader = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=lambda *a, **k: client)
        self.assertEqual(reader('MEXC')['hedge_symbols'], ['ARRRUSDT'])

    def test_read_only_empty_account_is_valid(self):
        client = self.client()
        client.account.return_value['balances'] = []
        reader = PreviewBalances(keyring_factory=lambda: object(),
            client_factory=lambda *a, **k: client)
        self.assertEqual(reader.read_account('MEXC'), {})

    def test_gate_only_coverage_has_no_fictitious_mexc_client(self):
        client = self.client()
        publisher = DesktopCoveragePublisher(clients={'GATE': client},
            vps=Mock(), event_secret='x'*48, consumer_id='test', assets=('USDT',))
        self.assertEqual(set(publisher.clients), {'GATE'})


if __name__ == '__main__':
    unittest.main()
