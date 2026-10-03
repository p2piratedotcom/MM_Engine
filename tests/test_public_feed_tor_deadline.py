"""Public MEXC reads stay usable over Tor without relaxing book freshness."""

from unittest import TestCase
from unittest.mock import patch

from kdf_mm.http import TransportError
from kdf_mm.market_data import MarketDataStore, StaleMarketData
from kdf_mm.mexc import MexcClient, MexcError
from kdf_mm.public_feed import MexcPublicFeed
from kdf_mm.vps_agent import _safe_cex_error


class DelayedPublicTransport:
    def __init__(self, clock, *, depth_seconds=1.0):
        self.clock = clock
        self.depth_seconds = depth_seconds
        self.calls = []

    def request(self, **kwargs):
        url = kwargs["url"]
        seconds = (3.0 if "/exchangeInfo?" in url else
                   2.0 if "/ticker/24hr?" in url else self.depth_seconds)
        self.calls.append(kwargs)
        if seconds > kwargs["total_timeout"]:
            self.clock[0] += kwargs["total_timeout"]
            raise TransportError("remote API is unreachable (timeout)")
        self.clock[0] += seconds
        if "/exchangeInfo?" in url:
            return {"symbols": [{
                "symbol": "ARRRUSDT", "isSpotTradingAllowed": True,
                "orderTypes": ["LIMIT"], "tradeSideType": 1,
                "baseAsset": "ARRR", "quoteAsset": "USDT",
                "baseSizePrecision": "0.01", "quoteAmountPrecision": "1",
                "quoteAssetPrecision": 4,
            }]}
        if "/ticker/24hr?" in url:
            return {"volume": "1234"}
        return {"bids": [["0.24", "4"]], "asks": [["0.25", "3"]]}


class TorDeadlineTests(TestCase):
    def _feed(self, transport):
        store = MarketDataStore(
            symbol="ARRRUSDT", secret="test-only", max_age_ms=10_000,
            clock_ms=lambda: 10_100,
        )
        feed = MexcPublicFeed(
            client=MexcClient(transport=transport, clock_ms=lambda: 10_100),
            store=store, snapshot_secret="test-only",
            clock_ms=lambda: 10_100,
        )
        return feed, store

    def test_slow_tor_metadata_does_not_consume_book_budget(self):
        clock = [100.0]
        transport = DelayedPublicTransport(clock)
        feed, store = self._feed(transport)
        with patch("kdf_mm.public_feed.time.monotonic", side_effect=lambda: clock[0]):
            feed.fetch_once()
        self.assertEqual(store.current().sequence, 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertGreater(transport.calls[0]["total_timeout"], 3)
        self.assertGreater(transport.calls[1]["total_timeout"], 2)
        self.assertLessEqual(transport.calls[2]["total_timeout"], 4)

    def test_remote_mexc_payload_is_not_reflected_to_wallet(self):
        error = MexcError(
            "remote API rejected the request: private detail",
            status=403,
            payload={"msg": "private detail"},
        )
        self.assertEqual(
            _safe_cex_error(error),
            "MEXC API rejected the request (HTTP 403)",
        )
        self.assertEqual(
            _safe_cex_error(MexcError("remote API is unreachable (timeout)")),
            "MEXC remote API is unreachable (timeout)",
        )

    def test_slow_order_book_still_fails_closed(self):
        clock = [100.0]
        transport = DelayedPublicTransport(clock, depth_seconds=5.0)
        feed, store = self._feed(transport)
        with patch("kdf_mm.public_feed.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(MexcError):
                feed.fetch_once()
        with self.assertRaises(StaleMarketData):
            store.current()
        self.assertLessEqual(transport.calls[2]["total_timeout"], 4)
