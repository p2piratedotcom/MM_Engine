"""Durable strategy budgets; repricing and restarts never reset consumption."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .strategy import StrategySpec


class StrategyStore:
    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'PAUSED', detail TEXT NOT NULL DEFAULT '',
                last_write REAL NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT 'null',
                confirmations INTEGER NOT NULL DEFAULT 0, preview TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS strategy_orders (
                order_uuid TEXT PRIMARY KEY, strategy_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_consumption (
                swap_uuid TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                sold TEXT NOT NULL, observed_at REAL NOT NULL, outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
                strategy_id TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
                confirmations INTEGER NOT NULL, preview TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_update_intents (
                strategy_id TEXT PRIMARY KEY,
                order_uuid TEXT NOT NULL,
                old_price TEXT NOT NULL,
                old_volume TEXT NOT NULL,
                new_price TEXT NOT NULL,
                new_volume TEXT NOT NULL,
                min_volume TEXT,
                requested_at REAL NOT NULL,
                cancel_requested_at REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS strategy_safety_cooldowns (
                strategy_id TEXT PRIMARY KEY,
                last_cancel REAL NOT NULL,
                streak INTEGER NOT NULL,
                resume_after REAL NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_book_samples (
                symbol TEXT NOT NULL, sequence INTEGER NOT NULL,
                observed_at_ms INTEGER NOT NULL, best_bid TEXT NOT NULL,
                best_ask TEXT NOT NULL, bid_depth_1pct TEXT NOT NULL,
                ask_depth_1pct TEXT NOT NULL, bid_depth_total TEXT NOT NULL,
                ask_depth_total TEXT NOT NULL, quantity_step TEXT NOT NULL,
                min_quote_amount TEXT NOT NULL, bid_levels INTEGER NOT NULL,
                ask_levels INTEGER NOT NULL, timings_ms TEXT NOT NULL,
                PRIMARY KEY(symbol, observed_at_ms, sequence)
            );
            CREATE INDEX IF NOT EXISTS market_book_samples_time
                ON market_book_samples(observed_at_ms);
            CREATE TRIGGER IF NOT EXISTS strategy_state_audit AFTER UPDATE ON strategies
            WHEN NEW.state != OLD.state OR NEW.detail != OLD.detail
            BEGIN
                INSERT INTO strategy_events(strategy_id,state,detail,confirmations,preview)
                VALUES(NEW.id,NEW.state,NEW.detail,NEW.confirmations,NEW.preview);
            END;
        """)
        self._market_sample_writes = 0

    def record_market_book_sample(self, sample: dict[str, Any]) -> None:
        """Store compact public-book evidence for later depth/threshold analysis."""
        fields = ("symbol", "sequence", "observed_at_ms", "best_bid", "best_ask",
                  "bid_depth_1pct", "ask_depth_1pct", "bid_depth_total",
                  "ask_depth_total", "quantity_step", "min_quote_amount",
                  "bid_levels", "ask_levels")
        values = tuple(sample[field] for field in fields)
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO market_book_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, json.dumps(sample["timings_ms"], sort_keys=True)),
            )
            self._market_sample_writes += 1
            if self._market_sample_writes % 100 == 0:
                self.db.execute("DELETE FROM market_book_samples WHERE observed_at_ms < ?",
                    (int(self.clock() * 1000) - 7 * 86400_000,))

    def note_safety_withdrawal(self, strategy_id: str, source: str) -> None:
        """Persist adaptive re-entry delay only after a confirmed safety cancel."""
        if not strategy_id or source not in {"strategy_safety", "market_data", "hedge_depth", "coverage"}:
            return
        now = self.clock()
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT last_cancel,streak FROM strategy_safety_cooldowns WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()
            streak = min(4, old["streak"] + 1) if old and 0 <= now - old["last_cancel"] < 900 else 1
            delay = min(300, 60 * 2 ** (streak - 1))
            self.db.execute(
                """INSERT INTO strategy_safety_cooldowns
                   (strategy_id,last_cancel,streak,resume_after,source) VALUES (?,?,?,?,?)
                   ON CONFLICT(strategy_id) DO UPDATE SET
                   last_cancel=excluded.last_cancel,streak=excluded.streak,
                   resume_after=excluded.resume_after,source=excluded.source""",
                (strategy_id, now, streak, now + delay, source),
            )

    def safety_cooldown(self, strategy_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM strategy_safety_cooldowns WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()
        return dict(row) if row else None

    def begin_update(self, strategy_id: str, order_uuid: str, *, old_price: Decimal,
                     old_volume: Decimal, new_price: Decimal, new_volume: Decimal,
                     min_volume: Decimal | None) -> None:
        """Persist the exact KDF update before its RPC is sent."""
        if not order_uuid or min(old_price, old_volume, new_price, new_volume) <= 0:
            raise ValueError("invalid maker order update intent")
        with self.lock:
            previous = self.db.execute(
                "SELECT state FROM strategy_update_intents WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
            if previous and previous['state'] != 'DONE':
                raise ValueError("unresolved KDF order update already exists")
            self.db.execute("""
                INSERT INTO strategy_update_intents
                    (strategy_id,order_uuid,old_price,old_volume,new_price,new_volume,
                     min_volume,requested_at,state,detail)
                VALUES (?,?,?,?,?,?,?,?, 'PENDING','')
                ON CONFLICT(strategy_id) DO UPDATE SET
                    order_uuid=excluded.order_uuid,old_price=excluded.old_price,
                    old_volume=excluded.old_volume,new_price=excluded.new_price,
                    new_volume=excluded.new_volume,min_volume=excluded.min_volume,
                    requested_at=excluded.requested_at,cancel_requested_at=0,
                    state='PENDING',detail=''
            """, (strategy_id, order_uuid, str(old_price), str(old_volume),
                  str(new_price), str(new_volume),
                  str(min_volume) if min_volume is not None else None, self.clock()))

    def update_intent(self, strategy_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM strategy_update_intents WHERE strategy_id=? AND state!='DONE'",
                (strategy_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_update_intent_state(self, strategy_id: str, state: str, detail: str = '') -> None:
        if state not in {'PENDING', 'HELD', 'DONE'}:
            raise ValueError("invalid update intent state")
        with self.lock:
            result = self.db.execute(
                "UPDATE strategy_update_intents SET state=?,detail=? WHERE strategy_id=? AND state!='DONE'",
                (state, detail, strategy_id),
            )
            if result.rowcount != 1:
                raise KeyError(strategy_id)

    def note_update_cancel(self, strategy_id: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE strategy_update_intents SET cancel_requested_at=? WHERE strategy_id=? AND state='PENDING'",
                (self.clock(), strategy_id),
            )

    def close(self) -> None:
        self.db.close()

    def rows(self, *, include_deleted=False) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM strategies" + ("" if include_deleted else " WHERE state != 'DELETED'") + " ORDER BY id").fetchall()
        return [{**dict(row), "spec": json.loads(row["spec"]), "preview": json.loads(row["preview"])} for row in rows]

    def get(self, strategy_id: str) -> dict[str, Any]:
        for row in self.rows(include_deleted=True):
            if row["id"] == strategy_id:
                return row
        raise KeyError(strategy_id)

    def create_group(self, specs: tuple[StrategySpec, ...], *, scaled=False) -> None:
        if not specs or len(specs) > 2:
            raise ValueError("creare una strategia o una coppia di strategie")
        keys = [(s.market_id, s.side) for s in specs]
        if len(set(keys)) != len(keys):
            raise ValueError("un solo ordine per mercato e lato")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = [StrategySpec.from_payload(row["spec"]) for row in self.rows()]
                if all(any(old == new for old in existing) for new in specs):
                    self.db.execute("COMMIT")
                    return
                # Canonical reverse representations must not bypass one-per-side.
                pairs = {(s.sold.ticker, s.bought.ticker) for s in existing}
                for spec in specs:
                    if self.db.execute("SELECT 1 FROM strategies WHERE id=? AND state='DELETED'", (spec.strategy_id,)).fetchone():
                        raise ValueError("identificativo archiviato: creare una nuova strategia")
                    pair = (spec.sold.ticker, spec.bought.ticker)
                    if pair in pairs:
                        siblings = [s for s in existing if (s.sold.ticker, s.bought.ticker) == pair]
                        if not scaled or not spec.scale_group or any(
                            (s.scale_group or s.strategy_id) != spec.scale_group or s.premium == spec.premium
                            for s in siblings
                        ):
                            raise ValueError("esiste già una strategia per questa direzione/premium: usare Scala con un premium diverso")
                    pairs.add(pair)
                    self.db.execute("INSERT INTO strategies(id,spec) VALUES (?,?)",
                                    (spec.strategy_id, json.dumps(spec.payload(), sort_keys=True)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def replace_spec(self, spec: StrategySpec) -> None:
        with self.lock:
            row = self.get(spec.strategy_id)
            previous = StrategySpec.from_payload(row["spec"])
            if row["enabled"] or row["state"] in {"WRITING", "REVIEW_REQUIRED", "DELETED"}:
                raise ValueError("mettere in pausa e risolvere le anomalie prima di modificare")
            if (previous.base, previous.quote, previous.side, previous.cex) != (
                spec.base, spec.quote, spec.side, spec.cex
            ):
                raise ValueError("una modifica non può cambiare ticker, mapping, direzione o CEX")
            self.db.execute("UPDATE strategies SET spec=?,state='PAUSED',detail='',confirmations=0,evidence='null',preview='{}' WHERE id=?",
                            (json.dumps(spec.payload(), sort_keys=True), spec.strategy_id))

    def update(self, strategy_id: str, **values: Any) -> None:
        allowed = {"enabled", "state", "detail", "last_write", "evidence", "confirmations", "preview"}
        if not values or not values.keys() <= allowed:
            raise ValueError("campi stato strategia non validi")
        with self.lock:
            result = self.db.execute(
                f"UPDATE strategies SET {','.join(key+'=?' for key in values)} WHERE id=?",
                (*values.values(), strategy_id),
            )
            if result.rowcount != 1:
                raise KeyError(strategy_id)

    def cap_auto(self, strategy_id, maximum):
        from dataclasses import replace
        with self.lock:
            spec = StrategySpec.from_payload(self.get(strategy_id)["spec"])
            if spec.quantity_mode != "auto" or maximum <= 0:
                raise ValueError("riduzione riservata a quantità automatica positiva")
            capped = replace(spec, max_sold=min(maximum, spec.max_sold) if spec.max_sold else maximum)
            self.db.execute("UPDATE strategies SET spec=? WHERE id=?", (json.dumps(capped.payload()), strategy_id))

    def bind_order(self, strategy_id: str, order_uuid: str) -> None:
        with self.lock:
            previous = self.strategy_for_order(order_uuid)
            if previous is not None and previous != strategy_id:
                raise ValueError("ordine già assegnato a un'altra strategia")
            self.db.execute("INSERT OR IGNORE INTO strategy_orders VALUES (?,?)", (order_uuid, strategy_id))

    def archive(self, strategy_id: str) -> None:
        """Remove from management without erasing financial history or identity."""
        with self.lock:
            row = self.get(strategy_id)
            if row["state"] == "DELETED":
                return
            if row["enabled"] or row["state"] != "PAUSED":
                raise ValueError("mettere la strategia in pausa e risolvere le anomalie prima di eliminarla")
            pending = self.db.execute(
                "SELECT 1 FROM strategy_consumption WHERE strategy_id=? "
                "AND outcome NOT IN ('SUCCEEDED','REFUNDED') LIMIT 1",
                (strategy_id,),
            ).fetchone()
            if pending:
                raise ValueError("swap non risolti: impossibile eliminare la strategia")
            self.update(strategy_id, state="DELETED", detail="Eliminata dalla gestione; storico conservato")

    def archive_group(self, strategy_ids: tuple[str, ...]) -> None:
        """All requested configurations disappear together, or none do."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for sid in strategy_ids:
                    self.archive(sid)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def strategy_for_order(self, order_uuid: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT strategy_id FROM strategy_orders WHERE order_uuid=?", (order_uuid,)).fetchone()
        return row[0] if row else None

    def orders_for_strategy(self, strategy_id: str) -> tuple[str, ...]:
        """Bound UUIDs, newest binding first; never infer ownership from pair alone."""
        with self.lock:
            rows = self.db.execute(
                "SELECT order_uuid FROM strategy_orders WHERE strategy_id=? ORDER BY rowid DESC",
                (strategy_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def record_swap(self, *, strategy_id: str, swap_uuid: str, sold: Decimal, outcome: str) -> None:
        if not sold.is_finite() or sold <= 0:
            raise ValueError("importo effettivo dello swap non valido")
        with self.lock:
            previous = self.db.execute("SELECT * FROM strategy_consumption WHERE swap_uuid=?", (swap_uuid,)).fetchone()
            if previous and (previous["strategy_id"] != strategy_id or Decimal(previous["sold"]) != sold):
                raise ValueError("termini swap cambiati: verifica manuale richiesta")
            # A later KDF poll still reports FAILED after the refund was
            # verified.  Never downgrade the durable REFUNDED disposition.
            stored_outcome = "REFUNDED" if previous and previous["outcome"] == "REFUNDED" and outcome == "FAILED" else outcome
            self.db.execute(
                "INSERT INTO strategy_consumption VALUES(?,?,?,?,?) ON CONFLICT(swap_uuid) DO UPDATE SET outcome=excluded.outcome",
                (swap_uuid, strategy_id, str(sold), self.clock(), stored_outcome),
            )

    def mark_refunded(self, swap_uuid: str) -> None:
        """Release strategy budget only after the caller verifies the refund."""
        with self.lock:
            row = self.db.execute(
                "SELECT outcome FROM strategy_consumption WHERE swap_uuid=?",
                (swap_uuid,),
            ).fetchone()
            if row is None or row["outcome"] not in {"FAILED", "REFUNDED"}:
                raise ValueError("only a recorded failed swap can be marked refunded")
            self.db.execute(
                "UPDATE strategy_consumption SET outcome='REFUNDED' WHERE swap_uuid=?",
                (swap_uuid,),
            )

    def refunded_swap_uuids(self) -> tuple[str, ...]:
        """Return swaps whose maker refund was durably verified by KDF readback."""
        with self.lock:
            rows = self.db.execute(
                "SELECT swap_uuid FROM strategy_consumption WHERE outcome='REFUNDED' "
                "ORDER BY observed_at, swap_uuid"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def remaining(self, spec: StrategySpec) -> tuple[Decimal | None, Decimal | None]:
        with self.lock:
            rows = self.db.execute(
                "SELECT sold,observed_at,outcome FROM strategy_consumption WHERE strategy_id=?",
                (spec.strategy_id,),
            ).fetchall()
        # ACTIVE is reserved immediately; FAILED is held conservatively until
        # reviewed. A REFUNDED row has explicit KDF proof and restores budget.
        rows = [row for row in rows if row["outcome"] != "REFUNDED"]
        used = sum((Decimal(row["sold"]) for row in rows), Decimal(0))
        daily = sum((Decimal(row["sold"]) for row in rows if row["observed_at"] > self.clock() - 86400), Decimal(0))
        lifetime = spec.max_sold if spec.replenish else max(Decimal(0), spec.total_sold_budget - used)
        return lifetime, max(Decimal(0), spec.daily_sold_cap - daily) if spec.daily_sold_cap is not None else None
