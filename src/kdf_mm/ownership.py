from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from .models import DexSide, QuotePlan


class OwnedOrderStatus(StrEnum):
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    ERROR = "ERROR"


class OwnedSwapState(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OrderOwnershipConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnedOrder:
    order_uuid: str
    dex_side: DexSide
    kdf_base: str
    kdf_rel: str
    kdf_price: Decimal
    kdf_volume: Decimal
    status: OwnedOrderStatus
    last_error: str | None
    missing_polls: int = 0
    market_id: str = ""
    inventory_pool: str = ""
    # KDF's available_amount can shrink when sibling maker orders share the
    # same wallet pool.  Keep the advertised bounds separate so that a
    # liquidity-multiplier allocation is not mistaken for a smaller quote.
    kdf_max_volume: Decimal = Decimal("0")
    kdf_min_volume: Decimal = Decimal("0")
    hedging_enabled: bool = True
    market_reference_required: bool = True

    @property
    def advertised_volume(self) -> Decimal:
        return self.kdf_max_volume if self.kdf_max_volume > 0 else self.kdf_volume


@dataclass(frozen=True, slots=True)
class OwnedSwap:
    swap_uuid: str
    order_uuid: str
    dex_side: DexSide
    arrr_quantity: Decimal
    state: OwnedSwapState
    last_event: str
    acknowledged: bool
    market_id: str = ""
    inventory_pool: str = ""

    @property
    def base_quantity(self) -> Decimal:
        return self.arrr_quantity


class OrderOwnershipStore:
    """Registro degli UUID creati dal bot; impedisce cancellazioni globali."""

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        database_path = None if str(path) == ":memory:" else Path(path)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        if database_path is not None:
            os.chmod(database_path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS owned_orders (
                order_uuid TEXT PRIMARY KEY,
                dex_side TEXT NOT NULL,
                kdf_base TEXT NOT NULL,
                kdf_rel TEXT NOT NULL,
                kdf_price TEXT NOT NULL,
                kdf_volume TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(owned_orders)")
        }
        for name in ('reason_source', 'strategy_id'):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE owned_orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        for name in ('hedging_enabled', 'market_reference_required'):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE owned_orders ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1")
        self.connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS owned_protection_immutable
            BEFORE UPDATE OF hedging_enabled,market_reference_required ON owned_orders
            WHEN NEW.hedging_enabled != OLD.hedging_enabled OR NEW.market_reference_required != OLD.market_reference_required
            BEGIN SELECT RAISE(ABORT,'Immutable maker UUID protection policy'); END;
        """)
        # Append-only audit. Triggers make state and its event one atomic write,
        # including publications and state changes discovered by reconciliation.
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
                order_uuid TEXT NOT NULL, strategy_id TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                previous_state TEXT, state TEXT, reason TEXT, detail TEXT
            );
            CREATE INDEX IF NOT EXISTS order_events_uuid ON order_events(order_uuid, id);
            CREATE TRIGGER IF NOT EXISTS order_created_audit AFTER INSERT ON owned_orders BEGIN
                INSERT INTO order_events(order_uuid,strategy_id,event,source,state,reason)
                VALUES(NEW.order_uuid,NEW.strategy_id,'PUBLISHED','ownership',NEW.status,NEW.last_error);
            END;
            CREATE TRIGGER IF NOT EXISTS order_state_audit AFTER UPDATE ON owned_orders
            WHEN NEW.status != OLD.status OR NEW.last_error IS NOT OLD.last_error BEGIN
                INSERT INTO order_events(order_uuid,strategy_id,event,source,previous_state,state,reason)
                VALUES(NEW.order_uuid,NEW.strategy_id,
                    CASE WHEN NEW.status != OLD.status THEN 'STATE_CHANGED' ELSE 'REASON_UPDATED' END,
                    NEW.reason_source,OLD.status,NEW.status,NEW.last_error);
            END;
            CREATE TRIGGER IF NOT EXISTS order_binding_audit AFTER UPDATE OF strategy_id ON owned_orders
            WHEN NEW.strategy_id != OLD.strategy_id BEGIN
                INSERT INTO order_events(order_uuid,strategy_id,event,source,state)
                VALUES(NEW.order_uuid,NEW.strategy_id,'STRATEGY_BOUND','strategy',NEW.status);
            END;
        """)
        if "missing_polls" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN missing_polls INTEGER NOT NULL DEFAULT 0"
            )
        if "missing_since_at" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN missing_since_at TEXT"
            )
            # A pre-upgrade observation has no trustworthy start time. Give it
            # a fresh grace period rather than treating the order's age as one.
            self.connection.execute(
                """UPDATE owned_orders
                   SET missing_since_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
                   WHERE status = 'OPEN' AND missing_polls > 0"""
            )
        if "market_id" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN market_id TEXT NOT NULL DEFAULT ''"
            )
        if "inventory_pool" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN inventory_pool TEXT NOT NULL DEFAULT ''"
            )
        if "kdf_max_volume" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN kdf_max_volume TEXT NOT NULL DEFAULT ''"
            )
        if "kdf_min_volume" not in columns:
            self.connection.execute(
                "ALTER TABLE owned_orders ADD COLUMN kdf_min_volume TEXT NOT NULL DEFAULT '0'"
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS owned_swaps (
                swap_uuid TEXT PRIMARY KEY,
                order_uuid TEXT NOT NULL,
                dex_side TEXT NOT NULL,
                arrr_quantity TEXT NOT NULL,
                state TEXT NOT NULL,
                last_event TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS owned_swaps_side_state
                ON owned_swaps(dex_side, state, acknowledged);
            """
        )
        swap_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(owned_swaps)")
        }
        if "market_id" not in swap_columns:
            self.connection.execute(
                "ALTER TABLE owned_swaps ADD COLUMN market_id TEXT NOT NULL DEFAULT ''"
            )
        if "inventory_pool" not in swap_columns:
            self.connection.execute(
                "ALTER TABLE owned_swaps ADD COLUMN inventory_pool TEXT NOT NULL DEFAULT ''"
            )
        self.connection.executescript(
            """
            UPDATE owned_orders
            SET market_id = CASE
                WHEN dex_side = 'SELL_ARRR' THEN 'ARRR-' || kdf_rel
                ELSE 'ARRR-' || kdf_base
            END
            WHERE market_id = '';
            UPDATE owned_orders SET inventory_pool = kdf_base
            WHERE inventory_pool = '';
            UPDATE owned_orders SET kdf_max_volume = kdf_volume
            WHERE kdf_max_volume = '';
            UPDATE owned_swaps
            SET market_id = COALESCE(
                    (SELECT market_id FROM owned_orders
                     WHERE owned_orders.order_uuid = owned_swaps.order_uuid), ''),
                inventory_pool = COALESCE(
                    (SELECT inventory_pool FROM owned_orders
                     WHERE owned_orders.order_uuid = owned_swaps.order_uuid), '')
            WHERE market_id = '' OR inventory_pool = '';
            CREATE INDEX IF NOT EXISTS owned_orders_market_side_status
                ON owned_orders(market_id, dex_side, status);
            CREATE INDEX IF NOT EXISTS owned_orders_pool_status
                ON owned_orders(inventory_pool, status);
            CREATE INDEX IF NOT EXISTS owned_orders_status
                ON owned_orders(status);
            CREATE INDEX IF NOT EXISTS owned_swaps_pool_state
                ON owned_swaps(inventory_pool, state, acknowledged);
            CREATE INDEX IF NOT EXISTS owned_swaps_order_uuid
                ON owned_swaps(order_uuid);
            """
        )

    def close(self) -> None:
        self.connection.close()

    def register(
        self, order_uuid: str, plan: QuotePlan, *, min_volume: Decimal | None = None,
    ) -> OwnedOrder:
        if type(plan.hedging_enabled) is not bool or type(plan.market_reference_required) is not bool:
            raise ValueError("Invalid immutable maker protection policy")
        if not order_uuid:
            raise ValueError("order_uuid is required")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM owned_orders WHERE order_uuid = ?", (order_uuid,)
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                expected = (
                    plan.dex_side,
                    plan.kdf_base,
                    plan.kdf_rel,
                    plan.kdf_price,
                    plan.kdf_volume,
                    plan.market_id or _market_id(plan.dex_side, plan.kdf_base, plan.kdf_rel),
                    plan.inventory_pool or plan.kdf_base,
                    min_volume or Decimal("0"),
                    plan.hedging_enabled, plan.market_reference_required,
                )
                actual = (
                    existing.dex_side,
                    existing.kdf_base,
                    existing.kdf_rel,
                    existing.kdf_price,
                    existing.advertised_volume,
                    existing.market_id,
                    existing.inventory_pool,
                    existing.kdf_min_volume,
                    existing.hedging_enabled, existing.market_reference_required,
                )
                if actual != expected:
                    raise OrderOwnershipConflict(
                        "KDF order UUID already exists with different quote details"
                    )
                return existing
            self.connection.execute(
                """
                INSERT INTO owned_orders
                    (order_uuid, dex_side, kdf_base, kdf_rel,
                     kdf_price, kdf_volume, status, market_id, inventory_pool,
                     kdf_max_volume, kdf_min_volume, hedging_enabled, market_reference_required)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_uuid,
                    plan.dex_side.value,
                    plan.kdf_base,
                    plan.kdf_rel,
                    str(plan.kdf_price),
                    str(plan.kdf_volume),
                    OwnedOrderStatus.OPEN.value,
                    plan.market_id or _market_id(plan.dex_side, plan.kdf_base, plan.kdf_rel),
                    plan.inventory_pool or plan.kdf_base,
                    str(plan.kdf_volume),
                    str(min_volume or Decimal("0")),
                    int(plan.hedging_enabled), int(plan.market_reference_required),
                ),
            )
        order = self.get(order_uuid)
        assert order is not None
        return order

    def get(self, order_uuid: str) -> OwnedOrder | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM owned_orders WHERE order_uuid = ?", (order_uuid,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def order_age_seconds(self, order_uuid: str) -> float:
        """Wall-clock age of a durable order, including across service restarts."""
        with self._lock:
            row = self.connection.execute(
                """
                SELECT (julianday('now') - julianday(created_at)) * 86400
                FROM owned_orders WHERE order_uuid = ?
                """,
                (order_uuid,),
            ).fetchone()
        if row is None or row[0] is None:
            raise KeyError(order_uuid)
        return max(0.0, float(row[0]))

    def missing_age_seconds(self, order_uuid: str) -> float | None:
        """Elapsed time since this order first became unavailable, not since publication."""
        with self._lock:
            row = self.connection.execute(
                """SELECT (julianday('now') - julianday(missing_since_at)) * 86400
                   FROM owned_orders WHERE order_uuid = ?""",
                (order_uuid,),
            ).fetchone()
        if row is None:
            raise KeyError(order_uuid)
        return None if row[0] is None else max(0.0, float(row[0]))

    def active(self) -> tuple[OwnedOrder, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_orders
                WHERE status = ? ORDER BY created_at, order_uuid
                """,
                (OwnedOrderStatus.OPEN.value,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def error_orders(self) -> tuple[OwnedOrder, ...]:
        """Orders awaiting readback; never silently treat them as cancelled."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM owned_orders WHERE status = ? ORDER BY created_at, order_uuid",
                (OwnedOrderStatus.ERROR.value,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def completed_swap_orders(self) -> tuple[OwnedOrder, ...]:
        """Previously matched UUIDs that may remain live after a partial swap."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT o.* FROM owned_orders AS o
                WHERE o.status = ? AND EXISTS (
                    SELECT 1 FROM owned_swaps AS s WHERE s.order_uuid = o.order_uuid
                )
                ORDER BY o.created_at, o.order_uuid
                """,
                (OwnedOrderStatus.COMPLETED.value,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def active_for_market_side(
        self, market_id: str, dex_side: DexSide
    ) -> tuple[OwnedOrder, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_orders
                WHERE market_id = ? AND dex_side = ? AND status = ?
                ORDER BY created_at, order_uuid
                """,
                (market_id, dex_side.value, OwnedOrderStatus.OPEN.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def active_for_pool(self, inventory_pool: str) -> tuple[OwnedOrder, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_orders
                WHERE inventory_pool = ? AND status = ?
                ORDER BY created_at, order_uuid
                """,
                (inventory_pool, OwnedOrderStatus.OPEN.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def problem_orders_for_side(self, dex_side: DexSide) -> tuple[OwnedOrder, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_orders
                WHERE dex_side = ? AND (status = ? OR missing_polls > 0)
                ORDER BY created_at, order_uuid
                """,
                (dex_side.value, OwnedOrderStatus.ERROR.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def problem_orders_for_pool(self, inventory_pool: str) -> tuple[OwnedOrder, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_orders
                WHERE inventory_pool = ? AND (status = ? OR missing_polls > 0)
                ORDER BY created_at, order_uuid
                """,
                (inventory_pool, OwnedOrderStatus.ERROR.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def note_seen(
        self,
        order_uuid: str,
        *,
        kdf_price: Decimal,
        kdf_volume: Decimal,
        kdf_max_volume: Decimal | None = None,
        kdf_min_volume: Decimal | None = None,
    ) -> OwnedOrder:
        if kdf_price <= 0 or kdf_volume <= 0:
            raise ValueError("observed KDF order price and volume must be positive")
        current = self.get(order_uuid)
        maximum = (kdf_max_volume if kdf_max_volume is not None else
                   current.advertised_volume if current is not None else kdf_volume)
        minimum = (kdf_min_volume if kdf_min_volume is not None else
                   current.kdf_min_volume if current is not None else Decimal("0"))
        if maximum < kdf_volume or minimum < 0 or minimum > maximum:
            raise ValueError("observed KDF order volume bounds are invalid")
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET kdf_price = ?, kdf_volume = ?, kdf_max_volume = ?,
                    kdf_min_volume = ?, missing_polls = 0, missing_since_at = NULL,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE order_uuid = ? AND status = ?
                """,
                (
                    str(kdf_price),
                    str(kdf_volume),
                    str(maximum),
                    str(minimum),
                    order_uuid,
                    OwnedOrderStatus.OPEN.value,
                ),
            )
        observed = self.get(order_uuid)
        if observed is None:
            raise KeyError(order_uuid)
        # A concurrent cancellation wins over an older polling snapshot.
        return observed

    def restore_seen(self, order_uuid: str, *, kdf_price: Decimal,
                     kdf_volume: Decimal, kdf_max_volume: Decimal | None = None,
                     kdf_min_volume: Decimal | None = None) -> OwnedOrder:
        """Restore a missing/matched UUID only after a fresh KDF readback."""
        if not all(value.is_finite() and value > 0 for value in (kdf_price, kdf_volume)):
            raise ValueError("observed KDF order price and volume must be positive")
        current = self.get(order_uuid)
        maximum = (kdf_max_volume if kdf_max_volume is not None else
                   current.advertised_volume if current is not None else kdf_volume)
        minimum = (kdf_min_volume if kdf_min_volume is not None else
                   current.kdf_min_volume if current is not None else Decimal("0"))
        if maximum < kdf_volume or minimum < 0 or minimum > maximum:
            raise ValueError("observed KDF order volume bounds are invalid")
        with self._lock:
            self.connection.execute(
                """
                UPDATE owned_orders
                SET status = ?, kdf_price = ?, kdf_volume = ?,
                    kdf_max_volume = ?, kdf_min_volume = ?,
                    missing_polls = 0, missing_since_at = NULL, last_error = NULL,
                    reason_source = 'kdf_readback', updated_at = CURRENT_TIMESTAMP
                WHERE order_uuid = ? AND status IN (?, ?)
                """,
                (OwnedOrderStatus.OPEN.value, str(kdf_price), str(kdf_volume),
                 str(maximum), str(minimum),
                 order_uuid, OwnedOrderStatus.ERROR.value, OwnedOrderStatus.COMPLETED.value),
            )
        restored = self.get(order_uuid)
        if restored is None:
            raise KeyError(order_uuid)
        return restored

    def note_missing(
        self, order_uuid: str, *,
        reason: str = "owned order is missing from KDF",
    ) -> OwnedOrder:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET missing_since_at = CASE
                        WHEN missing_polls = 0 OR last_error IS NOT ?
                        THEN strftime('%Y-%m-%d %H:%M:%f', 'now')
                        ELSE COALESCE(missing_since_at,
                            strftime('%Y-%m-%d %H:%M:%f', 'now')) END,
                    missing_polls = CASE WHEN last_error IS ?
                        THEN missing_polls + 1 ELSE 1 END,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE order_uuid = ? AND status = ?
                """,
                (reason, reason, reason, order_uuid, OwnedOrderStatus.OPEN.value),
            )
        missing = self.get(order_uuid)
        if missing is None:
            raise KeyError(order_uuid)
        return missing

    def resolve_order_errors_for_side(self, dex_side: DexSide) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET status = ?, missing_polls = 0, updated_at = CURRENT_TIMESTAMP
                WHERE dex_side = ? AND status = ?
                """,
                (
                    OwnedOrderStatus.CANCELLED.value,
                    dex_side.value,
                    OwnedOrderStatus.ERROR.value,
                ),
            )
        return cursor.rowcount

    def resolve_order_errors_for_pool(self, inventory_pool: str) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET status = ?, missing_polls = 0, updated_at = CURRENT_TIMESTAMP
                WHERE inventory_pool = ? AND status = ?
                """,
                (
                    OwnedOrderStatus.CANCELLED.value,
                    inventory_pool,
                    OwnedOrderStatus.ERROR.value,
                ),
            )
        return cursor.rowcount

    def update_quote(
        self,
        order_uuid: str,
        *,
        kdf_price: Decimal,
        kdf_volume: Decimal,
        kdf_max_volume: Decimal | None = None,
        kdf_min_volume: Decimal | None = None,
    ) -> OwnedOrder:
        if kdf_price <= 0 or kdf_volume <= 0:
            raise ValueError("order price and volume must be positive")
        maximum = kdf_max_volume if kdf_max_volume is not None else kdf_volume
        minimum = kdf_min_volume
        if maximum < kdf_volume or (minimum is not None and (minimum < 0 or minimum > maximum)):
            raise ValueError("order volume bounds are invalid")
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET kdf_price = ?, kdf_volume = ?, kdf_max_volume = ?,
                    kdf_min_volume = COALESCE(?, kdf_min_volume),
                    updated_at = CURRENT_TIMESTAMP
                WHERE order_uuid = ? AND status = ?
                """,
                (
                    str(kdf_price),
                    str(kdf_volume),
                    str(maximum),
                    str(minimum) if minimum is not None else None,
                    order_uuid,
                    OwnedOrderStatus.OPEN.value,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(order_uuid)
        updated = self.get(order_uuid)
        assert updated is not None
        return updated

    def mark(
        self,
        order_uuid: str,
        status: OwnedOrderStatus,
        *,
        error: str | None = None,
        source: str = 'reconciliation',
        strategy_id: str = '',
        only_if_open: bool = False,
    ) -> OwnedOrder:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE owned_orders
                SET status = ?, last_error = CASE
                        WHEN ? IS NOT NULL AND ? != '' THEN ?
                        WHEN last_error IS NOT NULL THEN last_error
                        WHEN ? = 'CANCELLED' THEN 'Causa non disponibile da KDF'
                        ELSE NULL END,
                    reason_source = CASE WHEN ? IS NOT NULL AND ? != '' OR last_error IS NULL THEN ? ELSE reason_source END,
                    strategy_id = CASE WHEN ? != '' THEN ? ELSE strategy_id END,
                    missing_polls = 0, missing_since_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE order_uuid = ? AND (? = 0 OR status = 'OPEN')
                """,
                (status.value, error, error, error, status.value, error, error, source,
                 strategy_id, strategy_id, order_uuid, int(only_if_open)),
            )
        updated = self.get(order_uuid)
        if updated is None:
            raise KeyError(order_uuid)
        return updated

    def mark_late_matched(self, order_uuid: str) -> OwnedOrder:
        """Clear a premature ERROR only after the reconciler linked a KDF swap."""
        with self._lock:
            self.connection.execute(
                """UPDATE owned_orders
                   SET status = 'COMPLETED', last_error = NULL,
                       missing_polls = 0, missing_since_at = NULL,
                       reason_source = 'late_swap_readback',
                       updated_at = CURRENT_TIMESTAMP
                   WHERE order_uuid = ? AND status = 'ERROR'
                     AND EXISTS (SELECT 1 FROM owned_swaps
                                 WHERE owned_swaps.order_uuid = owned_orders.order_uuid
                                   AND state = 'ACTIVE')""",
                (order_uuid,),
            )
        updated = self.get(order_uuid)
        if updated is None:
            raise KeyError(order_uuid)
        return updated

    def record_order_event(self, order_uuid, event, *, source, reason, strategy_id='', detail=None):
        with self._lock:
            self.connection.execute(
                'INSERT INTO order_events(order_uuid,strategy_id,event,source,reason,detail) VALUES(?,?,?,?,?,?)',
                (order_uuid, strategy_id, event, source, reason, detail))

    def bind_strategy(self, order_uuid, strategy_id):
        with self._lock:
            row = self.connection.execute('SELECT strategy_id FROM owned_orders WHERE order_uuid=?', (order_uuid,)).fetchone()
            if row is None:
                raise KeyError(order_uuid)
            if row['strategy_id'] and row['strategy_id'] != strategy_id:
                raise OrderOwnershipConflict('Order already bound to another strategy')
            self.connection.execute('UPDATE owned_orders SET strategy_id=? WHERE order_uuid=?', (strategy_id, order_uuid))

    def upsert_swap(
        self,
        *,
        swap_uuid: str,
        order_uuid: str,
        dex_side: DexSide,
        arrr_quantity: Decimal,
        state: OwnedSwapState,
        last_event: str,
        market_id: str = "",
        inventory_pool: str = "",
    ) -> OwnedSwap:
        if not swap_uuid or not order_uuid or not last_event:
            raise ValueError("swap UUID, order UUID and last event are required")
        if not arrr_quantity.is_finite() or arrr_quantity <= 0:
            raise ValueError("swap base quantity must be positive and finite")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM owned_swaps WHERE swap_uuid = ?", (swap_uuid,)
            ).fetchone()
            if row is not None:
                existing = self._swap_from_row(row)
                if (
                    existing.order_uuid != order_uuid
                    or existing.dex_side is not dex_side
                    or existing.arrr_quantity != arrr_quantity
                    or (market_id and existing.market_id != market_id)
                    or (inventory_pool and existing.inventory_pool != inventory_pool)
                ):
                    raise OrderOwnershipConflict(
                        "swap UUID already exists with different ownership details"
                    )
                self.connection.execute(
                    """
                    UPDATE owned_swaps
                    SET acknowledged = CASE WHEN state != ? THEN 0 ELSE acknowledged END,
                        state = ?, last_event = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE swap_uuid = ?
                    """,
                    (state.value, state.value, last_event, swap_uuid),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO owned_swaps
                        (swap_uuid, order_uuid, dex_side, arrr_quantity,
                         state, last_event, market_id, inventory_pool)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        swap_uuid,
                        order_uuid,
                        dex_side.value,
                        str(arrr_quantity),
                        state.value,
                        last_event,
                        market_id,
                        inventory_pool,
                    ),
                )
        swap = self.get_swap(swap_uuid)
        assert swap is not None
        return swap

    def get_swap(self, swap_uuid: str) -> OwnedSwap | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM owned_swaps WHERE swap_uuid = ?", (swap_uuid,)
            ).fetchone()
        return self._swap_from_row(row) if row is not None else None

    def swaps(self, *, limit: int = 100) -> tuple[OwnedSwap, ...]:
        if limit <= 0:
            raise ValueError("swap limit must be positive")
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_swaps
                ORDER BY updated_at DESC, swap_uuid LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._swap_from_row(row) for row in rows)

    def swaps_for_order(self, order_uuid: str) -> tuple[OwnedSwap, ...]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM owned_swaps
                WHERE order_uuid = ? ORDER BY updated_at DESC, swap_uuid
                """,
                (order_uuid,),
            ).fetchall()
        return tuple(self._swap_from_row(row) for row in rows)

    def blocking_swap_for_side(self, dex_side: DexSide) -> OwnedSwap | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM owned_swaps
                WHERE dex_side = ? AND (state = ? OR acknowledged = 0)
                ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END,
                         updated_at DESC, swap_uuid
                LIMIT 1
                """,
                (
                    dex_side.value,
                    OwnedSwapState.ACTIVE.value,
                    OwnedSwapState.ACTIVE.value,
                ),
            ).fetchone()
        return self._swap_from_row(row) if row is not None else None

    def blocking_swap_for_pool(self, inventory_pool: str) -> OwnedSwap | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM owned_swaps
                WHERE inventory_pool = ? AND (state = ? OR acknowledged = 0)
                ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END,
                         updated_at DESC, swap_uuid
                LIMIT 1
                """,
                (
                    inventory_pool,
                    OwnedSwapState.ACTIVE.value,
                    OwnedSwapState.ACTIVE.value,
                ),
            ).fetchone()
        return self._swap_from_row(row) if row is not None else None

    def swap_state_counts(self) -> tuple[int, int]:
        """Return active swaps and unacknowledged terminal swaps without a limit."""
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN state = ? THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN state != ? AND acknowledged = 0
                                      THEN 1 ELSE 0 END), 0)
                FROM owned_swaps
                """,
                (OwnedSwapState.ACTIVE.value, OwnedSwapState.ACTIVE.value),
            ).fetchone()
        return int(row[0]), int(row[1])

    def acknowledge_swaps_for_side(self, dex_side: DexSide) -> int:
        with self._lock:
            active = self.connection.execute(
                """
                SELECT COUNT(*) FROM owned_swaps
                WHERE dex_side = ? AND state = ?
                """,
                (dex_side.value, OwnedSwapState.ACTIVE.value),
            ).fetchone()[0]
            if active:
                raise ValueError("cannot acknowledge a side with an active swap")
            cursor = self.connection.execute(
                """
                UPDATE owned_swaps
                SET acknowledged = 1, updated_at = CURRENT_TIMESTAMP
                WHERE dex_side = ? AND state != ? AND acknowledged = 0
                """,
                (dex_side.value, OwnedSwapState.ACTIVE.value),
            )
        return cursor.rowcount

    def acknowledge_swap(self, swap_uuid: str) -> None:
        """Acknowledge one proven successful swap, never unrelated pool swaps."""
        with self._lock:
            swap = self.get_swap(swap_uuid)
            if swap is None or swap.state is not OwnedSwapState.SUCCEEDED:
                raise ValueError("only a successful swap can be auto-acknowledged")
            self.connection.execute("UPDATE owned_swaps SET acknowledged=1 WHERE swap_uuid=?", (swap_uuid,))

    def acknowledge_refunded_swap(
        self, swap_uuid: str, *, terminal_event: str
    ) -> None:
        """Acknowledge one failed swap only after an explicit maker refund proof."""
        if terminal_event != "MakerPaymentRefunded":
            raise ValueError("failed swap requires MakerPaymentRefunded proof")
        with self._lock:
            swap = self.get_swap(swap_uuid)
            if swap is None or swap.state is not OwnedSwapState.FAILED:
                raise ValueError("only a failed swap with a verified refund can be acknowledged")
            self.connection.execute(
                "UPDATE owned_swaps SET acknowledged=1, updated_at=CURRENT_TIMESTAMP "
                "WHERE swap_uuid=?",
                (swap_uuid,),
            )

    def acknowledge_swaps_for_pool(self, inventory_pool: str) -> int:
        with self._lock:
            active = self.connection.execute(
                """
                SELECT COUNT(*) FROM owned_swaps
                WHERE inventory_pool = ? AND state = ?
                """,
                (inventory_pool, OwnedSwapState.ACTIVE.value),
            ).fetchone()[0]
            if active:
                raise ValueError("cannot acknowledge a pool with an active swap")
            cursor = self.connection.execute(
                """
                UPDATE owned_swaps
                SET acknowledged = 1, updated_at = CURRENT_TIMESTAMP
                WHERE inventory_pool = ? AND state != ? AND acknowledged = 0
                """,
                (inventory_pool, OwnedSwapState.ACTIVE.value),
            )
        return cursor.rowcount

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OwnedOrder:
        return OwnedOrder(
            order_uuid=row["order_uuid"],
            dex_side=DexSide(row["dex_side"]),
            kdf_base=row["kdf_base"],
            kdf_rel=row["kdf_rel"],
            kdf_price=Decimal(row["kdf_price"]),
            kdf_volume=Decimal(row["kdf_volume"]),
            status=OwnedOrderStatus(row["status"]),
            last_error=row["last_error"],
            missing_polls=int(row["missing_polls"]),
            market_id=row["market_id"],
            inventory_pool=row["inventory_pool"],
            kdf_max_volume=Decimal(row["kdf_max_volume"] or row["kdf_volume"]),
            kdf_min_volume=Decimal(row["kdf_min_volume"] or "0"),
            hedging_enabled=bool(row["hedging_enabled"]),
            market_reference_required=bool(row["market_reference_required"]),
        )

    @staticmethod
    def _swap_from_row(row: sqlite3.Row) -> OwnedSwap:
        return OwnedSwap(
            swap_uuid=row["swap_uuid"],
            order_uuid=row["order_uuid"],
            dex_side=DexSide(row["dex_side"]),
            arrr_quantity=Decimal(row["arrr_quantity"]),
            state=OwnedSwapState(row["state"]),
            last_event=row["last_event"],
            acknowledged=bool(row["acknowledged"]),
            market_id=row["market_id"],
            inventory_pool=row["inventory_pool"],
        )


def _market_id(dex_side: DexSide, kdf_base: str, kdf_rel: str) -> str:
    if dex_side is DexSide.SELL_ARRR:
        return f"{kdf_base}-{kdf_rel}"
    return f"{kdf_rel}-{kdf_base}"
