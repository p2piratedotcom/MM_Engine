from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import DexSide, HedgeSide
from .ownership import OwnedOrder, OwnedSwap, OwnedSwapState


EVENT_SCHEMA_VERSION = 1
HEDGE_EVENT_TYPE = "HEDGE_REQUIRED"
HEDGE_TRIGGER_EVENT = "MakerPaymentSent"
SWAP_OUTCOME_EVENT_TYPE = "KDF_SWAP_OUTCOME"
SWAP_OUTCOME_TRIGGER_EVENT = "KdfSwapFinished"
_CONSUMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class OutboxConflict(RuntimeError):
    pass


class OutboxIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    event: Mapping[str, Any]
    signature: str
    acknowledged: bool
    acknowledged_by: str | None
    acknowledged_at_ms: int | None

    @property
    def event_id(self) -> int:
        return int(self.event["event_id"])

    @property
    def swap_uuid(self) -> str:
        return str(self.event["swap_uuid"])

    def envelope(self) -> dict[str, Any]:
        return {
            "event": dict(self.event),
            "signature": self.signature,
            "delivery": {
                "acknowledged": self.acknowledged,
                "acknowledged_by": self.acknowledged_by,
                "acknowledged_at_ms": self.acknowledged_at_ms,
            },
        }


class HedgeEventOutbox:
    """Durable, signed, single-consumer outbox for KDF hedge milestones."""

    def __init__(
        self,
        path: str | Path,
        *,
        secret: str,
        hedge_symbol: str = "ARRRUSDT",
        hedge_base_asset: str = "ARRR",
        hedge_quote_asset: str = "USDT",
        quote_valuation_resolver: Callable[[OwnedOrder], Mapping[str, Any]]
        | None = None,
        clock_ms: Callable[[], int] | None = None,
        hedge_route_resolver: Callable[[OwnedOrder, Mapping[str, str]], Mapping[str, Any]] | None = None,
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("KDF_MM_EVENT_SECRET must contain at least 32 bytes")
        self._secret = secret.encode("utf-8")
        self._hedge_symbol = hedge_symbol.strip().upper()
        if not self._hedge_symbol:
            raise ValueError("hedge symbol is required")
        self._hedge_base_asset = hedge_base_asset.strip().upper()
        self._hedge_quote_asset = hedge_quote_asset.strip().upper()
        if (
            not self._hedge_base_asset
            or not self._hedge_quote_asset
            or self._hedge_base_asset == self._hedge_quote_asset
        ):
            raise ValueError("hedge base and quote assets are invalid")
        self._quote_valuation_resolver = quote_valuation_resolver
        self.hedge_route_resolver = hedge_route_resolver
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.RLock()
        database_path = None if str(path) == ":memory:" else Path(path)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(path), check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        if database_path is not None:
            os.chmod(database_path, 0o600)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hedge_event_outbox (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                swap_uuid TEXT NOT NULL,
                trigger_event TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                signature TEXT NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_by TEXT,
                acknowledged_at_ms INTEGER,
                UNIQUE(schema_version, event_type, swap_uuid, trigger_event)
            );
            CREATE INDEX IF NOT EXISTS hedge_event_outbox_delivery
                ON hedge_event_outbox(acknowledged, event_id);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def observe_swap(
        self,
        order: OwnedOrder,
        swap: OwnedSwap,
        status: Mapping[str, Any],
    ) -> OutboxEvent | None:
        trigger_timestamp_ms = _event_timestamp_ms(status, HEDGE_TRIGGER_EVENT)
        if trigger_timestamp_ms is None and not _has_event(
            status, HEDGE_TRIGGER_EVENT
        ):
            return None
        observed_at_ms = int(self._clock_ms())
        if observed_at_ms <= 0:
            raise ValueError("outbox clock must return a positive timestamp")
        trigger_timestamp_ms = trigger_timestamp_ms or observed_at_ms
        terms = _swap_terms(status)
        if terms["maker_coin"] != order.kdf_base or terms["taker_coin"] != order.kdf_rel:
            raise OutboxConflict("KDF swap coins do not match the owned order")
        if swap.order_uuid != order.order_uuid or swap.swap_uuid != str(status.get("uuid")):
            raise OutboxConflict("KDF swap identity does not match the owned record")

        if not getattr(order,"hedging_enabled",True):
            return None  # Validated UUID policy authorizes no CEX event/trade.

        quote_ticker = (
            order.kdf_rel
            if order.dex_side is DexSide.SELL_ARRR
            else order.kdf_base
        )
        base_ticker = (
            order.kdf_base
            if order.dex_side is DexSide.SELL_ARRR
            else order.kdf_rel
        )
        hedge_side = (
            HedgeSide.BUY
            if order.dex_side is DexSide.SELL_ARRR
            else HedgeSide.SELL
        )
        valuation = self._quote_valuation(order, swap.swap_uuid)
        immutable = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_type": HEDGE_EVENT_TYPE,
            "swap_uuid": swap.swap_uuid,
            "order_uuid": order.order_uuid,
            "market_id": order.market_id,
            "dex_side": order.dex_side.value,
            "hedge_side": hedge_side.value,
            "hedge_symbol": self._hedge_symbol,
            "hedge_base_asset": self._hedge_base_asset,
            "hedge_quote_asset": self._hedge_quote_asset,
            "arrr_quantity": str(swap.arrr_quantity),
            "base_ticker": base_ticker,
            "base_quantity": str(swap.base_quantity),
            "quote_ticker": quote_ticker,
            "kdf_maker_coin": terms["maker_coin"],
            "kdf_maker_amount": terms["maker_amount"],
            "kdf_taker_coin": terms["taker_coin"],
            "kdf_taker_amount": terms["taker_amount"],
            "trigger_event": HEDGE_TRIGGER_EVENT,
            "trigger_timestamp_ms": trigger_timestamp_ms,
            **valuation,
        }
        # Persist the route once, independently of subsequent book changes or
        # configuration changes. Delivery/reconciliation must be idempotent.
        existing = self._find_event(EVENT_SCHEMA_VERSION, HEDGE_EVENT_TYPE, swap.swap_uuid, HEDGE_TRIGGER_EVENT)
        if existing is not None:
            for field in ("hedge_legs", "strategy_id", "cex", "hedge_symbol",
                          "hedge_base_asset", "hedge_quote_asset"):
                if field in existing.event:
                    immutable[field] = existing.event[field]
        elif self.hedge_route_resolver is not None:
            immutable.update(self.hedge_route_resolver(order, terms))
        hedge_event = self._publish(immutable, observed_at_ms=observed_at_ms)
        if swap.state in {OwnedSwapState.SUCCEEDED, OwnedSwapState.FAILED}:
            completed_at_ms = (
                _last_event_timestamp_ms(status) or trigger_timestamp_ms
            )
            existing_outcome = self._find_event(
                EVENT_SCHEMA_VERSION,
                SWAP_OUTCOME_EVENT_TYPE,
                swap.swap_uuid,
                SWAP_OUTCOME_TRIGGER_EVENT,
            )
            # Outbox events are immutable once published.  Preserve an older
            # generic value (usually ``Finished``) on replay, while new failed
            # swaps retain the decisive refund result needed by the desktop
            # inventory guard.
            terminal_event = (
                str(existing_outcome.event["terminal_event"])
                if existing_outcome is not None
                else _outcome_terminal_event(status, success=swap.state is OwnedSwapState.SUCCEEDED)
            )
            outcome = {
                **immutable,
                "event_type": SWAP_OUTCOME_EVENT_TYPE,
                "trigger_event": SWAP_OUTCOME_TRIGGER_EVENT,
                "trigger_timestamp_ms": completed_at_ms,
                "completed_at_ms": completed_at_ms,
                "kdf_success": swap.state is OwnedSwapState.SUCCEEDED,
                "terminal_event": terminal_event,
            }
            self._publish(outcome, observed_at_ms=observed_at_ms)
        return hedge_event

    def _quote_valuation(
        self, order: OwnedOrder, swap_uuid: str
    ) -> dict[str, Any]:
        existing = self._find_event(
            EVENT_SCHEMA_VERSION,
            HEDGE_EVENT_TYPE,
            swap_uuid,
            HEDGE_TRIGGER_EVENT,
        )
        fields = (
            "quote_usdt_rate",
            "quote_usdt_symbol",
            "quote_usdt_side",
            "quote_usdt_observed_at_ms",
        )
        if existing is not None:
            return {
                field: existing.event[field]
                for field in fields
                if field in existing.event
            }
        if self._quote_valuation_resolver is None:
            return {}
        try:
            raw = self._quote_valuation_resolver(order)
            result = {field: raw[field] for field in fields}
            rate = Decimal(str(result["quote_usdt_rate"]))
            if not rate.is_finite() or rate <= 0:
                raise ValueError("invalid quote/USDT rate")
            if str(result["quote_usdt_side"]) not in {"DIRECT", "BID", "ASK"}:
                raise ValueError("invalid quote/USDT side")
            if int(result["quote_usdt_observed_at_ms"]) <= 0:
                raise ValueError("invalid quote/USDT timestamp")
            return result
        except Exception:
            # Hedge delivery must not be lost merely because an accounting
            # reference is stale. The desktop ledger will mark it incomplete.
            return {}

    def _find_event(
        self,
        schema_version: int,
        event_type: str,
        swap_uuid: str,
        trigger_event: str,
    ) -> OutboxEvent | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM hedge_event_outbox
                WHERE schema_version = ? AND event_type = ?
                  AND swap_uuid = ? AND trigger_event = ?
                """,
                (schema_version, event_type, swap_uuid, trigger_event),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get(self, event_id: int) -> OutboxEvent | None:
        if event_id <= 0:
            raise ValueError("event_id must be positive")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM hedge_event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> tuple[OutboxEvent, ...]:
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if limit <= 0 or limit > 1000:
            raise ValueError("event limit must be in [1, 1000]")
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM hedge_event_outbox
                WHERE event_id > ? ORDER BY event_id LIMIT ?
                """,
                (after_event_id, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def acknowledge(self, event_id: int, *, consumer_id: str) -> OutboxEvent:
        if event_id <= 0:
            raise ValueError("event_id must be positive")
        if not _CONSUMER_ID.fullmatch(consumer_id):
            raise ValueError("invalid outbox consumer_id")
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM hedge_event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            existing_consumer = row["acknowledged_by"]
            if bool(row["acknowledged"]):
                if existing_consumer != consumer_id:
                    raise OutboxConflict(
                        f"event {event_id} was acknowledged by another consumer"
                    )
                return self._from_row(row)
            self.connection.execute(
                """
                UPDATE hedge_event_outbox
                SET acknowledged = 1, acknowledged_by = ?, acknowledged_at_ms = ?
                WHERE event_id = ?
                """,
                (consumer_id, int(self._clock_ms()), event_id),
            )
        acknowledged = self.get(event_id)
        assert acknowledged is not None
        return acknowledged

    def status(self) -> dict[str, int]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END), 0)
                           AS unacknowledged,
                       COALESCE(MAX(event_id), 0) AS latest_event_id
                FROM hedge_event_outbox
                """
            ).fetchone()
        return {
            "total": int(row["total"]),
            "unacknowledged": int(row["unacknowledged"]),
            "acknowledged": int(row["total"] - row["unacknowledged"]),
            "latest_event_id": int(row["latest_event_id"]),
        }

    def _publish(
        self,
        immutable: Mapping[str, Any],
        *,
        observed_at_ms: int,
    ) -> OutboxEvent:
        identity = (
            int(immutable["schema_version"]),
            str(immutable["event_type"]),
            str(immutable["swap_uuid"]),
            str(immutable["trigger_event"]),
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                """
                SELECT * FROM hedge_event_outbox
                WHERE schema_version = ? AND event_type = ?
                  AND swap_uuid = ? AND trigger_event = ?
                """,
                identity,
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                expected = {
                    "event_id": existing.event_id,
                    **dict(immutable),
                    "observed_at_ms": int(existing.event["observed_at_ms"]),
                }
                if expected != dict(existing.event):
                    raise OutboxConflict(
                        "swap UUID already emitted with different hedge details"
                    )
                return existing

            cursor = self.connection.execute(
                """
                INSERT INTO hedge_event_outbox
                    (schema_version, event_type, swap_uuid, trigger_event,
                     payload_json, signature, observed_at_ms)
                VALUES (?, ?, ?, ?, '{}', '', ?)
                """,
                (*identity, observed_at_ms),
            )
            event_id = int(cursor.lastrowid)
            event = {
                "event_id": event_id,
                **dict(immutable),
                "observed_at_ms": observed_at_ms,
            }
            payload_json = _canonical_json(event)
            signature = _sign(event, self._secret)
            self.connection.execute(
                """
                UPDATE hedge_event_outbox
                SET payload_json = ?, signature = ? WHERE event_id = ?
                """,
                (payload_json, signature, event_id),
            )
        published = self.get(event_id)
        assert published is not None
        return published

    def _from_row(self, row: sqlite3.Row) -> OutboxEvent:
        try:
            event = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise OutboxIntegrityError("outbox payload is not valid JSON") from exc
        if not isinstance(event, Mapping):
            raise OutboxIntegrityError("outbox payload is not a JSON object")
        signature = str(row["signature"])
        if int(event.get("event_id", 0)) != int(row["event_id"]):
            raise OutboxIntegrityError("outbox event id does not match its row")
        if not hmac.compare_digest(signature, _sign(event, self._secret)):
            raise OutboxIntegrityError("outbox event signature is invalid")
        return OutboxEvent(
            event=dict(event),
            signature=signature,
            acknowledged=bool(row["acknowledged"]),
            acknowledged_by=row["acknowledged_by"],
            acknowledged_at_ms=(
                int(row["acknowledged_at_ms"])
                if row["acknowledged_at_ms"] is not None
                else None
            ),
        )


def verify_event_envelope(envelope: Mapping[str, Any], *, secret: str) -> bool:
    event = envelope.get("event")
    signature = envelope.get("signature")
    if not isinstance(event, Mapping) or not isinstance(signature, str):
        return False
    expected = _sign(event, secret.encode("utf-8"))
    return hmac.compare_digest(signature, expected)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sign(payload: Mapping[str, Any], secret: bytes) -> str:
    return hmac.new(
        secret,
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _has_event(status: Mapping[str, Any], wanted: str) -> bool:
    return _event_timestamp_ms(status, wanted, require_timestamp=False) is not None


def _event_timestamp_ms(
    status: Mapping[str, Any],
    wanted: str,
    *,
    require_timestamp: bool = True,
) -> int | None:
    events = status.get("events")
    if not isinstance(events, list):
        return None
    for item in events:
        if not isinstance(item, Mapping):
            continue
        event = item.get("event")
        if not isinstance(event, Mapping) or event.get("type") != wanted:
            continue
        timestamp = item.get("timestamp")
        if timestamp is None:
            return -1 if not require_timestamp else None
        try:
            parsed = int(timestamp)
        except (TypeError, ValueError):
            raise OutboxIntegrityError("KDF event timestamp is invalid")
        if parsed <= 0:
            raise OutboxIntegrityError("KDF event timestamp must be positive")
        return parsed
    return None


def _last_event_timestamp_ms(status: Mapping[str, Any]) -> int | None:
    events = status.get("events")
    if not isinstance(events, list):
        return None
    latest: int | None = None
    for item in events:
        if not isinstance(item, Mapping) or not isinstance(item.get("event"), Mapping):
            continue
        timestamp = item.get("timestamp")
        if timestamp is None:
            continue
        try:
            parsed = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise OutboxIntegrityError("KDF event timestamp is invalid") from exc
        if parsed <= 0:
            raise OutboxIntegrityError("KDF event timestamp must be positive")
        latest = parsed if latest is None else max(latest, parsed)
    return latest


def _outcome_terminal_event(status: Mapping[str, Any], *, success: bool) -> str:
    """Keep the economically decisive terminal event, not generic Finished."""
    events = status.get("events")
    event_types = []
    if isinstance(events, list):
        for item in events:
            event = item.get("event") if isinstance(item, Mapping) else None
            value = event.get("type") if isinstance(event, Mapping) else None
            if isinstance(value, str) and value:
                event_types.append(value)
    if not success:
        # A failed maker swap that emitted MakerPaymentSent is safe to release
        # only after the refund succeeded.  Keep the latest decisive refund
        # event so a transient failure followed by a successful retry is not
        # mistaken for an unrecovered payment (or vice versa).
        decisive = [
            value for value in event_types
            if value in {"MakerPaymentRefundFailed", "MakerPaymentRefunded"}
        ]
        if decisive:
            return decisive[-1]
    return event_types[-1] if event_types else "Finished"


def _swap_terms(status: Mapping[str, Any]) -> dict[str, str]:
    terms: dict[str, Any] = {
        "maker_coin": status.get("maker_coin"),
        "maker_amount": status.get("maker_amount"),
        "taker_coin": status.get("taker_coin"),
        "taker_amount": status.get("taker_amount"),
    }
    if any(value is None for value in terms.values()):
        events = status.get("events")
        if isinstance(events, list):
            for item in events:
                event = item.get("event") if isinstance(item, Mapping) else None
                if not isinstance(event, Mapping) or event.get("type") != "Started":
                    continue
                data = event.get("data")
                if isinstance(data, Mapping):
                    for key in terms:
                        terms[key] = terms[key] if terms[key] is not None else data.get(key)
                break
    if not isinstance(terms["maker_coin"], str) or not isinstance(
        terms["taker_coin"], str
    ):
        raise OutboxIntegrityError("KDF swap coin terms are missing")
    result = {
        "maker_coin": str(terms["maker_coin"]),
        "taker_coin": str(terms["taker_coin"]),
    }
    for key in ("maker_amount", "taker_amount"):
        try:
            amount = Decimal(str(terms[key]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OutboxIntegrityError(f"KDF swap {key} is invalid") from exc
        if not amount.is_finite() or amount <= 0:
            raise OutboxIntegrityError(f"KDF swap {key} must be positive")
        result[key] = str(amount)
    return result
