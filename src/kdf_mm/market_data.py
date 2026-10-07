from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .models import OrderBook
from .network_diagnostics import emit as diagnostic


class MarketDataError(ValueError):
    pass


class InvalidSnapshotSignature(MarketDataError):
    pass


class StaleMarketData(MarketDataError):
    pass


class SnapshotReplay(MarketDataError):
    pass


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sign_snapshot(payload: Mapping[str, Any], secret: str) -> str:
    if not secret:
        raise ValueError("snapshot secret is required")
    return hmac.new(
        secret.encode("utf-8"),
        _canonical_payload(payload),
        hashlib.sha256,
    ).hexdigest()


def with_snapshot_signature(payload: Mapping[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(payload)
    signed["signature"] = sign_snapshot(signed, secret)
    return signed


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    sequence: int
    symbol: str
    observed_at_ms: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    base_volume_24h: Decimal
    buy_capacity_arrr: Decimal
    sell_capacity_arrr: Decimal
    quantity_step: Decimal
    price_step: Decimal
    min_quote_amount: Decimal
    signature: str
    volume_observed_at_ms: int | None = None
    volume_known: bool = True

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MarketSnapshot":
        try:
            snapshot = cls(
                sequence=int(payload["sequence"]),
                symbol=str(payload["symbol"]).upper(),
                observed_at_ms=int(payload["observed_at_ms"]),
                bids=_levels(payload["bids"]),
                asks=_levels(payload["asks"]),
                base_volume_24h=Decimal(str(payload["base_volume_24h"])),
                buy_capacity_arrr=Decimal(str(payload["buy_capacity_arrr"])),
                sell_capacity_arrr=Decimal(str(payload["sell_capacity_arrr"])),
                quantity_step=Decimal(str(payload["quantity_step"])),
                price_step=Decimal(str(payload["price_step"])),
                min_quote_amount=Decimal(str(payload["min_quote_amount"])),
                signature=str(payload["signature"]),
                volume_observed_at_ms=(int(payload['volume_observed_at_ms'])
                                       if payload.get('volume_observed_at_ms') is not None else None),
                volume_known=payload.get('volume_known',True),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise MarketDataError("invalid market snapshot") from exc
        snapshot._validate()
        return snapshot

    def _validate(self) -> None:
        if type(self.volume_known) is not bool:
            raise MarketDataError("volume_known must be boolean")
        if self.sequence < 0 or self.observed_at_ms <= 0:
            raise MarketDataError("snapshot sequence and time must be positive")
        if self.volume_observed_at_ms is not None and self.volume_observed_at_ms <= 0:
            raise MarketDataError('rolling volume observation time must be positive')
        if not self.symbol or not self.signature:
            raise MarketDataError("snapshot symbol and signature are required")
        numeric = (
            self.base_volume_24h,
            self.buy_capacity_arrr,
            self.sell_capacity_arrr,
            self.quantity_step,
            self.price_step,
            self.min_quote_amount,
        )
        if any(value < 0 for value in numeric):
            raise MarketDataError("snapshot numeric values cannot be negative")
        if self.quantity_step <= 0 or self.price_step <= 0:
            raise MarketDataError("market precision steps must be positive")
        # Parsing through OrderBook also validates positive prices and quantities.
        self.order_book()

    def order_book(self) -> OrderBook:
        return OrderBook.from_mexc(
            {"bids": self.bids, "asks": self.asks},
            observed_at_ms=self.observed_at_ms,
        )


def _levels(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("order-book levels must be a sequence")
    rows: list[tuple[str, str]] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
            raise TypeError("invalid order-book level")
        rows.append((str(row[0]), str(row[1])))
    if not rows:
        raise ValueError("order-book side cannot be empty")
    return tuple(rows)


class MarketDataStore:
    def __init__(
        self,
        *,
        symbol: str,
        secret: str,
        max_age_ms: int = 10_000,
        max_future_skew_ms: int = 2_000,
        clock_ms: Callable[[], int],
    ) -> None:
        if not secret:
            raise ValueError("snapshot secret is required")
        if max_age_ms <= 0 or max_future_skew_ms < 0:
            raise ValueError("invalid market-data timing limits")
        self.symbol = symbol.upper()
        self.secret = secret
        self.max_age_ms = max_age_ms
        self.max_future_skew_ms = max_future_skew_ms
        self.clock_ms = clock_ms
        self._current: MarketSnapshot | None = None
        self._lock = threading.RLock()

    def ingest(self, payload: Mapping[str, Any]) -> MarketSnapshot:
        provided = str(payload.get("signature", ""))
        expected = sign_snapshot(payload, self.secret)
        if not hmac.compare_digest(provided, expected):
            raise InvalidSnapshotSignature("market snapshot signature is invalid")
        snapshot = MarketSnapshot.from_payload(payload)
        if snapshot.symbol != self.symbol:
            raise MarketDataError(
                f"snapshot symbol {snapshot.symbol} does not match {self.symbol}"
            )
        now = self.clock_ms()
        if snapshot.observed_at_ms > now + self.max_future_skew_ms:
            self._diagnostic_rejection(snapshot, now, 'book_future', 'ingest')
            raise MarketDataError("market snapshot is too far in the future")
        if not getattr(self,"price_reference_only",False):
            self._check_volume_age(snapshot, now, source="ingest")
        if now - snapshot.observed_at_ms > self.max_age_ms:
            self._diagnostic_rejection(snapshot, now, "book_expired", "ingest")
            raise StaleMarketData("market snapshot arrived already stale")
        with self._lock:
            if self._current is not None and snapshot.sequence <= self._current.sequence:
                raise SnapshotReplay("market snapshot sequence is not increasing")
            self._current = snapshot
        return snapshot

    def current(self, *, price_only=False) -> MarketSnapshot:
        with self._lock:
            current = self._current
        if current is None:
            raise StaleMarketData("no market snapshot is available")
        now = self.clock_ms()
        if current.observed_at_ms > now+self.max_future_skew_ms:
            self._diagnostic_rejection(current,now,"book_future","current")
            raise StaleMarketData("Market book is future-dated")
        if not price_only:
            self._check_volume_age(current, now)
        if now - current.observed_at_ms > self.max_age_ms:
            self._diagnostic_rejection(current, now, "book_expired", "current")
            raise StaleMarketData("market snapshot has expired")
        return current

    def _diagnostic_rejection(self, snapshot, now, reason, source):
        volume = snapshot.volume_observed_at_ms
        diagnostic('market_rejected', throttle_key=(id(self), reason, source),
                   symbol=self.symbol, sequence=snapshot.sequence, reason=reason, source=source,
                   now_ms=now, book_observed_ms=snapshot.observed_at_ms,
                   volume_observed_ms=volume, book_age_ms=now-snapshot.observed_at_ms,
                   volume_age_ms=None if volume is None else now-volume,
                   book_max_age_ms=self.max_age_ms, volume_max_age_ms=15000)

    def _check_volume_age(self, snapshot, now, source='current'):
        if not snapshot.volume_known:
            self._diagnostic_rejection(snapshot,now,"volume_missing",source)
            raise StaleMarketData("Rolling volume is unavailable; only price reference is usable")
        observed = snapshot.volume_observed_at_ms
        if observed is not None and (observed > now + self.max_future_skew_ms
                                      or now - observed >= 15000):
            self._diagnostic_rejection(snapshot, now,
                'volume_future' if observed > now+self.max_future_skew_ms else 'volume_expired', source)
            raise StaleMarketData('rolling-volume snapshot is expired or future-dated')

    def age_ms(self) -> int | None:
        with self._lock:
            current = self._current
        if current is None:
            return None
        return max(0, self.clock_ms() - current.observed_at_ms)
