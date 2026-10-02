from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .models import DexSide, HedgeSide


class HedgeState(StrEnum):
    RESERVED = "RESERVED"
    HEDGE_READY = "HEDGE_READY"
    TESTING = "TESTING"
    TEST_VALIDATED = "TEST_VALIDATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    RECONCILED = "RECONCILED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class InventoryAdjustmentKind(StrEnum):
    ACQUIRE = "ACQUIRE"
    DISPOSE = "DISPOSE"


TERMINAL_STATES = {
    HedgeState.RECONCILED,
    HedgeState.REVIEW_REQUIRED,
    HedgeState.CANCELLED,
    HedgeState.FAILED,
}

_ALLOWED_TRANSITIONS: dict[HedgeState, set[HedgeState]] = {
    HedgeState.RESERVED: {
        HedgeState.HEDGE_READY,
        HedgeState.REVIEW_REQUIRED,
        HedgeState.CANCELLED,
        HedgeState.FAILED,
    },
    HedgeState.HEDGE_READY: {
        HedgeState.TESTING,
        HedgeState.SUBMITTING,
        HedgeState.REVIEW_REQUIRED,
        HedgeState.CANCELLED,
        HedgeState.FAILED,
    },
    HedgeState.TESTING: {
        HedgeState.TEST_VALIDATED,
        HedgeState.REVIEW_REQUIRED,
    },
    HedgeState.TEST_VALIDATED: {
        HedgeState.SUBMITTING,
        HedgeState.REVIEW_REQUIRED,
        HedgeState.CANCELLED,
    },
    HedgeState.REVIEW_REQUIRED: {HedgeState.RECONCILED},
    HedgeState.SUBMITTING: {
        HedgeState.SUBMITTED,
        HedgeState.UNKNOWN,
        HedgeState.PARTIAL,
        HedgeState.FILLED,
        HedgeState.FAILED,
    },
    HedgeState.UNKNOWN: {
        HedgeState.SUBMITTED,
        HedgeState.PARTIAL,
        HedgeState.FILLED,
        HedgeState.FAILED,
        HedgeState.RECONCILED,
    },
    HedgeState.SUBMITTED: {
        HedgeState.UNKNOWN,
        HedgeState.PARTIAL,
        HedgeState.FILLED,
        HedgeState.CANCELLED,
        HedgeState.FAILED,
    },
    HedgeState.PARTIAL: {
        HedgeState.HEDGE_READY,
        HedgeState.SUBMITTING,
        HedgeState.FILLED,
        HedgeState.REVIEW_REQUIRED,
        HedgeState.FAILED,
    },
    HedgeState.FILLED: {HedgeState.RECONCILED, HedgeState.FAILED},
    HedgeState.RECONCILED: set(),
    HedgeState.CANCELLED: set(),
    HedgeState.FAILED: {HedgeState.RECONCILED},
}


class JournalConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HedgeRecord:
    swap_uuid: str
    dex_side: DexSide
    hedge_side: HedgeSide
    target_quantity: Decimal
    filled_quantity: Decimal
    state: HedgeState
    last_error: str | None


@dataclass(frozen=True, slots=True)
class HedgeAttempt:
    swap_uuid: str
    sequence: int
    client_order_id: str
    hedge_side: HedgeSide
    requested_quantity: Decimal
    limit_price: Decimal
    status: str
    mexc_order_id: str | None
    executed_quantity: Decimal
    quote_quantity: Decimal

    @property
    def cex_order_id(self) -> str | None:
        """Venue-neutral alias; the stored column name is kept for migration safety."""
        return self.mexc_order_id


@dataclass(frozen=True, slots=True)
class ReceivedHedgeEvent:
    event_id: int
    swap_uuid: str
    order_uuid: str
    schema_version: int
    event_type: str
    market_id: str
    dex_side: DexSide
    hedge_side: HedgeSide
    hedge_symbol: str
    target_quantity: Decimal
    client_order_id: str
    trigger_event: str
    trigger_timestamp_ms: int
    event: Mapping[str, Any]
    signature: str
    acknowledged: bool
    acknowledged_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ReceivedSwapOutcome:
    event_id: int
    swap_uuid: str
    kdf_success: bool
    terminal_event: str
    completed_at_ms: int
    event: Mapping[str, Any]
    signature: str
    acknowledged: bool
    acknowledged_at_ms: int | None


@dataclass(frozen=True, slots=True)
class EconomicFee:
    swap_uuid: str
    fee_key: str
    venue: str
    asset: str
    amount: Decimal
    source: str
    occurred_at_ms: int | None


@dataclass(frozen=True, slots=True)
class MexcTradeFill:
    swap_uuid: str
    sequence: int
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: HedgeSide
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    commission: Decimal
    commission_asset: str | None
    traded_at_ms: int


@dataclass(frozen=True, slots=True)
class InventoryBaseline:
    baseline_key: str
    asset: str
    quantity: Decimal
    total_cost_usdt: Decimal
    observed_at_ms: int
    source: str
    note: str | None


@dataclass(frozen=True, slots=True)
class InventoryAdjustment:
    adjustment_key: str
    asset: str
    kind: InventoryAdjustmentKind
    quantity: Decimal
    total_cost_usdt: Decimal | None
    occurred_at_ms: int
    source: str
    note: str | None


def client_order_id_for(swap_uuid: str, sequence: int = 1) -> str:
    if not swap_uuid:
        raise ValueError("swap_uuid is required")
    if sequence <= 0 or sequence > 99:
        raise ValueError("sequence must be in [1, 99]")
    digest = hashlib.sha256(swap_uuid.encode("utf-8")).hexdigest()[:20]
    return f"kdfmm-{digest}-{sequence:02d}"


class HedgeJournal:
    def __init__(self, path: str | Path) -> None:
        database_path = None if str(path) == ":memory:" else Path(path)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if database_path is not None:
            os.chmod(database_path, 0o600)
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def record_runtime_sample(self, *, timings_ms, errors) -> None:
        """Operational diagnostics only: no balances, tokens or signed payloads."""
        with self._lock:
            self.connection.execute(
                'INSERT INTO runtime_samples(timings_ms,errors) VALUES (?,?)',
                (json.dumps(timings_ms, sort_keys=True), json.dumps(errors, sort_keys=True)),
            )

    def __enter__(self) -> "HedgeJournal":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
                timings_ms TEXT NOT NULL, errors TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hedges (
                swap_uuid TEXT PRIMARY KEY,
                dex_side TEXT NOT NULL,
                hedge_side TEXT NOT NULL,
                target_quantity TEXT NOT NULL,
                filled_quantity TEXT NOT NULL DEFAULT '0',
                state TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hedge_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                swap_uuid TEXT NOT NULL REFERENCES hedges(swap_uuid),
                sequence INTEGER NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                hedge_side TEXT NOT NULL,
                requested_quantity TEXT NOT NULL,
                limit_price TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PLANNED',
                mexc_order_id TEXT,
                executed_quantity TEXT NOT NULL DEFAULT '0',
                quote_quantity TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(swap_uuid, sequence)
            );

            CREATE TABLE IF NOT EXISTS received_hedge_events (
                event_id INTEGER PRIMARY KEY,
                swap_uuid TEXT NOT NULL UNIQUE REFERENCES hedges(swap_uuid),
                order_uuid TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                market_id TEXT NOT NULL,
                hedge_symbol TEXT NOT NULL,
                trigger_event TEXT NOT NULL,
                trigger_timestamp_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                signature TEXT NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_at_ms INTEGER,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS received_hedge_events_delivery
                ON received_hedge_events(acknowledged, event_id);

            CREATE TABLE IF NOT EXISTS received_swap_outcomes (
                event_id INTEGER PRIMARY KEY,
                swap_uuid TEXT NOT NULL UNIQUE REFERENCES hedges(swap_uuid),
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                trigger_event TEXT NOT NULL,
                completed_at_ms INTEGER NOT NULL,
                kdf_success INTEGER NOT NULL,
                terminal_event TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                signature TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_at_ms INTEGER,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS received_swap_outcomes_delivery
                ON received_swap_outcomes(acknowledged, event_id);

            CREATE TABLE IF NOT EXISTS economic_fees (
                fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
                swap_uuid TEXT NOT NULL REFERENCES hedges(swap_uuid),
                fee_key TEXT NOT NULL UNIQUE,
                venue TEXT NOT NULL,
                asset TEXT NOT NULL,
                amount TEXT NOT NULL,
                source TEXT NOT NULL,
                occurred_at_ms INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS economic_fees_swap
                ON economic_fees(swap_uuid, fee_id);

            CREATE TABLE IF NOT EXISTS mexc_trade_fills (
                fill_key TEXT PRIMARY KEY,
                swap_uuid TEXT NOT NULL REFERENCES hedges(swap_uuid),
                sequence INTEGER NOT NULL,
                trade_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                quote_quantity TEXT NOT NULL,
                commission TEXT NOT NULL,
                commission_asset TEXT,
                traded_at_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_id),
                FOREIGN KEY(swap_uuid, sequence)
                    REFERENCES hedge_attempts(swap_uuid, sequence)
            );

            CREATE INDEX IF NOT EXISTS mexc_trade_fills_swap
                ON mexc_trade_fills(swap_uuid, sequence, traded_at_ms);

            CREATE TABLE IF NOT EXISTS inventory_baselines (
                baseline_key TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                quantity TEXT NOT NULL,
                total_cost_usdt TEXT NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                source TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS inventory_baselines_asset_time
                ON inventory_baselines(asset, observed_at_ms);

            CREATE TABLE IF NOT EXISTS inventory_adjustments (
                adjustment_key TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                kind TEXT NOT NULL,
                quantity TEXT NOT NULL,
                total_cost_usdt TEXT,
                occurred_at_ms INTEGER NOT NULL,
                source TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS inventory_adjustments_asset_time
                ON inventory_adjustments(asset, occurred_at_ms);
            """
        )

    def record_received_event(
        self,
        *,
        event_id: int,
        swap_uuid: str,
        order_uuid: str,
        schema_version: int,
        event_type: str,
        market_id: str,
        dex_side: DexSide,
        hedge_side: HedgeSide,
        hedge_symbol: str,
        target_quantity: Decimal,
        trigger_event: str,
        trigger_timestamp_ms: int,
        event: Mapping[str, Any],
        signature: str,
    ) -> ReceivedHedgeEvent:
        """Persist an authenticated VPS event and reserve its hedge atomically."""
        if event_id <= 0 or schema_version <= 0 or trigger_timestamp_ms <= 0:
            raise ValueError("event identifiers and timestamps must be positive")
        required_strings = {
            "swap_uuid": swap_uuid,
            "order_uuid": order_uuid,
            "event_type": event_type,
            "market_id": market_id,
            "hedge_symbol": hedge_symbol,
            "trigger_event": trigger_event,
            "signature": signature,
        }
        if any(not value for value in required_strings.values()):
            raise ValueError("received event fields cannot be empty")
        if not target_quantity.is_finite() or target_quantity <= 0:
            raise ValueError("target_quantity must be positive and finite")
        expected_hedge_side = (
            HedgeSide.BUY if dex_side is DexSide.SELL_ARRR else HedgeSide.SELL
        )
        if hedge_side is not expected_hedge_side:
            raise JournalConflict("hedge side is inconsistent with the DEX side")

        payload_json = json.dumps(
            dict(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        client_order_id = client_order_id_for(swap_uuid)

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = self._received_row(event_id=event_id)
                if existing_row is not None:
                    existing = self._received_from_row(existing_row)
                    if (
                        payload_json != existing_row["payload_json"]
                        or signature != existing.signature
                        or client_order_id != existing.client_order_id
                    ):
                        raise JournalConflict(
                            "event ID already exists with different signed details"
                        )
                    self.connection.execute("COMMIT")
                    return existing

                if self._outcome_row(event_id=event_id) is not None:
                    raise JournalConflict(
                        "event ID already belongs to a swap outcome"
                    )

                swap_row = self._received_row(swap_uuid=swap_uuid)
                if swap_row is not None:
                    raise JournalConflict(
                        "swap UUID already belongs to another received event"
                    )

                hedge_row = self.connection.execute(
                    "SELECT * FROM hedges WHERE swap_uuid = ?", (swap_uuid,)
                ).fetchone()
                if hedge_row is None:
                    self.connection.execute(
                        """
                        INSERT INTO hedges
                            (swap_uuid, dex_side, hedge_side, target_quantity, state)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            swap_uuid,
                            dex_side.value,
                            hedge_side.value,
                            str(target_quantity),
                            HedgeState.RESERVED.value,
                        ),
                    )
                else:
                    hedge = self._hedge_from_row(hedge_row)
                    if (
                        hedge.dex_side is not dex_side
                        or hedge.hedge_side is not hedge_side
                        or hedge.target_quantity != target_quantity
                    ):
                        raise JournalConflict(
                            "swap UUID already exists with different hedge details"
                        )

                self.connection.execute(
                    """
                    INSERT INTO received_hedge_events
                        (event_id, swap_uuid, order_uuid, schema_version,
                         event_type, market_id, hedge_symbol, trigger_event,
                         trigger_timestamp_ms, payload_json, signature,
                         client_order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        swap_uuid,
                        order_uuid,
                        schema_version,
                        event_type,
                        market_id,
                        hedge_symbol,
                        trigger_event,
                        trigger_timestamp_ms,
                        payload_json,
                        signature,
                        client_order_id,
                    ),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

        received = self.received_event(event_id)
        assert received is not None
        return received

    def received_event(self, event_id: int) -> ReceivedHedgeEvent | None:
        if event_id <= 0:
            raise ValueError("event_id must be positive")
        with self._lock:
            row = self._received_row(event_id=event_id)
        return self._received_from_row(row) if row is not None else None

    def record_received_outcome(
        self,
        *,
        event_id: int,
        swap_uuid: str,
        schema_version: int,
        event_type: str,
        trigger_event: str,
        completed_at_ms: int,
        kdf_success: bool,
        terminal_event: str,
        event: Mapping[str, Any],
        signature: str,
    ) -> ReceivedSwapOutcome:
        if event_id <= 0 or schema_version <= 0 or completed_at_ms <= 0:
            raise ValueError("outcome identifiers and timestamps must be positive")
        if not all(
            (swap_uuid, event_type, trigger_event, terminal_event, signature)
        ):
            raise ValueError("outcome event fields cannot be empty")
        if not isinstance(kdf_success, bool):
            raise ValueError("kdf_success must be a boolean")
        payload_json = json.dumps(
            dict(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing_any = self.received_delivery_event(event_id)
                if existing_any is not None:
                    if not isinstance(existing_any, ReceivedSwapOutcome):
                        raise JournalConflict(
                            "event ID already belongs to another event type"
                        )
                    row = self._outcome_row(event_id=event_id)
                    assert row is not None
                    if (
                        payload_json != row["payload_json"]
                        or signature != existing_any.signature
                    ):
                        raise JournalConflict(
                            "outcome event ID already exists with different details"
                        )
                    self.connection.execute("COMMIT")
                    return existing_any

                hedge_event = self.received_event_for_swap(swap_uuid)
                if hedge_event is None:
                    raise JournalConflict(
                        "swap outcome arrived before its hedge event"
                    )
                existing_swap = self._outcome_row(swap_uuid=swap_uuid)
                if existing_swap is not None:
                    raise JournalConflict(
                        "swap already has a different outcome event"
                    )
                for key in (
                    "order_uuid",
                    "market_id",
                    "dex_side",
                    "hedge_side",
                    "hedge_symbol",
                    "arrr_quantity",
                    "quote_ticker",
                    "kdf_maker_coin",
                    "kdf_maker_amount",
                    "kdf_taker_coin",
                    "kdf_taker_amount",
                    "quote_usdt_rate",
                    "quote_usdt_symbol",
                    "quote_usdt_side",
                    "quote_usdt_observed_at_ms",
                ):
                    if event.get(key) != hedge_event.event.get(key):
                        raise JournalConflict(
                            "swap outcome does not match its signed hedge terms"
                        )
                self.connection.execute(
                    """
                    INSERT INTO received_swap_outcomes
                        (event_id, swap_uuid, schema_version, event_type,
                         trigger_event, completed_at_ms, kdf_success,
                         terminal_event, payload_json, signature)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        swap_uuid,
                        schema_version,
                        event_type,
                        trigger_event,
                        completed_at_ms,
                        int(kdf_success),
                        terminal_event,
                        payload_json,
                        signature,
                    ),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
        outcome = self.outcome_for_swap(swap_uuid)
        assert outcome is not None
        return outcome

    def outcome_for_swap(self, swap_uuid: str) -> ReceivedSwapOutcome | None:
        if not swap_uuid:
            raise ValueError("swap_uuid is required")
        with self._lock:
            row = self._outcome_row(swap_uuid=swap_uuid)
        return self._outcome_from_row(row) if row is not None else None

    def received_delivery_event(
        self, event_id: int
    ) -> ReceivedHedgeEvent | ReceivedSwapOutcome | None:
        hedge = self.received_event(event_id)
        if hedge is not None:
            return hedge
        with self._lock:
            row = self._outcome_row(event_id=event_id)
        return self._outcome_from_row(row) if row is not None else None

    def received_event_for_swap(self, swap_uuid: str) -> ReceivedHedgeEvent | None:
        if not swap_uuid:
            raise ValueError("swap_uuid is required")
        with self._lock:
            row = self._received_row(swap_uuid=swap_uuid)
        return self._received_from_row(row) if row is not None else None

    def pending_event_acknowledgements(
        self,
    ) -> tuple[ReceivedHedgeEvent | ReceivedSwapOutcome, ...]:
        with self._lock:
            hedge_rows = self.connection.execute(
                """
                SELECT r.*, h.dex_side, h.hedge_side, h.target_quantity
                FROM received_hedge_events AS r
                JOIN hedges AS h USING (swap_uuid)
                WHERE r.acknowledged = 0
                ORDER BY r.event_id
                """
            ).fetchall()
            outcome_rows = self.connection.execute(
                """
                SELECT * FROM received_swap_outcomes
                WHERE acknowledged = 0 ORDER BY event_id
                """
            ).fetchall()
        pending: list[ReceivedHedgeEvent | ReceivedSwapOutcome] = [
            *(self._received_from_row(row) for row in hedge_rows),
            *(self._outcome_from_row(row) for row in outcome_rows),
        ]
        return tuple(sorted(pending, key=lambda item: item.event_id))

    def mark_event_acknowledged(
        self, event_id: int, *, acknowledged_at_ms: int
    ) -> ReceivedHedgeEvent | ReceivedSwapOutcome:
        if event_id <= 0 or acknowledged_at_ms <= 0:
            raise ValueError("event_id and acknowledged_at_ms must be positive")
        with self._lock, self.connection:
            table = (
                "received_hedge_events"
                if self.received_event(event_id) is not None
                else "received_swap_outcomes"
            )
            cursor = self.connection.execute(
                """
                UPDATE {table}
                SET acknowledged = 1,
                    acknowledged_at_ms = COALESCE(acknowledged_at_ms, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """.format(table=table),
                (acknowledged_at_ms, event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(event_id)
        received = self.received_delivery_event(event_id)
        assert received is not None
        return received

    def record_economic_fee(
        self,
        *,
        swap_uuid: str,
        fee_key: str,
        venue: str,
        asset: str,
        amount: Decimal,
        source: str,
        occurred_at_ms: int | None = None,
    ) -> EconomicFee:
        if self.get(swap_uuid) is None:
            raise KeyError(swap_uuid)
        if not all((fee_key, venue, asset, source)):
            raise ValueError("economic fee fields cannot be empty")
        if venue not in {"KDF", "MEXC"}:
            raise ValueError("economic fee venue must be KDF or MEXC")
        if not amount.is_finite() or amount <= 0:
            raise ValueError("economic fee amount must be positive and finite")
        if occurred_at_ms is not None and occurred_at_ms <= 0:
            raise ValueError("economic fee timestamp must be positive")
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM economic_fees WHERE fee_key = ?", (fee_key,)
            ).fetchone()
            if row is not None:
                existing = self._fee_from_row(row)
                expected = EconomicFee(
                    swap_uuid=swap_uuid,
                    fee_key=fee_key,
                    venue=venue,
                    asset=asset,
                    amount=amount,
                    source=source,
                    occurred_at_ms=occurred_at_ms,
                )
                if existing != expected:
                    raise JournalConflict(
                        "fee key already exists with different economic details"
                    )
                return existing
            self.connection.execute(
                """
                INSERT INTO economic_fees
                    (swap_uuid, fee_key, venue, asset, amount, source,
                     occurred_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    swap_uuid,
                    fee_key,
                    venue,
                    asset,
                    str(amount),
                    source,
                    occurred_at_ms,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM economic_fees WHERE fee_key = ?", (fee_key,)
            ).fetchone()
        assert row is not None
        return self._fee_from_row(row)

    def record_mexc_trade_fill(
        self,
        *,
        swap_uuid: str,
        sequence: int,
        trade_id: str,
        order_id: str,
        client_order_id: str,
        symbol: str,
        side: HedgeSide,
        price: Decimal,
        quantity: Decimal,
        quote_quantity: Decimal,
        commission: Decimal,
        commission_asset: str | None,
        traded_at_ms: int,
    ) -> MexcTradeFill:
        attempt = self.get_attempt(swap_uuid, sequence)
        if attempt is None:
            raise KeyError((swap_uuid, sequence))
        if not all((trade_id, order_id, client_order_id, symbol)):
            raise ValueError("MEXC fill identities cannot be empty")
        if client_order_id != attempt.client_order_id:
            raise JournalConflict("MEXC fill client order ID does not match attempt")
        if attempt.mexc_order_id is not None and order_id != attempt.mexc_order_id:
            raise JournalConflict("MEXC fill order ID does not match attempt")
        if side is not attempt.hedge_side:
            raise JournalConflict("MEXC fill side does not match attempt")
        for amount, name in (
            (price, "price"),
            (quantity, "quantity"),
            (quote_quantity, "quote quantity"),
        ):
            if not amount.is_finite() or amount <= 0:
                raise ValueError(f"MEXC fill {name} must be positive and finite")
        if not commission.is_finite() or commission < 0:
            raise ValueError("MEXC fill commission cannot be negative")
        if commission > 0 and not commission_asset:
            raise ValueError("MEXC fill commission asset is required")
        if traded_at_ms <= 0:
            raise ValueError("MEXC fill timestamp must be positive")
        symbol = symbol.upper()
        fill_key = f"{symbol}:{trade_id}"
        expected = MexcTradeFill(
            swap_uuid=swap_uuid,
            sequence=sequence,
            trade_id=trade_id,
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            quote_quantity=quote_quantity,
            commission=commission,
            commission_asset=commission_asset,
            traded_at_ms=traded_at_ms,
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM mexc_trade_fills WHERE fill_key = ?", (fill_key,)
            ).fetchone()
            if row is not None:
                existing = self._fill_from_row(row)
                if existing != expected:
                    raise JournalConflict(
                        "MEXC trade ID already exists with different fill details"
                    )
                return existing
            already = self.connection.execute(
                """
                SELECT quantity FROM mexc_trade_fills
                WHERE swap_uuid = ? AND sequence = ?
                """,
                (swap_uuid, sequence),
            ).fetchall()
            total = sum(
                (Decimal(str(item["quantity"])) for item in already), start=Decimal("0")
            )
            if total + quantity > attempt.requested_quantity:
                raise JournalConflict(
                    "imported MEXC fills exceed requested attempt quantity"
                )
            self.connection.execute(
                """
                INSERT INTO mexc_trade_fills
                    (fill_key, swap_uuid, sequence, trade_id, order_id,
                     client_order_id, symbol, side, price, quantity,
                     quote_quantity, commission, commission_asset, traded_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_key,
                    swap_uuid,
                    sequence,
                    trade_id,
                    order_id,
                    client_order_id,
                    symbol,
                    side.value,
                    str(price),
                    str(quantity),
                    str(quote_quantity),
                    str(commission),
                    commission_asset,
                    traded_at_ms,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM mexc_trade_fills WHERE fill_key = ?", (fill_key,)
            ).fetchone()
        assert row is not None
        return self._fill_from_row(row)

    def mexc_trade_fills(
        self, swap_uuid: str | None = None
    ) -> tuple[MexcTradeFill, ...]:
        with self._lock:
            if swap_uuid is None:
                rows = self.connection.execute(
                    "SELECT * FROM mexc_trade_fills ORDER BY traded_at_ms, fill_key"
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM mexc_trade_fills
                    WHERE swap_uuid = ? ORDER BY traded_at_ms, fill_key
                    """,
                    (swap_uuid,),
                ).fetchall()
        return tuple(self._fill_from_row(row) for row in rows)

    def record_inventory_baseline(
        self,
        *,
        baseline_key: str,
        asset: str,
        quantity: Decimal,
        total_cost_usdt: Decimal,
        observed_at_ms: int,
        source: str,
        note: str | None = None,
    ) -> InventoryBaseline:
        if not all((baseline_key, asset, source)):
            raise ValueError("inventory baseline fields cannot be empty")
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("inventory baseline quantity must be positive and finite")
        if not total_cost_usdt.is_finite() or total_cost_usdt < 0:
            raise ValueError("inventory baseline cost cannot be negative")
        if observed_at_ms <= 0:
            raise ValueError("inventory baseline timestamp must be positive")
        if note is not None and len(note) > 500:
            raise ValueError("inventory baseline note is too long")
        expected = InventoryBaseline(
            baseline_key=baseline_key,
            asset=asset.upper(),
            quantity=quantity,
            total_cost_usdt=total_cost_usdt,
            observed_at_ms=observed_at_ms,
            source=source,
            note=note,
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM inventory_baselines WHERE baseline_key = ?",
                (baseline_key,),
            ).fetchone()
            if row is not None:
                existing = self._baseline_from_row(row)
                if existing != expected:
                    raise JournalConflict(
                        "baseline key already exists with different inventory details"
                    )
                return existing
            self.connection.execute(
                """
                INSERT INTO inventory_baselines
                    (baseline_key, asset, quantity, total_cost_usdt,
                     observed_at_ms, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expected.baseline_key,
                    expected.asset,
                    str(expected.quantity),
                    str(expected.total_cost_usdt),
                    expected.observed_at_ms,
                    expected.source,
                    expected.note,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM inventory_baselines WHERE baseline_key = ?",
                (baseline_key,),
            ).fetchone()
        assert row is not None
        return self._baseline_from_row(row)

    def inventory_baselines(
        self, asset: str | None = None
    ) -> tuple[InventoryBaseline, ...]:
        with self._lock:
            if asset is None:
                rows = self.connection.execute(
                    """
                    SELECT * FROM inventory_baselines
                    ORDER BY observed_at_ms, baseline_key
                    """
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM inventory_baselines WHERE asset = ?
                    ORDER BY observed_at_ms, baseline_key
                    """,
                    (asset.upper(),),
                ).fetchall()
        return tuple(self._baseline_from_row(row) for row in rows)

    def record_inventory_adjustment(
        self,
        *,
        adjustment_key: str,
        asset: str,
        kind: InventoryAdjustmentKind,
        quantity: Decimal,
        total_cost_usdt: Decimal | None,
        occurred_at_ms: int,
        source: str,
        note: str | None = None,
    ) -> InventoryAdjustment:
        if not all((adjustment_key, asset, source)):
            raise ValueError("inventory adjustment fields cannot be empty")
        if not isinstance(kind, InventoryAdjustmentKind):
            raise ValueError("inventory adjustment kind is invalid")
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("inventory adjustment quantity must be positive and finite")
        if kind is InventoryAdjustmentKind.ACQUIRE:
            if (
                total_cost_usdt is None
                or not total_cost_usdt.is_finite()
                or total_cost_usdt < 0
            ):
                raise ValueError(
                    "inventory acquisition requires a non-negative total cost"
                )
        elif total_cost_usdt is not None:
            raise ValueError("inventory disposal cost is calculated automatically")
        if occurred_at_ms <= 0:
            raise ValueError("inventory adjustment timestamp must be positive")
        if note is not None and len(note) > 500:
            raise ValueError("inventory adjustment note is too long")
        expected = InventoryAdjustment(
            adjustment_key=adjustment_key,
            asset=asset.upper(),
            kind=kind,
            quantity=quantity,
            total_cost_usdt=total_cost_usdt,
            occurred_at_ms=occurred_at_ms,
            source=source,
            note=note,
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM inventory_adjustments WHERE adjustment_key = ?",
                (adjustment_key,),
            ).fetchone()
            if row is not None:
                existing = self._adjustment_from_row(row)
                if existing != expected:
                    raise JournalConflict(
                        "adjustment key already exists with different inventory details"
                    )
                return existing
            self.connection.execute(
                """
                INSERT INTO inventory_adjustments
                    (adjustment_key, asset, kind, quantity, total_cost_usdt,
                     occurred_at_ms, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expected.adjustment_key,
                    expected.asset,
                    expected.kind.value,
                    str(expected.quantity),
                    (
                        str(expected.total_cost_usdt)
                        if expected.total_cost_usdt is not None
                        else None
                    ),
                    expected.occurred_at_ms,
                    expected.source,
                    expected.note,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM inventory_adjustments WHERE adjustment_key = ?",
                (adjustment_key,),
            ).fetchone()
        assert row is not None
        return self._adjustment_from_row(row)

    def inventory_adjustments(
        self, asset: str | None = None
    ) -> tuple[InventoryAdjustment, ...]:
        with self._lock:
            if asset is None:
                rows = self.connection.execute(
                    """
                    SELECT * FROM inventory_adjustments
                    ORDER BY occurred_at_ms, adjustment_key
                    """
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM inventory_adjustments WHERE asset = ?
                    ORDER BY occurred_at_ms, adjustment_key
                    """,
                    (asset.upper(),),
                ).fetchall()
        return tuple(self._adjustment_from_row(row) for row in rows)

    def economic_fees(self, swap_uuid: str | None = None) -> tuple[EconomicFee, ...]:
        with self._lock:
            if swap_uuid is None:
                rows = self.connection.execute(
                    "SELECT * FROM economic_fees ORDER BY fee_id"
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM economic_fees
                    WHERE swap_uuid = ? ORDER BY fee_id
                    """,
                    (swap_uuid,),
                ).fetchall()
        return tuple(self._fee_from_row(row) for row in rows)

    def received_event_cursor(self) -> int:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT MAX(event_id) FROM (
                    SELECT event_id FROM received_hedge_events
                    UNION ALL
                    SELECT event_id FROM received_swap_outcomes
                )
                """
            ).fetchone()
        return int(row[0] or 0)

    def received_event_status(self) -> dict[str, int]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END), 0)
                           AS pending_acknowledgement
                FROM (
                    SELECT acknowledged FROM received_hedge_events
                    UNION ALL
                    SELECT acknowledged FROM received_swap_outcomes
                )
                """
            ).fetchone()
        return {
            "total": int(row["total"]),
            "pending_acknowledgement": int(row["pending_acknowledgement"]),
            "acknowledged": int(row["total"] - row["pending_acknowledgement"]),
            "cursor": self.received_event_cursor(),
        }

    def _received_row(
        self,
        *,
        event_id: int | None = None,
        swap_uuid: str | None = None,
    ) -> sqlite3.Row | None:
        if (event_id is None) == (swap_uuid is None):
            raise ValueError("select exactly one received event identity")
        column = "r.event_id" if event_id is not None else "r.swap_uuid"
        value: int | str = event_id if event_id is not None else str(swap_uuid)
        return self.connection.execute(
            f"""
            SELECT r.*, h.dex_side, h.hedge_side, h.target_quantity
            FROM received_hedge_events AS r
            JOIN hedges AS h USING (swap_uuid)
            WHERE {column} = ?
            """,
            (value,),
        ).fetchone()

    def _outcome_row(
        self,
        *,
        event_id: int | None = None,
        swap_uuid: str | None = None,
    ) -> sqlite3.Row | None:
        if (event_id is None) == (swap_uuid is None):
            raise ValueError("select exactly one outcome identity")
        column = "event_id" if event_id is not None else "swap_uuid"
        value: int | str = event_id if event_id is not None else str(swap_uuid)
        return self.connection.execute(
            f"SELECT * FROM received_swap_outcomes WHERE {column} = ?",
            (value,),
        ).fetchone()

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> ReceivedSwapOutcome:
        try:
            event = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise JournalConflict("stored outcome event is not valid JSON") from exc
        if not isinstance(event, Mapping):
            raise JournalConflict("stored outcome event is not a JSON object")
        return ReceivedSwapOutcome(
            event_id=int(row["event_id"]),
            swap_uuid=str(row["swap_uuid"]),
            kdf_success=bool(row["kdf_success"]),
            terminal_event=str(row["terminal_event"]),
            completed_at_ms=int(row["completed_at_ms"]),
            event=dict(event),
            signature=str(row["signature"]),
            acknowledged=bool(row["acknowledged"]),
            acknowledged_at_ms=(
                int(row["acknowledged_at_ms"])
                if row["acknowledged_at_ms"] is not None
                else None
            ),
        )

    @staticmethod
    def _fee_from_row(row: sqlite3.Row) -> EconomicFee:
        return EconomicFee(
            swap_uuid=str(row["swap_uuid"]),
            fee_key=str(row["fee_key"]),
            venue=str(row["venue"]),
            asset=str(row["asset"]),
            amount=Decimal(str(row["amount"])),
            source=str(row["source"]),
            occurred_at_ms=(
                int(row["occurred_at_ms"])
                if row["occurred_at_ms"] is not None
                else None
            ),
        )

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> MexcTradeFill:
        return MexcTradeFill(
            swap_uuid=str(row["swap_uuid"]),
            sequence=int(row["sequence"]),
            trade_id=str(row["trade_id"]),
            order_id=str(row["order_id"]),
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            side=HedgeSide(str(row["side"])),
            price=Decimal(str(row["price"])),
            quantity=Decimal(str(row["quantity"])),
            quote_quantity=Decimal(str(row["quote_quantity"])),
            commission=Decimal(str(row["commission"])),
            commission_asset=(
                str(row["commission_asset"])
                if row["commission_asset"] is not None
                else None
            ),
            traded_at_ms=int(row["traded_at_ms"]),
        )

    @staticmethod
    def _baseline_from_row(row: sqlite3.Row) -> InventoryBaseline:
        return InventoryBaseline(
            baseline_key=str(row["baseline_key"]),
            asset=str(row["asset"]),
            quantity=Decimal(str(row["quantity"])),
            total_cost_usdt=Decimal(str(row["total_cost_usdt"])),
            observed_at_ms=int(row["observed_at_ms"]),
            source=str(row["source"]),
            note=str(row["note"]) if row["note"] is not None else None,
        )

    @staticmethod
    def _adjustment_from_row(row: sqlite3.Row) -> InventoryAdjustment:
        return InventoryAdjustment(
            adjustment_key=str(row["adjustment_key"]),
            asset=str(row["asset"]),
            kind=InventoryAdjustmentKind(str(row["kind"])),
            quantity=Decimal(str(row["quantity"])),
            total_cost_usdt=(
                Decimal(str(row["total_cost_usdt"]))
                if row["total_cost_usdt"] is not None
                else None
            ),
            occurred_at_ms=int(row["occurred_at_ms"]),
            source=str(row["source"]),
            note=str(row["note"]) if row["note"] is not None else None,
        )

    @staticmethod
    def _received_from_row(row: sqlite3.Row) -> ReceivedHedgeEvent:
        try:
            event = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise JournalConflict("stored received event is not valid JSON") from exc
        if not isinstance(event, Mapping):
            raise JournalConflict("stored received event is not a JSON object")
        return ReceivedHedgeEvent(
            event_id=int(row["event_id"]),
            swap_uuid=str(row["swap_uuid"]),
            order_uuid=str(row["order_uuid"]),
            schema_version=int(row["schema_version"]),
            event_type=str(row["event_type"]),
            market_id=str(row["market_id"]),
            dex_side=DexSide(row["dex_side"]),
            hedge_side=HedgeSide(row["hedge_side"]),
            hedge_symbol=str(row["hedge_symbol"]),
            target_quantity=Decimal(row["target_quantity"]),
            client_order_id=str(row["client_order_id"]),
            trigger_event=str(row["trigger_event"]),
            trigger_timestamp_ms=int(row["trigger_timestamp_ms"]),
            event=dict(event),
            signature=str(row["signature"]),
            acknowledged=bool(row["acknowledged"]),
            acknowledged_at_ms=(
                int(row["acknowledged_at_ms"])
                if row["acknowledged_at_ms"] is not None
                else None
            ),
        )

    def reserve(
        self,
        *,
        swap_uuid: str,
        dex_side: DexSide,
        target_quantity: Decimal,
    ) -> HedgeRecord:
        if not swap_uuid:
            raise ValueError("swap_uuid is required")
        if target_quantity <= 0:
            raise ValueError("target_quantity must be positive")
        hedge_side = HedgeSide.BUY if dex_side is DexSide.SELL_ARRR else HedgeSide.SELL

        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM hedges WHERE swap_uuid = ?", (swap_uuid,)
            ).fetchone()
            if row is not None:
                existing = self._hedge_from_row(row)
                if (
                    existing.dex_side is not dex_side
                    or existing.target_quantity != target_quantity
                ):
                    raise JournalConflict("swap UUID already exists with different hedge details")
                return existing
            self.connection.execute(
                """
                INSERT INTO hedges
                    (swap_uuid, dex_side, hedge_side, target_quantity, state)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    swap_uuid,
                    dex_side.value,
                    hedge_side.value,
                    str(target_quantity),
                    HedgeState.RESERVED.value,
                ),
            )
        record = self.get(swap_uuid)
        assert record is not None
        return record

    def get(self, swap_uuid: str) -> HedgeRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM hedges WHERE swap_uuid = ?", (swap_uuid,)
            ).fetchone()
        return self._hedge_from_row(row) if row is not None else None

    def hedges(
        self, *, states: set[HedgeState] | None = None
    ) -> tuple[HedgeRecord, ...]:
        with self._lock:
            if states is None:
                rows = self.connection.execute(
                    "SELECT * FROM hedges ORDER BY created_at, swap_uuid"
                ).fetchall()
            elif not states:
                return ()
            else:
                values = tuple(sorted(state.value for state in states))
                placeholders = ",".join("?" for _ in values)
                rows = self.connection.execute(
                    f"SELECT * FROM hedges WHERE state IN ({placeholders}) "
                    "ORDER BY created_at, swap_uuid",
                    values,
                ).fetchall()
        return tuple(self._hedge_from_row(row) for row in rows)

    def transition(
        self,
        swap_uuid: str,
        new_state: HedgeState,
        *,
        filled_quantity: Decimal | None = None,
        error: str | None = None,
    ) -> HedgeRecord:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM hedges WHERE swap_uuid = ?", (swap_uuid,)
            ).fetchone()
            if row is None:
                raise KeyError(swap_uuid)
            current = HedgeState(row["state"])
            if new_state is current:
                return self._hedge_from_row(row)
            if new_state not in _ALLOWED_TRANSITIONS[current]:
                raise JournalConflict(f"invalid hedge transition: {current} -> {new_state}")

            new_filled = Decimal(row["filled_quantity"])
            if filled_quantity is not None:
                if filled_quantity < new_filled:
                    raise JournalConflict("filled quantity cannot decrease")
                if filled_quantity > Decimal(row["target_quantity"]):
                    raise JournalConflict("filled quantity exceeds target")
                new_filled = filled_quantity
            self.connection.execute(
                """
                UPDATE hedges
                SET state = ?, filled_quantity = ?, last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE swap_uuid = ?
                """,
                (new_state.value, str(new_filled), error, swap_uuid),
            )
        record = self.get(swap_uuid)
        assert record is not None
        return record

    def resolve_attention(self, swap_uuid: str, *, note: str) -> HedgeRecord:
        selected_note = note.strip()
        if not selected_note or len(selected_note) > 500:
            raise ValueError("manual resolution note must contain 1-500 characters")
        hedge = self.get(swap_uuid)
        if hedge is None:
            raise KeyError(swap_uuid)
        if hedge.state not in {
            HedgeState.REVIEW_REQUIRED,
            HedgeState.UNKNOWN,
            HedgeState.FAILED,
        }:
            raise JournalConflict(
                f"hedge {swap_uuid} is not awaiting manual exposure resolution"
            )
        return self.transition(
            swap_uuid,
            HedgeState.RECONCILED,
            error=f"manual exposure resolution: {selected_note}",
        )

    def create_attempt(
        self,
        *,
        swap_uuid: str,
        sequence: int,
        requested_quantity: Decimal,
        limit_price: Decimal,
    ) -> HedgeAttempt:
        hedge = self.get(swap_uuid)
        if hedge is None:
            raise KeyError(swap_uuid)
        if requested_quantity <= 0 or limit_price <= 0:
            raise ValueError("attempt quantity and price must be positive")
        client_order_id = client_order_id_for(swap_uuid, sequence)

        with self.connection:
            row = self.connection.execute(
                """
                SELECT * FROM hedge_attempts
                WHERE swap_uuid = ? AND sequence = ?
                """,
                (swap_uuid, sequence),
            ).fetchone()
            if row is not None:
                existing = self._attempt_from_row(row)
                if (
                    existing.requested_quantity != requested_quantity
                    or existing.limit_price != limit_price
                    or existing.hedge_side is not hedge.hedge_side
                ):
                    raise JournalConflict("hedge attempt already exists with different details")
                return existing
            self.connection.execute(
                """
                INSERT INTO hedge_attempts
                    (swap_uuid, sequence, client_order_id, hedge_side,
                     requested_quantity, limit_price)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    swap_uuid,
                    sequence,
                    client_order_id,
                    hedge.hedge_side.value,
                    str(requested_quantity),
                    str(limit_price),
                ),
            )
        attempt = self.get_attempt(swap_uuid, sequence)
        assert attempt is not None
        return attempt

    def get_attempt(self, swap_uuid: str, sequence: int) -> HedgeAttempt | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM hedge_attempts WHERE swap_uuid = ? AND sequence = ?",
                (swap_uuid, sequence),
            ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def attempts(self, swap_uuid: str) -> tuple[HedgeAttempt, ...]:
        if not swap_uuid:
            raise ValueError("swap_uuid is required")
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM hedge_attempts WHERE swap_uuid = ? ORDER BY sequence",
                (swap_uuid,),
            ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def latest_attempt(self, swap_uuid: str) -> HedgeAttempt | None:
        attempts = self.attempts(swap_uuid)
        return attempts[-1] if attempts else None

    def total_executed(self, swap_uuid: str) -> Decimal:
        rows = self.connection.execute(
            "SELECT executed_quantity FROM hedge_attempts WHERE swap_uuid = ?",
            (swap_uuid,),
        ).fetchall()
        return sum((Decimal(row["executed_quantity"]) for row in rows), start=Decimal("0"))

    def update_attempt(
        self,
        *,
        swap_uuid: str,
        sequence: int,
        status: str,
        mexc_order_id: str | None = None,
        executed_quantity: Decimal | None = None,
        quote_quantity: Decimal | None = None,
    ) -> HedgeAttempt:
        current = self.get_attempt(swap_uuid, sequence)
        if current is None:
            raise KeyError((swap_uuid, sequence))
        executed = current.executed_quantity if executed_quantity is None else executed_quantity
        quote = current.quote_quantity if quote_quantity is None else quote_quantity
        if executed < current.executed_quantity or quote < current.quote_quantity:
            raise JournalConflict("attempt execution totals cannot decrease")
        if executed > current.requested_quantity:
            raise JournalConflict("attempt execution exceeds requested quantity")
        self.connection.execute(
            """
            UPDATE hedge_attempts
            SET status = ?, mexc_order_id = COALESCE(?, mexc_order_id),
                executed_quantity = ?, quote_quantity = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE swap_uuid = ? AND sequence = ?
            """,
            (status, mexc_order_id, str(executed), str(quote), swap_uuid, sequence),
        )
        updated = self.get_attempt(swap_uuid, sequence)
        assert updated is not None
        return updated

    @staticmethod
    def _hedge_from_row(row: sqlite3.Row) -> HedgeRecord:
        return HedgeRecord(
            swap_uuid=row["swap_uuid"],
            dex_side=DexSide(row["dex_side"]),
            hedge_side=HedgeSide(row["hedge_side"]),
            target_quantity=Decimal(row["target_quantity"]),
            filled_quantity=Decimal(row["filled_quantity"]),
            state=HedgeState(row["state"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> HedgeAttempt:
        return HedgeAttempt(
            swap_uuid=row["swap_uuid"],
            sequence=row["sequence"],
            client_order_id=row["client_order_id"],
            hedge_side=HedgeSide(row["hedge_side"]),
            requested_quantity=Decimal(row["requested_quantity"]),
            limit_price=Decimal(row["limit_price"]),
            status=row["status"],
            mexc_order_id=row["mexc_order_id"],
            executed_quantity=Decimal(row["executed_quantity"]),
            quote_quantity=Decimal(row["quote_quantity"]),
        )
