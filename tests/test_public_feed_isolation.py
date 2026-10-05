"""No network, keys, wallet or trading: exercise slow public-reader queues."""
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kdf_mm.exchanges import create_public_reader
from kdf_mm.exchanges.plugin_client import SpotPluginClient
from kdf_mm.market_data import MarketDataStore, StaleMarketData
from kdf_mm.models import OrderBook
from kdf_mm.public_feed import MexcPublicFeed, MexcPublicFeedError


class Reader:
    def __init__(self, clock):
        self.clock = clock
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.closed = False
        self.serial = threading.Lock()

    def symbol_rules(self, symbol):
        return SimpleNamespace(quantity_step=Decimal('.01'),
                               min_quote_amount=Decimal('1'), price_step=Decimal('.001'))

    def ticker_24h(self, symbol):
        with self.serial:
            if self.block:
                self.entered.set()
                if not self.release.wait(2):
                    raise TimeoutError('simulated metadata deadline')
            return {'volume': '1234'}

    def order_book(self, symbol, *, limit):
        # Model the stdio host's serialized requests. Sharing this reader
        # with metadata makes the depth call time out before reaching an API.
        if not self.serial.acquire(timeout=.2):
            raise TimeoutError('depth queued behind metadata')
        try:
            return OrderBook.from_mexc({'bids': [['1', '20']], 'asks': [['1.01', '20']]},
                                       observed_at_ms=self.clock[0])
        finally:
            self.serial.release()

    def close(self):
        self.closed = True


class PublicFeedIsolationTests(TestCase):
    def make_feed(self, symbol='ARRRUSDT', venue='MEXC'):
        now = [10_000]
        readers = []
        def factory():
            reader = Reader(now)
            readers.append(reader)
            return reader
        store = MarketDataStore(symbol=symbol, secret='test-only', max_age_ms=10_000,
                                clock_ms=lambda: now[0])
        feed = MexcPublicFeed(client=None, client_factory=factory, store=store,
            snapshot_secret='test-only', symbol=symbol, venue=venue,
            clock_ms=lambda: now[0])
        self.addCleanup(feed.stop)
        return feed, store, now, readers

    def test_slow_metadata_does_not_stop_book_refresh(self):
        for venue in ('MEXC', 'GATE', 'EXPERIMENTAL'):
            with self.subTest(venue=venue):
                feed, store, now, readers = self.make_feed(venue=venue)
                feed.fetch_once()
                readers[1].block = True
                thread = threading.Thread(target=feed._refresh_ticker,
                                          args=(float('inf'),), daemon=True)
                feed._metadata_thread = thread
                thread.start()
                try:
                    self.assertTrue(readers[1].entered.wait(1))
                    now[0] += 9_000
                    # A queued ticker is still blocked; depth uses another host.
                    feed.fetch_once()
                    self.assertEqual(store.current().sequence, 2)
                    self.assertEqual(store.current().observed_at_ms, now[0])
                finally:
                    readers[1].release.set()
                    thread.join(2)

    def test_expired_metadata_cannot_freshen_book(self):
        feed, store, now, readers = self.make_feed()
        feed.fetch_once()
        readers[1].block = True
        thread = threading.Thread(target=feed._refresh_ticker,
                                  args=(float('inf'),), daemon=True)
        feed._metadata_thread = thread
        thread.start()
        try:
            self.assertTrue(readers[1].entered.wait(1))
            now[0] += 15_001
            with self.assertRaisesRegex(MexcPublicFeedError, 'expired'):
                feed.fetch_once()
            with self.assertRaises(StaleMarketData):
                store.current()
        finally:
            readers[1].release.set()
            thread.join(2)

    def test_each_market_and_metadata_lane_get_separate_clients(self):
        a, _, _, ar = self.make_feed()
        b, _, _, br = self.make_feed('DASHUSDT')
        self.assertEqual(len({id(r) for r in (*ar, *br)}), 4)
        a.stop()
        b.stop()
        self.assertTrue(all(r.closed for r in (*ar, *br)))

    def test_public_plugin_readers_bypass_pool_without_credentials(self):
        item = {'venue': 'MEXC'}
        with patch('kdf_mm.exchanges.installed_plugins', return_value={'MEXC': item}), \
             patch('kdf_mm.exchanges.load_config', return_value=SimpleNamespace(venue='MEXC')):
            first, second = create_public_reader('MEXC'), create_public_reader('MEXC')
        self.assertIsInstance(first, SpotPluginClient)
        self.assertIsNot(first, second)
        for reader in (first, second):
            self.assertIsNone(reader.options['api_key'])
            self.assertIsNone(reader.options['api_secret'])
            self.assertFalse(reader.trading_enabled)
            reader.close()

    def test_failed_metadata_refresh_does_not_extend_cache_ttl(self):
        feed, _, now, readers = self.make_feed()
        feed.fetch_once()
        before = feed._ticker_at_ms
        with patch.object(readers[1], 'ticker_24h', side_effect=TimeoutError):
            now[0] += 14_000
            with self.assertRaises(TimeoutError):
                feed._refresh_ticker(float('inf'))
        self.assertEqual(feed._ticker_at_ms, before)

    def test_actual_background_metadata_loop_leaves_depth_running(self):
        feed, store, _, readers = self.make_feed()
        feed.fetch_once()
        readers[1].block = True
        feed.start()
        try:
            self.assertTrue(readers[1].entered.wait(1))
            feed.fetch_once()
            self.assertGreaterEqual(store.current().sequence, 2)
            self.assertTrue(feed.status().running)
        finally:
            readers[1].release.set()
            feed.stop()
        self.assertFalse(feed.status().running)
        self.assertFalse(feed._metadata_thread.is_alive())
