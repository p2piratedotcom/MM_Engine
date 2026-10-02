from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import DexSide, QuotePlan
from .vps_controller import VpsController, quote_plan_payload


AUTO_RESUME_PAUSE_REASONS = frozenset(
    {"coverage_unavailable", "market_data_stale"}
)
REPRICING_STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class QuoteTarget:
    dex_side: DexSide
    requested_quantity: Decimal
    kdf_available_quantity: Decimal
    premium: Decimal
    market_id: str = ""

    def __post_init__(self) -> None:
        if (
            not self.requested_quantity.is_finite()
            or self.requested_quantity <= 0
            or not self.kdf_available_quantity.is_finite()
            or self.kdf_available_quantity <= 0
        ):
            raise ValueError("quote target quantities must be positive and finite")
        if not self.premium.is_finite():
            raise ValueError("quote target premium must be finite")

    def payload(self) -> dict[str, str]:
        return {
            "market_id": self.market_id,
            "dex_side": self.dex_side.value,
            "requested_quantity": str(self.requested_quantity),
            "kdf_available_quantity": str(self.kdf_available_quantity),
            "premium": str(self.premium),
        }


class RepricingEngine:
    """Maintains at most one owned KDF quote per market and base-asset side."""

    def __init__(
        self,
        *,
        controller: VpsController,
        poll_interval_seconds: float = 3.0,
        min_price_change: Decimal = Decimal("0.0025"),
        min_update_interval_seconds: float = 15.0,
        monotonic: Callable[[], float] | None = None,
        resume_guard: Callable[[], str | None] | None = None,
        side_blocker: Callable[[DexSide], str | None] | None = None,
        quote_blocker: Callable[[str, DexSide], str | None] | None = None,
        auto_resume_confirmations: int = 3,
        state_path: str | Path | None = None,
    ) -> None:
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0.5:
            raise ValueError("repricing poll interval must be at least 0.5 seconds")
        if (
            not min_price_change.is_finite()
            or min_price_change < 0
            or min_price_change >= 1
        ):
            raise ValueError("minimum price change must be in [0, 1)")
        if (
            not math.isfinite(min_update_interval_seconds)
            or min_update_interval_seconds <= 0
        ):
            raise ValueError("minimum update interval must be positive")
        if auto_resume_confirmations < 1:
            raise ValueError("auto-resume confirmations must be positive")
        self.controller = controller
        self.poll_interval_seconds = poll_interval_seconds
        self.min_price_change = min_price_change
        self.min_update_interval_seconds = min_update_interval_seconds
        self.monotonic = monotonic or time.monotonic
        self.resume_guard = resume_guard
        self.side_blocker = side_blocker
        self.quote_blocker = quote_blocker
        self.auto_resume_confirmations = auto_resume_confirmations
        self.state_path = Path(state_path).resolve() if state_path else None
        self._targets: dict[str, QuoteTarget] = {}
        self._last_action: dict[str, str] = {}
        self._last_error: dict[str, str] = {}
        self._last_plan: dict[str, QuotePlan] = {}
        self._last_write_attempt: dict[str, float] = {}
        self._last_write: dict[str, float] = {}
        self._paused = True
        self._pause_reason = "manual"
        self._resume_block_reason: str | None = None
        self._auto_resume_observations = 0
        self._auto_resume_block_reason: str | None = None
        self._auto_resume_last_evidence: object | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._restore_state()

    def start_worker(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kdf-repricing-engine",
                daemon=True,
            )
            self._thread.start()

    def stop_worker(self) -> None:
        # Keep the last operator-requested state on disk.  A service restart
        # must restore a running strategy, while an explicit manual pause must
        # remain sticky.
        self.pause(reason="service_stopped", persist=False)
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.poll_interval_seconds + 1.0))

    def configure_target(
        self,
        *,
        dex_side: DexSide,
        requested_quantity: Decimal,
        kdf_available_quantity: Decimal,
        premium: Decimal,
        market_id: str | None = None,
    ) -> dict[str, Any]:
        selected_market = market_id
        if selected_market is None:
            selected_market = str(getattr(self.controller, "default_market_id", ""))
        target = QuoteTarget(
            dex_side=dex_side,
            requested_quantity=requested_quantity,
            kdf_available_quantity=kdf_available_quantity,
            premium=premium,
            market_id=selected_market,
        )
        key = self._target_key(target.market_id, target.dex_side)
        with self._operation_lock:
            with self._state_lock:
                self._targets[key] = target
                self._last_action[key] = "CONFIGURED"
                self._last_error.pop(key, None)
                self._last_plan.pop(key, None)
                self._resume_block_reason = None
                self._persist_state_locked()
        self._wake.set()
        return self.payload()

    def configure_policy(
        self,
        *,
        min_price_change: Decimal,
        min_update_interval_seconds: float,
    ) -> dict[str, Any]:
        if (
            not min_price_change.is_finite()
            or min_price_change < 0
            or min_price_change >= 1
        ):
            raise ValueError("minimum price change must be in [0, 1)")
        if (
            not math.isfinite(min_update_interval_seconds)
            or min_update_interval_seconds <= 0
        ):
            raise ValueError("minimum update interval must be positive")
        with self._state_lock:
            self.min_price_change = min_price_change
            self.min_update_interval_seconds = min_update_interval_seconds
            self._persist_state_locked()
        self._wake.set()
        return self.payload()

    def remove_target(
        self, dex_side: DexSide, *, market_id: str | None = None
    ) -> dict[str, Any]:
        selected_market = market_id
        if selected_market is None:
            selected_market = str(getattr(self.controller, "default_market_id", ""))
        key = self._target_key(selected_market, dex_side)
        with self._operation_lock:
            with self._state_lock:
                if key not in self._targets:
                    raise KeyError(key)
                del self._targets[key]
                self._last_action.pop(key, None)
                self._last_error.pop(key, None)
                self._last_plan.pop(key, None)
                self._resume_block_reason = None
                self._persist_state_locked()
        return self.payload()

    def resume(self) -> dict[str, Any]:
        try:
            with self._operation_lock:
                with self._state_lock:
                    target_quotes = tuple(self._targets.values())
                if not target_quotes:
                    raise ValueError(
                        "configure at least one quote side before resuming"
                    )
                # Never resume against missing or stale prices.
                for target in target_quotes:
                    self._assert_target_market_fresh(target)
                if self.controller.kdf.orders_enabled:
                    if self.resume_guard is not None:
                        blocked = self.resume_guard()
                        if blocked is not None:
                            raise ValueError(blocked)
                    if (
                        self.side_blocker is not None
                        or self.quote_blocker is not None
                    ):
                        for target in target_quotes:
                            blocked = self._block_reason(target)
                            if blocked is not None:
                                raise ValueError(blocked)
                with self._state_lock:
                    self._paused = False
                    self._pause_reason = None
                    self._resume_block_reason = None
                    self._reset_auto_resume_locked()
                    self._persist_state_locked()
        except Exception as exc:
            with self._state_lock:
                self._resume_block_reason = str(exc) or type(exc).__name__
            raise
        self._wake.set()
        return self.payload()

    def pause(
        self, *, reason: str = "manual", persist: bool = True
    ) -> dict[str, Any]:
        if not reason:
            raise ValueError("pause reason is required")
        # If a write is already in flight, wait for it to finish before reporting PAUSED.
        with self._operation_lock:
            with self._state_lock:
                self._paused = True
                self._pause_reason = reason
                self._resume_block_reason = None
                self._reset_auto_resume_locked()
                if persist:
                    self._persist_state_locked()
        return self.payload()

    def observe_auto_resume(
        self,
        *,
        pause_reason: str,
        healthy: bool,
    ) -> dict[str, Any]:
        """Resume an automatic safety pause after stable, full preflight checks.

        A manual pause or a different failure reason is never overridden.  The
        caller supplies consecutive health observations; target market data,
        reconciliation and aggregate target coverage are revalidated here.
        """
        should_wake = False
        with self._operation_lock:
            with self._state_lock:
                eligible = self._paused and self._pause_reason == pause_reason
                # The safety monitor observes more than one recoverable cause.
                # An observation for a different cause must not erase the
                # confirmation streak belonging to the active pause.
                if not eligible:
                    return self.payload()
                if not healthy:
                    self._reset_auto_resume_locked()
                    return self.payload()
            try:
                plans = self._validate_auto_resume_locked()
                evidence = self._auto_resume_evidence(pause_reason, plans)
            except Exception as exc:
                with self._state_lock:
                    self._auto_resume_observations = 0
                    self._auto_resume_last_evidence = None
                    self._auto_resume_block_reason = str(exc) or type(exc).__name__
                return self.payload()
            with self._state_lock:
                if not self._paused or self._pause_reason != pause_reason:
                    self._reset_auto_resume_locked()
                    return self.payload()
                if (
                    evidence is not None
                    and evidence == self._auto_resume_last_evidence
                ):
                    return self.payload()
                self._auto_resume_last_evidence = evidence
                self._auto_resume_observations += 1
                self._auto_resume_block_reason = None
                if self._auto_resume_observations >= self.auto_resume_confirmations:
                    self._paused = False
                    self._pause_reason = None
                    self._resume_block_reason = None
                    self._reset_auto_resume_locked()
                    for plan in plans:
                        key = self._target_key(plan.market_id, plan.dex_side)
                        self._last_action[key] = "AUTO_RESUMED"
                    self._persist_state_locked()
                    should_wake = True
        if should_wake:
            self._wake.set()
        return self.payload()

    def run_once(self) -> dict[str, Any]:
        with self._cycle_lock:
            with self._state_lock:
                if self._paused:
                    return self.payload()
                targets = tuple(self._targets.values())
            for target in targets:
                self._reprice(target)
        return self.payload()

    def payload(self) -> dict[str, Any]:
        now = self.monotonic()
        with self._state_lock:
            thread_running = self._thread is not None and self._thread.is_alive()
            sides: dict[str, Any] = {}
            quotes: dict[str, Any] = {}
            for key, target in self._targets.items():
                last_attempt = self._last_write_attempt.get(key)
                next_write = (
                    0.0
                    if last_attempt is None
                    else max(
                        0.0,
                        self.min_update_interval_seconds - (now - last_attempt),
                    )
                )
                quotes[key] = {
                    **target.payload(),
                    "last_action": self._last_action.get(key),
                    "last_error": self._last_error.get(key),
                    "last_plan": (
                        quote_plan_payload(self._last_plan[key])
                        if key in self._last_plan
                        else None
                    ),
                    "seconds_until_next_write": round(next_write, 3),
                    "has_written": key in self._last_write,
                    "reconciliation_block": self._block_reason(target),
                }
            # `sides` remains as a compatibility alias. Multi-market clients use
            # `quotes`, whose keys are MARKET_ID:SIDE.
            sides = dict(quotes)
            return {
                "state": "PAUSED" if self._paused else "RUNNING",
                "pause_reason": self._pause_reason,
                "resume_block_reason": self._resume_block_reason,
                "worker_running": thread_running,
                "orders_enabled": bool(self.controller.kdf.orders_enabled),
                "poll_interval_seconds": self.poll_interval_seconds,
                "min_price_change": str(self.min_price_change),
                "min_update_interval_seconds": self.min_update_interval_seconds,
                "max_active_orders_per_side": 1,
                "max_active_orders_per_market_side": 1,
                "reconciliation_guard": (
                    self.resume_guard() if self.resume_guard is not None else None
                ),
                "auto_resume": {
                    "eligible": self._paused
                    and self._pause_reason in AUTO_RESUME_PAUSE_REASONS,
                    "pause_reason": (
                        self._pause_reason
                        if self._paused
                        and self._pause_reason in AUTO_RESUME_PAUSE_REASONS
                        else None
                    ),
                    "healthy_observations": self._auto_resume_observations,
                    "required_healthy_observations": self.auto_resume_confirmations,
                    "block_reason": self._auto_resume_block_reason,
                },
                "sides": sides,
                "quotes": quotes,
            }

    def _restore_state(self) -> None:
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read persisted repricing state") from exc
        if not isinstance(loaded, Mapping):
            raise ValueError("persisted repricing state must be an object")
        if loaded.get("schema_version") != REPRICING_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported persisted repricing state schema")
        state = str(loaded.get("state", ""))
        if state not in {"RUNNING", "PAUSED"}:
            raise ValueError("persisted repricing state is invalid")
        pause_reason = loaded.get("pause_reason")
        if state == "PAUSED" and not isinstance(pause_reason, str):
            raise ValueError("persisted repricing pause reason is invalid")
        min_price_change = Decimal(str(loaded["min_price_change"]))
        min_update_interval_seconds = float(
            loaded["min_update_interval_seconds"]
        )
        if (
            not min_price_change.is_finite()
            or min_price_change < 0
            or min_price_change >= 1
        ):
            raise ValueError("persisted minimum price change is invalid")
        if (
            not math.isfinite(min_update_interval_seconds)
            or min_update_interval_seconds <= 0
        ):
            raise ValueError("persisted minimum update interval is invalid")
        rows = loaded.get("targets")
        if not isinstance(rows, list):
            raise ValueError("persisted repricing targets are invalid")
        targets: dict[str, QuoteTarget] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("persisted repricing target is invalid")
            target = QuoteTarget(
                dex_side=DexSide(str(row["dex_side"])),
                requested_quantity=Decimal(str(row["requested_quantity"])),
                kdf_available_quantity=Decimal(
                    str(row["kdf_available_quantity"])
                ),
                premium=Decimal(str(row["premium"])),
                market_id=str(row["market_id"]),
            )
            key = self._target_key(target.market_id, target.dex_side)
            if key in targets:
                raise ValueError("persisted repricing target is duplicated")
            targets[key] = target
        if state == "RUNNING" and not targets:
            raise ValueError("running persisted repricing state has no targets")
        self.min_price_change = min_price_change
        self.min_update_interval_seconds = min_update_interval_seconds
        self._targets = targets
        self._paused = state == "PAUSED"
        self._pause_reason = str(pause_reason) if self._paused else None
        for key in targets:
            self._last_action[key] = "RESTORED"

    def _persist_state_locked(self) -> None:
        path = self.state_path
        if path is None:
            return
        payload = {
            "schema_version": REPRICING_STATE_SCHEMA_VERSION,
            "state": "PAUSED" if self._paused else "RUNNING",
            "pause_reason": self._pause_reason,
            "min_price_change": str(self.min_price_change),
            "min_update_interval_seconds": self.min_update_interval_seconds,
            "targets": [
                target.payload()
                for _, target in sorted(self._targets.items())
            ],
        }
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _reprice(self, target: QuoteTarget) -> None:
        side = target.dex_side
        key = self._target_key(target.market_id, side)
        try:
            if self.controller.kdf.orders_enabled:
                blocked = self._block_reason(target)
                if blocked is not None:
                    self._record(
                        key,
                        "BLOCKED_RECONCILIATION",
                        None,
                        blocked,
                    )
                    return
            plan = self.controller.preview_quote(
                dex_side=side,
                requested_quantity=target.requested_quantity,
                kdf_available_quantity=target.kdf_available_quantity,
                premium=target.premium,
                **({"market_id": target.market_id} if target.market_id else {}),
            )
            if target.market_id and hasattr(
                self.controller, "active_orders_for_market_side"
            ):
                active = self.controller.active_orders_for_market_side(
                    target.market_id, side
                )
            else:
                active = self.controller.active_orders_for_side(side)
            if len(active) > 1:
                message = f"more than one owned order is open for {side.value}"
                self._record(key, "ORDER_LIMIT_VIOLATION", plan, message)
                self.pause(reason="order_limit_violation")
                return
            if not active:
                self._maybe_write(target, plan, action="PUBLISH")
                return

            owned = active[0]
            old_human_price = (
                owned.kdf_price
                if side is DexSide.SELL_ARRR
                else Decimal("1") / owned.kdf_price
            )
            price_change = abs(plan.human_price_quote_per_base - old_human_price) / old_human_price
            old_base_quantity = (
                owned.kdf_volume
                if side is DexSide.SELL_ARRR
                else owned.kdf_volume * owned.kdf_price
            )
            quantity_changed = plan.base_quantity != old_base_quantity
            if price_change < self.min_price_change and not quantity_changed:
                self._record(key, "BELOW_THRESHOLD", plan)
                return
            self._maybe_write(
                target,
                plan,
                action="UPDATE",
                order_uuid=owned.order_uuid,
            )
        except Exception as exc:
            self._record(key, "ERROR", None, f"{type(exc).__name__}: {exc}")

    def _maybe_write(
        self,
        target: QuoteTarget,
        plan: QuotePlan,
        *,
        action: str,
        order_uuid: str | None = None,
    ) -> None:
        side = target.dex_side
        key = self._target_key(target.market_id, side)
        now = self.monotonic()
        with self._state_lock:
            last_attempt = self._last_write_attempt.get(key)
            if (
                last_attempt is not None
                and now - last_attempt < self.min_update_interval_seconds
            ):
                self._record(key, "RATE_LIMITED", plan)
                return
            if not self.controller.kdf.orders_enabled:
                self._record(key, f"SIMULATION_{action}", plan)
                return

        with self._operation_lock:
            with self._state_lock:
                if self._paused or self._targets.get(key) != target:
                    return
                self._last_write_attempt[key] = now
            try:
                if action == "PUBLISH":
                    self.controller.publish_quote(plan)
                elif action == "UPDATE" and order_uuid is not None:
                    self.controller.update_owned_quote(order_uuid, plan)
                else:
                    raise RuntimeError("invalid repricing action")
            except Exception as exc:
                self._record(key, "ERROR", plan, f"{type(exc).__name__}: {exc}")
                return
            with self._state_lock:
                self._last_write[key] = now
            self._record(
                key,
                "PUBLISHED" if action == "PUBLISH" else "UPDATED",
                plan,
            )

    def _record(
        self,
        key: str,
        action: str,
        plan: QuotePlan | None,
        error: str | None = None,
    ) -> None:
        with self._state_lock:
            self._last_action[key] = action
            if plan is not None:
                self._last_plan[key] = plan
            if error is None:
                self._last_error.pop(key, None)
            else:
                self._last_error[key] = error

    @staticmethod
    def _target_key(market_id: str, dex_side: DexSide) -> str:
        return f"{market_id}:{dex_side.value}" if market_id else dex_side.value

    def _block_reason(self, target: QuoteTarget) -> str | None:
        if self.quote_blocker is not None and target.market_id:
            return self.quote_blocker(target.market_id, target.dex_side)
        if self.side_blocker is not None:
            return self.side_blocker(target.dex_side)
        return None

    def _assert_target_market_fresh(self, target: QuoteTarget) -> None:
        if target.market_id and hasattr(self.controller, "market_status"):
            self.controller.market_status(target.market_id)
        else:
            self.controller.market_data.current()

    def _validate_auto_resume_locked(self) -> tuple[QuotePlan, ...]:
        with self._state_lock:
            targets = tuple(self._targets.values())
        if not targets:
            raise ValueError("configure at least one quote side before resuming")
        if self.controller.kdf.orders_enabled:
            if self.resume_guard is not None:
                blocked = self.resume_guard()
                if blocked is not None:
                    raise ValueError(blocked)
            for target in targets:
                blocked = self._block_reason(target)
                if blocked is not None:
                    raise ValueError(blocked)
        plans: list[QuotePlan] = []
        for target in targets:
            self._assert_target_market_fresh(target)
            plans.append(
                self.controller.preview_quote(
                    dex_side=target.dex_side,
                    requested_quantity=target.requested_quantity,
                    kdf_available_quantity=target.kdf_available_quantity,
                    premium=target.premium,
                    **(
                        {"market_id": target.market_id}
                        if target.market_id
                        else {}
                    ),
                )
            )
        coverage_check = getattr(
            self.controller, "assert_repricing_targets_coverage", None
        )
        if self.controller.kdf.orders_enabled and coverage_check is not None:
            coverage_check(tuple(plans))
        return tuple(plans)

    def _reset_auto_resume_locked(self) -> None:
        self._auto_resume_observations = 0
        self._auto_resume_block_reason = None
        self._auto_resume_last_evidence = None

    def _auto_resume_evidence(
        self,
        pause_reason: str,
        plans: tuple[QuotePlan, ...],
    ) -> object | None:
        """Require distinct fresh snapshots when recovering a stale feed."""
        if pause_reason != "market_data_stale" or not hasattr(
            self.controller, "market_status"
        ):
            return None
        market_ids = tuple(
            sorted({plan.market_id for plan in plans if plan.market_id})
        )
        if not market_ids:
            return None
        evidence: list[tuple[str, tuple[int, ...]]] = []
        for market_id in market_ids:
            status = self.controller.market_status(market_id)
            if not isinstance(status, Mapping):
                raise ValueError("fresh MEXC market status is invalid")
            raw_sequences = status.get("sequences")
            if isinstance(raw_sequences, (list, tuple)) and raw_sequences:
                sequences = tuple(int(value) for value in raw_sequences)
            elif status.get("sequence") is not None:
                sequences = (int(status["sequence"]),)
            else:
                raise ValueError("fresh MEXC snapshot has no sequence")
            evidence.append((market_id, sequences))
        return tuple(evidence)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Per-side errors are normally captured by _reprice. Keep the worker alive
                # if an unexpected orchestration error escapes a cycle.
                pass
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()
