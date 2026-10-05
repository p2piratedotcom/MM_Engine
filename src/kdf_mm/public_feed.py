from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .market_data import MarketDataStore, with_snapshot_signature
from .venues import market_data_key
from .network_diagnostics import emit as diagnostic
from uuid import uuid4


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
        client_factory: Any | None = None,
    ) -> None:
        if not snapshot_secret:
            raise ValueError("snapshot_secret is required")
        if interval_seconds < 1.0:
            raise ValueError("MEXC feed interval must be at least one second")
        if depth_limit <= 0 or depth_limit > 5000:
            raise ValueError("MEXC depth limit must be in [1, 5000]")
        # Each symbol has an independent book reader. Plugin hosts serialize
        # requests, so shared readers can starve one market behind another.
        # Metadata uses a second public-only reader and never queues ahead of
        # depth. The factory must not contain credentials or trading permission.
        self.client = client_factory() if client_factory else client
        self._metadata_client = client_factory() if client_factory else client
        self._owns_clients = client_factory is not None
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
        self._metadata_thread: threading.Thread | None = None
        self._fetch_lock = threading.RLock()
        self._metadata_lock = threading.RLock()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._timings_ms: dict[str, float] = {}
        self._book_observed_ms: int | None = None

    def _diagnostic(self, event, **fields):
        # Diagnostic clock failures must not change refresh outcomes.
        try:
            now = self.clock_ms()
        except Exception:
            return
        with self._lock:
            book, volume = self._book_observed_ms, self._ticker_at_ms
        diagnostic(event, venue=self.venue, symbol=self.symbol, now_ms=now,
                   book_observed_ms=book, volume_observed_ms=volume,
                   book_age_ms=None if book is None else now-book,
                   volume_age_ms=None if volume is None else now-volume,
                   book_max_age_ms=self.store.max_age_ms, volume_max_age_ms=15000,
                   **fields)

    def _stage(self, name, callback):
        started = time.monotonic()
        outcome, kind, status = 'received', None, None
        self._diagnostic('feed_stage_start', phase=name)
        try:
            return callback()
        except Exception as exc:
            outcome, kind, status = 'error', type(exc).__name__, getattr(exc, 'status', None)
            raise
        finally:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            with self._lock:
                self._timings_ms[name] = elapsed
            self._diagnostic('feed_stage_end', phase=name, elapsed_ms=elapsed,
                             outcome=outcome, failure_kind=kind, http_status=status)

    def fetch_once(self) -> Mapping[str, Any]:
        # Manual previews may refresh while the polling thread is active.
        started, identity = time.monotonic(), uuid4().hex
        started_at_ms = time.time_ns() // 1_000_000
        with self._fetch_lock:
            queue = round((time.monotonic()-started)*1000, 2)
            self._diagnostic('feed_cycle_start', request_id=identity, queue_ms=queue, started_at_ms=started_at_ms)
            outcome = 'received'
            try:
                return self._fetch_once()
            except Exception:
                outcome = 'error'
                raise
            finally:
                self._diagnostic('feed_cycle_end', request_id=identity, outcome=outcome,
                                 queue_ms=queue, elapsed_ms=round((time.monotonic()-started)*1000, 2),
                                 wall_elapsed_ms=time.time_ns()//1_000_000-started_at_ms)

    def _fetch_once(self) -> Mapping[str, Any]:
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
        with self._lock:
            self._book_observed_ms = book.observed_at_ms
        # Metadata can expire while a depth request is in flight. Recheck it
        # without extending its timestamp; a fresh book alone is insufficient.
        ticker = self._stage('ticker_24h_final', lambda: self._ticker_24h(metadata_deadline))
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
            "volume_observed_at_ms": ticker['_observed_at_ms'],
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
            if self._owns_clients:
                self._metadata_thread = threading.Thread(
                    target=self._run_metadata,
                    name=f"{self.venue.lower()}-public-metadata",
                    daemon=True,
                )
                self._metadata_thread.start()
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
            metadata_thread = self._metadata_thread
        for worker in (thread, metadata_thread):
            if worker is not None:
                worker.join(timeout=17.0)
        if self._owns_clients:
            for client in (self.client, self._metadata_client):
                close = getattr(client, "close", None)
                if callable(close):
                    close()

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
        with self._metadata_lock:
            with self._lock:
                if self._symbol is not None:
                    return self._symbol
            rules = self._read(self._metadata_client.symbol_rules, self.symbol, deadline=deadline)
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
        with self._lock:
            running = self._metadata_thread is not None and self._metadata_thread.is_alive()
        if running:
            # Do not block depth behind a slow metadata refresh or renew the
            # book timestamp using volume that has exceeded its original TTL.
            raise MexcPublicFeedError(f"{self.venue} rolling-volume data is unavailable or expired")
        return self._refresh_ticker(deadline)

    def _refresh_ticker(self, deadline):
        queued = time.monotonic()
        with self._metadata_lock:
            self._diagnostic('metadata_request_start', queue_ms=round((time.monotonic()-queued)*1000, 2))
            requested_at_ms = self.clock_ms()
            ticker = self._read(self._metadata_client.ticker_24h, self.symbol, deadline=deadline)
            try:
                volume = Decimal(str(ticker["volume"]))
                if not volume.is_finite() or volume < 0:
                    raise ValueError("invalid volume")
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise MexcPublicFeedError(f"{self.venue} reported invalid 24h volume") from exc
            with self._lock:
                self._ticker = {**ticker, '_observed_at_ms': requested_at_ms}
                self._ticker_at_ms = requested_at_ms
                return self._ticker

    def _run_metadata(self) -> None:
        failures = 0
        expected = time.monotonic()
        while not self._stop.is_set():
            self._diagnostic('metadata_wakeup', wakeup_lag_ms=max(0, round((time.monotonic()-expected)*1000)))
            try:
                self._stage('metadata_rules', lambda: self._symbol_rules(time.monotonic() + 12.0))
                # Refresh ahead of the unchanged 15-second volume TTL, with
                # a bounded request on a reader independent from order books.
                self._stage('metadata_refresh', lambda: self._refresh_ticker(time.monotonic() + 4.0))
                failures = 0
            except Exception:
                failures += 1
                # A failed refresh does not extend the cached data's lifetime.
                # fetch_once/status and the existing circuit breaker expose
                # expired data and withdraw unprotected orders normally.
                pass
            # A fixed five-second sleep after a four-second timeout allowed
            # two transient failures to exhaust the unchanged 15-second TTL.
            # Retry promptly, then back off during a sustained outage.
            delay = 5.0 if not failures else min(5.0, 0.5 * 2 ** min(failures - 1, 4))
            self._diagnostic('metadata_retry', failures=failures, retry_delay_ms=round(delay*1000))
            expected = time.monotonic() + delay
            self._stop.wait(delay)

    def _run(self) -> None:
        expected = time.monotonic()
        while not self._stop.is_set():
            started = time.monotonic()
            self._diagnostic('feed_wakeup', wakeup_lag_ms=max(0, round((started-expected)*1000)))
            try:
                self.fetch_once()
            except Exception as exc:
                with self._lock:
                    self._consecutive_failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
            elapsed = time.monotonic() - started
            with self._lock:
                failures = self._consecutive_failures
            delay = self.interval_seconds if not failures else min(
                self.interval_seconds, 0.5 * 2 ** min(failures - 1, 4))
            wait = delay if failures else max(0.1, delay - elapsed)
            self._diagnostic('feed_retry', failures=failures, retry_delay_ms=round(wait*1000))
            expected = time.monotonic() + wait
            self._stop.wait(wait)


class MexcPublicFeedGroup:
    """Lifecycle and status adapter for all symbols required by configured markets."""

    def __init__(
        self, feeds: Mapping[str, MexcPublicFeed], *, allow_empty: bool = False
    ) -> None:
        if not feeds and not allow_empty:
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
