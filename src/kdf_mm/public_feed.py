from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .market_data import MarketDataStore, with_snapshot_signature
from .venues import market_data_key


class MexcPublicFeedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MexcPublicFeedStatus:
    running: bool
    symbol: str
    sequence: int
    last_success_ms: int | None
    consecutive_failures: int
    last_error: str | None
    timings_ms: dict[str, float] = field(default_factory=dict)
    venue: str = "MEXC"


class MexcPublicFeed:
    """Polls only public MEXC endpoints and feeds signed local snapshots."""

    def __init__(
        self,
        *,
        client: Any,
        store: MarketDataStore,
        snapshot_secret: str,
        symbol: str = "ARRRUSDT",
        interval_seconds: float = 3.0,
        depth_limit: int = 100,
        clock_ms: Any | None = None,
        sample_sink: Any | None = None,
        venue: str = "MEXC",
    ) -> None:
        if not snapshot_secret:
            raise ValueError("snapshot_secret is required")
        if interval_seconds < 1.0:
            raise ValueError("MEXC feed interval must be at least one second")
        if depth_limit <= 0 or depth_limit > 5000:
            raise ValueError("MEXC depth limit must be in [1, 5000]")
        self.client = client
        self.store = store
        self.snapshot_secret = snapshot_secret
        self.symbol = symbol.upper()
        self.interval_seconds = interval_seconds
        self.depth_limit = depth_limit
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.sample_sink = sample_sink
        self.venue = venue.strip().upper()
        self._last_sample_ms: int | None = None
        self._sequence = 0
        self._last_success_ms: int | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._symbol: Any | None = None
        self._ticker: Mapping[str, Any] | None = None
        self._ticker_at_ms: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._timings_ms: dict[str, float] = {}

    def _stage(self, name, callback):
        started = time.monotonic()
        try:
            return callback()
        finally:
            with self._lock:
                self._timings_ms[name] = round((time.monotonic() - started) * 1000, 2)

    def fetch_once(self) -> Mapping[str, Any]:
        with self._lock:
            self._timings_ms = {}
        # Static symbol rules and rolling volume can arrive slowly over Tor.
        # They do not determine the book's age, so do not spend its freshness
        # budget on these requests. Both metadata reads share a hard deadline.
        metadata_deadline = time.monotonic() + 12.0
        rules = self._stage('symbol_rules', lambda: self._symbol_rules(metadata_deadline))
        ticker = self._stage('ticker_24h', lambda: self._ticker_24h(metadata_deadline))
        # Start a separate, short deadline immediately before requesting the
        # time-sensitive book. MexcClient timestamps it before the network call;
        # MarketDataStore rejects it if it is stale when received.
        book_deadline = time.monotonic() + min(4.0, self.store.max_age_ms / 2500)
        book = self._stage('depth', lambda: self._read(
            self.client.order_book, self.symbol, deadline=book_deadline,
            limit=self.depth_limit,
        ))
        if not book.bids or not book.asks:
            raise MexcPublicFeedError(f"{self.venue} returned an incomplete order book")
        try:
            volume = Decimal(str(ticker["volume"]))
            quantity_step = rules.quantity_step
            min_quote = rules.min_quote_amount
            price_step = rules.price_step
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise MexcPublicFeedError(f"{self.venue} symbol precision data is invalid") from exc
        if volume < 0:
            raise MexcPublicFeedError(f"{self.venue} reported a negative 24h volume")

        observed_at = book.observed_at_ms or self.clock_ms()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        payload: dict[str, Any] = {
            "sequence": sequence,
            "symbol": self.symbol,
            "observed_at_ms": observed_at,
            "bids": [[str(level.price), str(level.quantity)] for level in book.bids],
            "asks": [[str(level.price), str(level.quantity)] for level in book.asks],
            "base_volume_24h": str(volume),
            "buy_capacity_arrr": str(
                sum((level.quantity for level in book.asks), start=Decimal("0"))
            ),
            "sell_capacity_arrr": str(
                sum((level.quantity for level in book.bids), start=Decimal("0"))
            ),
            "quantity_step": str(quantity_step),
            "price_step": str(price_step),
            "min_quote_amount": str(min_quote),
        }
        signed = with_snapshot_signature(payload, self.snapshot_secret)
        snapshot = self.store.ingest(signed)
        if self.sample_sink is not None and (self._last_sample_ms is None
                                             or observed_at - self._last_sample_ms >= 15_000):
            try:
                best_bid, best_ask = book.bids[0].price, book.asks[0].price
                self.sample_sink({
                    "symbol": market_data_key(self.venue, self.symbol), "sequence": sequence,
                    "observed_at_ms": observed_at,
                    "best_bid": str(best_bid), "best_ask": str(best_ask),
                    "bid_depth_1pct": str(sum((level.quantity for level in book.bids
                        if level.price >= best_bid * Decimal("0.99")), Decimal("0"))),
                    "ask_depth_1pct": str(sum((level.quantity for level in book.asks
                        if level.price <= best_ask * Decimal("1.01")), Decimal("0"))),
                    "bid_depth_total": str(sum((level.quantity for level in book.bids), Decimal("0"))),
                    "ask_depth_total": str(sum((level.quantity for level in book.asks), Decimal("0"))),
                    "quantity_step": str(quantity_step),
                    "min_quote_amount": str(min_quote),
                    "bid_levels": len(book.bids), "ask_levels": len(book.asks),
                    "timings_ms": dict(self._timings_ms),
                })
                self._last_sample_ms = observed_at
            except Exception:
                # Optional observability must never invalidate an otherwise
                # fresh, signed safety snapshot.
                pass
        with self._lock:
            self._last_success_ms = self.clock_ms()
            self._consecutive_failures = 0
            self._last_error = None
        return asdict(snapshot)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"{self.venue.lower()}-public-feed",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_seconds + 1.0))

    def status(self) -> MexcPublicFeedStatus:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return MexcPublicFeedStatus(
                running=running,
                symbol=self.symbol,
                sequence=self._sequence,
                last_success_ms=self._last_success_ms,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                timings_ms=dict(self._timings_ms),
                venue=self.venue,
            )

    def _read(self, callback, *args, deadline: float, **kwargs):
        if getattr(self.client, "supports_read_deadlines", False) is True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MexcPublicFeedError(f"{self.venue} public-feed read deadline expired")
            kwargs.update(timeout=remaining, total_timeout=remaining)
        return callback(*args, **kwargs)

    def _symbol_rules(self, deadline: float):
        with self._lock:
            cached = self._symbol
        if cached is not None:
            return cached
        rules = self._read(self.client.symbol_rules, self.symbol, deadline=deadline)
        with self._lock:
            self._symbol = rules
        return rules

    def _ticker_24h(self, deadline: float) -> Mapping[str, Any]:
        # The rolling 24h volume changes much more slowly than the order book.
        # Avoid an extra remote request on every 3-second depth refresh while
        # never using a cached volume older than 15 seconds for a new quote.
        now = self.clock_ms()
        with self._lock:
            if (self._ticker is not None and self._ticker_at_ms is not None
                    and 0 <= now - self._ticker_at_ms < 15_000):
                return self._ticker
        ticker = self._read(self.client.ticker_24h, self.symbol, deadline=deadline)
        with self._lock:
            self._ticker = ticker
            self._ticker_at_ms = self.clock_ms()
        return ticker

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.fetch_once()
            except Exception as exc:
                with self._lock:
                    self._consecutive_failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.1, self.interval_seconds - elapsed))


class MexcPublicFeedGroup:
    """Lifecycle and status adapter for all symbols required by configured markets."""

    def __init__(self, feeds: Mapping[str, MexcPublicFeed]) -> None:
        if not feeds:
            raise ValueError("at least one public feed is required")
        self.feeds = {symbol.upper(): feed for symbol, feed in feeds.items()}
        self._active_symbols = set(self.feeds)
        self._started = False
        self._lock = threading.RLock()
        self._sample_sink: Any | None = None

    def set_sample_sink(self, sample_sink: Any) -> None:
        with self._lock:
            self._sample_sink = sample_sink
            for feed in self.feeds.values():
                feed.sample_sink = sample_sink

    def start(self) -> None:
        with self._lock:
            self._started = True
            feeds = [self.feeds[symbol] for symbol in self._active_symbols]
        for feed in feeds:
            feed.start()

    def add(self, symbol: str, feed: MexcPublicFeed) -> None:
        with self._lock:
            if symbol in self.feeds:
                raise ValueError("feed già configurato")
            feed.sample_sink = self._sample_sink
            self.feeds = {**self.feeds, symbol: feed}

    def stop(self) -> None:
        with self._lock:
            self._started = False
            feeds = list(self.feeds.values())
        for feed in feeds:
            feed.stop()

    def set_active_symbols(self, symbols: tuple[str, ...]) -> None:
        requested = {symbol.upper() for symbol in symbols}
        unknown = requested - set(self.feeds)
        if unknown:
            raise ValueError(
                f"unconfigured public-feed symbols: {', '.join(sorted(unknown))}"
            )
        with self._lock:
            previous = set(self._active_symbols)
            self._active_symbols = requested
            started = self._started
        if not started:
            return
        for symbol in previous - requested:
            self.feeds[symbol].stop()
        for symbol in requested - previous:
            self.feeds[symbol].start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = set(self._active_symbols)
        statuses = {
            symbol: {
                **asdict(feed.status()),
                "subscribed": symbol in active,
            }
            for symbol, feed in self.feeds.items()
        }
        return {
            "running": bool(active)
            and all(statuses[symbol]["running"] for symbol in active),
            "active_symbols": sorted(active),
            "symbols": statuses,
        }

    def status_for(self, symbols: tuple[str, ...]) -> dict[str, Any]:
        with self._lock:
            active = set(self._active_symbols)
        selected = {
            symbol: {
                **asdict(self.feeds[symbol].status()),
                "subscribed": symbol in active,
            }
            for symbol in symbols
            if symbol in self.feeds
        }
        return {
            "running": bool(selected)
            and all(
                item["running"] and item["subscribed"]
                for item in selected.values()
            ),
            "symbols": selected,
        }


def _positive_decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("value must be a positive finite decimal")
    return parsed


def _price_step(symbol: Mapping[str, Any]) -> Decimal:
    precision = symbol.get("quoteAssetPrecision", symbol.get("quotePrecision"))
    digits = int(precision)
    if digits < 0 or digits > 18:
        raise ValueError("quote precision is out of range")
    return Decimal(1).scaleb(-digits)


def _quantity_step(symbol: Mapping[str, Any]) -> Decimal:
    reported = Decimal(str(symbol.get("baseSizePrecision", "0")))
    if reported.is_finite() and reported > 0:
        return reported
    precision = int(symbol["baseAssetPrecision"])
    if precision < 0 or precision > 18:
        raise ValueError("base precision is out of range")
    return Decimal(1).scaleb(-precision)
