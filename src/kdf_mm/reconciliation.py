from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .kdf import KdfRpcClient
from .models import DexSide
from .ownership import (
    OrderOwnershipStore,
    OwnedOrder,
    OwnedOrderStatus,
    OwnedSwap,
    OwnedSwapState,
)


class KdfReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KdfReconciliationStatus:
    ready: bool
    worker_running: bool
    last_success_ms: int | None
    consecutive_failures: int
    last_error: str | None
    kdf_maker_orders: int
    owned_open_orders: int
    active_owned_swaps: int
    terminal_swaps_to_acknowledge: int
    problem_orders: int


class KdfReconciler:
    """Polls durable KDF order/swap state and reconciles only owned UUIDs."""

    def __init__(
        self,
        *,
        kdf: KdfRpcClient,
        ownership: OrderOwnershipStore,
        interval_seconds: float = 2.0,
        recent_swap_limit: int = 100,
        missing_confirmations: int = 2,
        missing_grace_seconds: float = 30.0,
        clock_ms: Callable[[], int] | None = None,
        pool_resolver: Callable[[str, DexSide], str] | None = None,
        active_swap_callback: Callable[[OwnedOrder, str], object] | None = None,
        owned_swap_observer: Callable[
            [OwnedOrder, OwnedSwap, Mapping[str, Any]], object
        ]
        | None = None,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds < 1.0:
            raise ValueError("KDF reconciliation interval must be at least one second")
        if recent_swap_limit <= 0 or recent_swap_limit > 1000:
            raise ValueError("recent swap limit must be in [1, 1000]")
        if missing_confirmations < 2:
            raise ValueError("missing order confirmations must be at least two")
        if not math.isfinite(missing_grace_seconds) or missing_grace_seconds < 0:
            raise ValueError("missing order grace must be finite and nonnegative")
        self.kdf = kdf
        self.ownership = ownership
        self.interval_seconds = interval_seconds
        self.recent_swap_limit = recent_swap_limit
        self.missing_confirmations = missing_confirmations
        self.missing_grace_seconds = missing_grace_seconds
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.pool_resolver = pool_resolver
        self.active_swap_callback = active_swap_callback
        self.owned_swap_observer = owned_swap_observer
        self._ready = False
        self._last_success_ms: int | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._kdf_maker_orders = 0
        self._unowned_order_uuids: tuple[str, ...] = ()
        self._thread: threading.Thread | None = None
        self._pause_callback: Callable[[str], None] | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()

    def set_pause_callback(self, callback: Callable[[str], None]) -> None:
        self._pause_callback = callback

    def notify_event(self) -> None:
        """Wake durable polling immediately after a KDF SSE order/swap event."""
        self._wake.set()

    def start_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kdf-order-swap-reconciler",
                daemon=True,
            )
            self._thread.start()

    def stop_worker(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_seconds + 1.0))

    def reconcile_once(self) -> dict[str, Any]:
        with self._cycle_lock:
            try:
                maker_orders = self._maker_orders(self.kdf.my_orders())
                active_payload = self.kdf.active_swaps(include_status=True)
                recent_payload = self.kdf.recent_swaps(limit=self.recent_swap_limit)
                swap_statuses = self._swap_statuses(
                    maker_orders,
                    active_payload,
                    recent_payload,
                )
                self._reconcile_swaps(swap_statuses)
                self._reconcile_orders(maker_orders)
            except Exception as exc:
                self._record_failure(exc)
                raise KdfReconciliationError(str(exc)) from exc
            with self._lock:
                self._unowned_order_uuids = tuple(sorted(
                    uuid for uuid in maker_orders if self.ownership.get(uuid) is None
                ))
                self._ready = True
                self._last_success_ms = self.clock_ms()
                self._consecutive_failures = 0
                self._last_error = None
                self._kdf_maker_orders = len(maker_orders)
        return self.payload()

    def resume_block_reason(self) -> str | None:
        with self._lock:
            if self._unowned_order_uuids:
                return "unowned KDF orders require reconciliation: " + ", ".join(self._unowned_order_uuids)
            if self._ready:
                return None
            if self._last_error:
                return f"KDF reconciliation is not ready: {self._last_error}"
            return "KDF reconciliation has not completed yet"

    def block_reason(self, dex_side: DexSide) -> str | None:
        readiness = self.resume_block_reason()
        if readiness is not None:
            return readiness
        problems = self.ownership.problem_orders_for_side(dex_side)
        if problems:
            return f"order reconciliation required: {problems[0].order_uuid}"
        swap = self.ownership.blocking_swap_for_side(dex_side)
        if swap is None:
            return None
        if swap.state is OwnedSwapState.ACTIVE:
            return f"active KDF swap: {swap.swap_uuid}"
        return f"terminal KDF swap requires acknowledgement: {swap.swap_uuid}"

    def block_quote(self, market_id: str, dex_side: DexSide) -> str | None:
        readiness = self.resume_block_reason()
        if readiness is not None:
            return readiness
        pool = self._pool_for(market_id, dex_side)
        problems = self.ownership.problem_orders_for_pool(pool)
        if problems:
            return f"inventory pool {pool} requires order reconciliation: {problems[0].order_uuid}"
        swap = self.ownership.blocking_swap_for_pool(pool)
        if swap is None:
            return None
        if swap.state is OwnedSwapState.ACTIVE:
            return f"inventory pool {pool} has active KDF swap: {swap.swap_uuid}"
        return f"inventory pool {pool} has a terminal KDF swap to acknowledge: {swap.swap_uuid}"

    def acknowledge_side(self, dex_side: DexSide) -> dict[str, Any]:
        readiness = self.resume_block_reason()
        if readiness is not None:
            raise KdfReconciliationError(readiness)
        pending = self.ownership.problem_orders_for_side(dex_side)
        if any(
            order.status is OwnedOrderStatus.OPEN and order.missing_polls > 0
            for order in pending
        ):
            raise KdfReconciliationError(
                "the missing order is still awaiting confirmation"
            )
        self.ownership.acknowledge_swaps_for_side(dex_side)
        self.ownership.resolve_order_errors_for_side(dex_side)
        return self.payload()

    def acknowledge_quote(self, market_id: str, dex_side: DexSide) -> dict[str, Any]:
        readiness = self.resume_block_reason()
        if readiness is not None:
            raise KdfReconciliationError(readiness)
        pool = self._pool_for(market_id, dex_side)
        pending = self.ownership.problem_orders_for_pool(pool)
        if any(
            order.status is OwnedOrderStatus.OPEN and order.missing_polls > 0
            for order in pending
        ):
            raise KdfReconciliationError(
                "the missing order is still awaiting confirmation"
            )
        self.ownership.acknowledge_swaps_for_pool(pool)
        self.ownership.resolve_order_errors_for_pool(pool)
        return self.payload()

    def _pool_for(self, market_id: str, dex_side: DexSide) -> str:
        if self.pool_resolver is not None:
            return self.pool_resolver(market_id, dex_side)
        return "ARRR" if dex_side is DexSide.SELL_ARRR else market_id.removeprefix("ARRR-")

    def payload(self) -> dict[str, Any]:
        swaps = self.ownership.swaps(limit=self.recent_swap_limit)
        active_swaps, terminal_to_acknowledge = self.ownership.swap_state_counts()
        problems = tuple(
            problem
            for side in DexSide
            for problem in self.ownership.problem_orders_for_side(side)
        )
        with self._lock:
            status = KdfReconciliationStatus(
                ready=self._ready and not self._unowned_order_uuids,
                worker_running=self._thread is not None and self._thread.is_alive(),
                last_success_ms=self._last_success_ms,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                kdf_maker_orders=self._kdf_maker_orders,
                owned_open_orders=len(self.ownership.active()),
                active_owned_swaps=active_swaps,
                terminal_swaps_to_acknowledge=terminal_to_acknowledge,
                problem_orders=len(problems),
            )
        return {
            **asdict(status),
            "unowned_order_uuids": list(self._unowned_order_uuids),
            "swaps": [self._swap_payload(swap) for swap in swaps],
            "problem_order_uuids": [order.order_uuid for order in problems],
        }

    @staticmethod
    def _maker_orders(payload: Any) -> Mapping[str, Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            raise KdfReconciliationError("KDF my_orders returned a non-object")
        orders = payload.get("maker_orders")
        if not isinstance(orders, Mapping):
            raise KdfReconciliationError("KDF my_orders omitted maker_orders")
        if not all(isinstance(uuid, str) and isinstance(value, Mapping) for uuid, value in orders.items()):
            raise KdfReconciliationError("KDF maker_orders has an invalid shape")
        return orders

    def _swap_statuses(
        self,
        maker_orders: Mapping[str, Mapping[str, Any]],
        active_payload: Any,
        recent_payload: Any,
    ) -> tuple[tuple[str, Mapping[str, Any], bool], ...]:
        if not isinstance(active_payload, Mapping):
            raise KdfReconciliationError("KDF active_swaps returned a non-object")
        active_uuids = active_payload.get("uuids")
        statuses = active_payload.get("statuses")
        if not isinstance(active_uuids, list) or not all(
            isinstance(uuid, str) for uuid in active_uuids
        ):
            raise KdfReconciliationError("KDF active_swaps omitted valid UUIDs")
        if not isinstance(statuses, Mapping):
            raise KdfReconciliationError("KDF active_swaps omitted statuses")
        if not isinstance(recent_payload, Mapping) or not isinstance(
            recent_payload.get("swaps"), list
        ):
            raise KdfReconciliationError("KDF my_recent_swaps omitted swaps")

        combined: dict[str, tuple[Mapping[str, Any], bool]] = {}
        for item in recent_payload["swaps"]:
            status = self._normalize_swap(item)
            uuid = status.get("uuid") if status is not None else None
            if isinstance(uuid, str):
                # KDF's my_recent_swaps contains the event history but omits
                # is_finished/is_success. Treating their absence as False kept
                # a completed live swap ACTIVE indefinitely. Read the canonical
                # status for owned swaps that KDF no longer lists as active.
                order_uuid = status.get("my_order_uuid")
                if (
                    uuid not in active_uuids
                    and isinstance(order_uuid, str)
                    and self.ownership.get(order_uuid) is not None
                    and (
                        not isinstance(status.get("is_finished"), bool)
                        or (
                            status.get("is_finished") is True
                            and not isinstance(status.get("is_success"), bool)
                        )
                    )
                ):
                    item = self.kdf.swap_status(uuid)
                    canonical = self._normalize_swap(item)
                    if canonical is None or canonical.get("uuid") != uuid:
                        raise KdfReconciliationError(
                            f"KDF omitted canonical status for recent swap {uuid}"
                        )
                    if (
                        not isinstance(canonical.get("is_finished"), bool)
                        or (
                            canonical["is_finished"]
                            and not isinstance(canonical.get("is_success"), bool)
                        )
                    ):
                        raise KdfReconciliationError(
                            f"KDF canonical status omitted terminal flags for recent swap {uuid}"
                        )
                combined[uuid] = (item, False)
        for uuid in active_uuids:
            item = statuses.get(uuid)
            if not isinstance(item, Mapping):
                item = self.kdf.swap_status(uuid)
            if not isinstance(item, Mapping):
                raise KdfReconciliationError(
                    f"KDF omitted status for active swap {uuid}"
                )
            combined[uuid] = (item, True)
        for order_uuid, order in maker_orders.items():
            if self.ownership.get(order_uuid) is None:
                continue
            started = order.get("started_swaps", [])
            if not isinstance(started, list) or not all(
                isinstance(uuid, str) for uuid in started
            ):
                raise KdfReconciliationError(
                    f"KDF order {order_uuid} has invalid started_swaps"
                )
            for uuid in started:
                item = combined.get(uuid, (None, False))[0]
                if item is None:
                    item = self.kdf.swap_status(uuid)
                status = self._normalize_swap(item)
                if status is None:
                    raise KdfReconciliationError(
                        f"KDF omitted status for started swap {uuid}"
                    )
                if status.get("my_order_uuid") != order_uuid:
                    raise KdfReconciliationError(
                        f"started swap {uuid} does not reference owned order {order_uuid}"
                    )
                if uuid not in combined:
                    combined[uuid] = (
                        item,
                        status.get("is_finished") is not True,
                    )
        return tuple((uuid, item, active) for uuid, (item, active) in combined.items())

    def _reconcile_swaps(
        self,
        statuses: tuple[tuple[str, Mapping[str, Any], bool], ...],
    ) -> None:
        for response_uuid, raw, reported_active in statuses:
            status = self._normalize_swap(raw)
            if status is None:
                raise KdfReconciliationError(
                    f"KDF returned an invalid status for swap {response_uuid}"
                )
            swap_uuid = str(status.get("uuid", response_uuid))
            if swap_uuid != response_uuid:
                raise KdfReconciliationError("KDF swap UUID does not match its map key")
            order_uuid = status.get("my_order_uuid")
            if not isinstance(order_uuid, str):
                continue
            owned = self.ownership.get(order_uuid)
            if owned is None:
                continue
            role = status.get("type", raw.get("swap_type", ""))
            if not str(role).startswith("Maker"):
                raise KdfReconciliationError(
                    f"owned maker order {order_uuid} is linked to a non-maker swap"
                )
            quantity = self._base_quantity(status, owned)
            last_event = self._last_event(status)
            finished = status.get("is_finished") is True and not reported_active
            if not finished:
                state = OwnedSwapState.ACTIVE
            else:
                success = status.get("is_success")
                if not isinstance(success, bool):
                    raise KdfReconciliationError(
                        f"finished swap {swap_uuid} omitted is_success"
                    )
                state = (
                    OwnedSwapState.SUCCEEDED if success else OwnedSwapState.FAILED
                )
            recorded_swap = self.ownership.upsert_swap(
                swap_uuid=swap_uuid,
                order_uuid=order_uuid,
                dex_side=owned.dex_side,
                arrr_quantity=quantity,
                state=state,
                last_event=last_event,
                market_id=owned.market_id,
                inventory_pool=owned.inventory_pool,
            )
            if (
                state is OwnedSwapState.ACTIVE
                and self.active_swap_callback is not None
            ):
                # Idempotent on every poll: this also closes the crash window
                # between persisting the swap and cancelling its siblings.
                # OCO runs before hedge delivery so no downstream work can
                # overtake the shared-liquidity claim.
                self.active_swap_callback(owned, swap_uuid)
            if self.owned_swap_observer is not None:
                # The observer receives the complete KDF history, so it can
                # recover a missed milestone after a process restart.
                self.owned_swap_observer(owned, recorded_swap, status)

    def _reconcile_orders(self, maker_orders: Mapping[str, Mapping[str, Any]]) -> None:
        for owned in self.ownership.active():
            observed = maker_orders.get(owned.order_uuid)
            related_swaps = self.ownership.swaps_for_order(owned.order_uuid)
            if observed is not None:
                price, volume, maximum, minimum = self._order_terms(
                    owned, observed, allow_zero=True,
                )
                if volume > 0:
                    self.ownership.note_seen(
                        owned.order_uuid, kdf_price=price, kdf_volume=volume,
                        kdf_max_volume=maximum, kdf_min_volume=minimum,
                    )
                elif any(
                    swap.state is OwnedSwapState.ACTIVE or not swap.acknowledged
                    for swap in related_swaps
                ):
                    # KDF can leave a fully matched order in my_orders with
                    # zero available volume while its swap is being recorded.
                    self.ownership.mark(
                        owned.order_uuid, OwnedOrderStatus.COMPLETED,
                        only_if_open=True,
                    )
                else:
                    # Before KDF exposes the swap UUID, block only this
                    # inventory pool. An ordinary zero-volume transition must
                    # not fail global reconciliation and withdraw every pair.
                    self._note_unavailable_order(
                        owned, "KDF order has zero available volume; awaiting swap readback",
                    )
                continue
            if any(
                swap.state is OwnedSwapState.ACTIVE or not swap.acknowledged
                for swap in related_swaps
            ):
                self.ownership.mark(
                    owned.order_uuid,
                    OwnedOrderStatus.COMPLETED,
                    only_if_open=True,
                )
                continue
            historical = self._historical_order_status(owned)
            # Fulfilled without an attributed swap is not evidence that its
            # hedge was delivered. Keep the UUID blocked until swap discovery.
            if historical is OwnedOrderStatus.COMPLETED and not related_swaps:
                historical = None
            if (historical in {OwnedOrderStatus.COMPLETED, OwnedOrderStatus.CANCELLED}
                    and related_swaps and all(swap.acknowledged for swap in related_swaps)):
                # A late historical row must name exactly the swaps already
                # settled locally; otherwise this could release new exposure.
                if self.verified_settled_order(owned.order_uuid) is not historical:
                    historical = None
            if historical is not None:
                self.ownership.mark(owned.order_uuid, historical, only_if_open=True)
                continue
            self._note_unavailable_order(owned, "owned order is missing from KDF")

        # An ERROR is not necessarily permanent: KDF can finish writing order
        # history after the missing-order grace expires. Re-read only UUIDs
        # linked to a known swap, and never clear the hedge gate by assumption.
        for owned in self.ownership.error_orders():
            related_swaps = self.ownership.swaps_for_order(owned.order_uuid)
            if not related_swaps:
                continue
            observed = maker_orders.get(owned.order_uuid)
            if observed is not None:
                price, volume, maximum, minimum = self._order_terms(
                    owned, observed, allow_zero=True,
                )
                if volume > 0:
                    self.ownership.restore_seen(
                        owned.order_uuid, kdf_price=price, kdf_volume=volume,
                        kdf_max_volume=maximum, kdf_min_volume=minimum,
                    )
                    continue
            if any(swap.state is OwnedSwapState.ACTIVE for swap in related_swaps):
                # KDF may expose the swap only after two zero-volume polls
                # marked an older order ERROR. The linked swap, not an
                # uncorroborated order snapshot, now owns the pool gate.
                self.ownership.mark_late_matched(owned.order_uuid)
                continue
            if observed is not None:
                continue
            terminal = self.verified_settled_order(owned.order_uuid)
            if terminal is not None:
                self.ownership.mark(
                    owned.order_uuid, terminal,
                    error="Ordine e swap conclusi verificati nello storico KDF",
                    source="post_swap_readback",
                )

        # A partial fill can briefly remove a maker UUID from my_orders and
        # return it with reduced available_amount after the swap finishes.
        # Keep managing that same UUID instead of publishing a replacement.
        for owned in self.ownership.completed_swap_orders():
            observed = maker_orders.get(owned.order_uuid)
            if observed is not None:
                price, volume, maximum, minimum = self._order_terms(
                    owned, observed, allow_zero=True,
                )
                if volume > 0:
                    self.ownership.restore_seen(
                        owned.order_uuid, kdf_price=price, kdf_volume=volume,
                        kdf_max_volume=maximum, kdf_min_volume=minimum,
                    )

    def _note_unavailable_order(self, owned: OwnedOrder, reason: str) -> None:
        missing = (self.ownership.note_missing(owned.order_uuid)
                   if reason == "owned order is missing from KDF"
                   else self.ownership.note_missing(owned.order_uuid, reason=reason))
        if (
            missing.status is OwnedOrderStatus.OPEN
            and missing.missing_polls >= self.missing_confirmations
            and (missing_age := self.ownership.missing_age_seconds(owned.order_uuid))
            is not None
            and missing_age >= self.missing_grace_seconds
        ):
            self.ownership.mark(
                owned.order_uuid, OwnedOrderStatus.ERROR,
                error=("owned order disappeared from KDF without a related swap"
                       if reason == "owned order is missing from KDF"
                       else reason + " without a related swap"),
                only_if_open=True,
            )

    def verified_settled_order(self, order_uuid: str) -> OwnedOrderStatus | None:
        """Return a terminal order state only with matching KDF and hedge proof.

        This is read-only. Active or not-yet-acknowledged swaps must not release
        their inventory pool or close an uncertain update intent.  A failed swap
        is eligible only after the settlement layer has acknowledged its verified
        maker refund; that acknowledgement is the durable refund+hedge proof.
        """
        owned = self.ownership.get(order_uuid)
        swaps = self.ownership.swaps_for_order(order_uuid)
        if owned is None or not swaps or any(
            swap.state is OwnedSwapState.ACTIVE or not swap.acknowledged
            for swap in swaps
        ):
            return None
        try:
            response = self.kdf.order_status(order_uuid)
        except Exception as exc:
            if "not found" in str(exc).lower() or "http 404" in str(exc).lower():
                return None
            raise
        if not isinstance(response, Mapping):
            raise KdfReconciliationError("KDF order_status returned a non-object")
        order = response.get("order", response)
        if not isinstance(order, Mapping):
            raise KdfReconciliationError("KDF order_status omitted order data")
        if (order.get("uuid") != order_uuid or order.get("base") != owned.kdf_base
                or order.get("rel") != owned.kdf_rel):
            raise KdfReconciliationError("KDF historical order identity or pair changed")
        started = order.get("started_swaps")
        matches = order.get("matches")
        if (not isinstance(started, list) or not all(isinstance(uid, str) for uid in started)
                or len(started) != len(set(started)) or not isinstance(matches, Mapping)
                or not matches):
            return None
        if set(started) != {swap.swap_uuid for swap in swaps} or set(matches) != set(started):
            raise KdfReconciliationError("KDF historical order has unrecognised swap UUIDs")
        reason = order.get("cancellation_reason", response.get("cancellation_reason"))
        if isinstance(reason, Mapping):
            reason = reason.get("type") or reason.get("reason")
        normalized = str(reason or "").replace("_", "").replace(" ", "").lower()
        if normalized == "fulfilled":
            return OwnedOrderStatus.COMPLETED
        if normalized == "cancelled":
            return OwnedOrderStatus.CANCELLED
        return None

    def _historical_order_status(
        self, owned: OwnedOrder
    ) -> OwnedOrderStatus | None:
        order_status = getattr(self.kdf, "order_status", None)
        if order_status is None:
            return None
        try:
            response = order_status(owned.order_uuid)
        except Exception as exc:
            # "not found" is expected while KDF is still committing history.
            if "not found" in str(exc).lower():
                return None
            raise
        if not isinstance(response, Mapping):
            raise KdfReconciliationError("KDF order_status returned a non-object")
        order = response.get("order", response)
        if not isinstance(order, Mapping):
            raise KdfReconciliationError("KDF order_status omitted order data")
        reason = order.get("cancellation_reason", response.get("cancellation_reason"))
        if isinstance(reason, Mapping):
            reason = reason.get("type") or reason.get("reason")
        normalized = str(reason or "").replace("_", "").replace(" ", "").lower()
        if "insufficientbalance" in normalized:
            return OwnedOrderStatus.INSUFFICIENT_BALANCE
        if "fulfilled" in normalized:
            return OwnedOrderStatus.COMPLETED
        if "cancel" in normalized:
            return OwnedOrderStatus.CANCELLED
        return None

    @staticmethod
    def _order_terms(
        owned: OwnedOrder, observed: Mapping[str, Any], *, allow_zero: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if observed.get("uuid") not in (None, owned.order_uuid):
            raise KdfReconciliationError("KDF maker order UUID is inconsistent")
        if observed.get("base") != owned.kdf_base or observed.get("rel") != owned.kdf_rel:
            raise KdfReconciliationError(
                f"KDF pair changed for owned order {owned.order_uuid}"
            )
        try:
            price = Decimal(str(observed["price"]))
            volume = Decimal(str(observed["available_amount"]))
            maximum = Decimal(str(observed.get("max_base_vol", owned.advertised_volume)))
            minimum = Decimal(str(observed.get("min_base_vol", owned.kdf_min_volume)))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise KdfReconciliationError(
                f"KDF order {owned.order_uuid} has invalid price or volume"
            ) from exc
        if (
            not all(value.is_finite() for value in (price, volume, maximum, minimum))
            or price <= 0 or volume < 0 or maximum <= 0 or minimum < 0
            or volume > maximum or minimum > maximum
            or (volume == 0 and not allow_zero)
        ):
            raise KdfReconciliationError(f"KDF order {owned.order_uuid} has invalid price or volume")
        return price, volume, maximum, minimum

    @staticmethod
    def _normalize_swap(raw: Any) -> Mapping[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        nested = raw.get("swap_data")
        return nested if isinstance(nested, Mapping) else raw

    @staticmethod
    def _last_event(status: Mapping[str, Any]) -> str:
        events = status.get("events")
        if not isinstance(events, list) or not events:
            return "NoEvents"
        latest = events[-1]
        event = latest.get("event") if isinstance(latest, Mapping) else None
        value = event.get("type") if isinstance(event, Mapping) else None
        return str(value) if value else "Unknown"

    @staticmethod
    def _base_quantity(status: Mapping[str, Any], owned: OwnedOrder) -> Decimal:
        maker_coin = status.get("maker_coin")
        taker_coin = status.get("taker_coin")
        maker_amount = status.get("maker_amount")
        taker_amount = status.get("taker_amount")
        if maker_coin is None or taker_coin is None:
            events = status.get("events")
            if isinstance(events, list):
                for item in events:
                    event = item.get("event") if isinstance(item, Mapping) else None
                    if not isinstance(event, Mapping) or event.get("type") != "Started":
                        continue
                    data = event.get("data")
                    if isinstance(data, Mapping):
                        maker_coin = data.get("maker_coin")
                        taker_coin = data.get("taker_coin")
                        maker_amount = data.get("maker_amount")
                        taker_amount = data.get("taker_amount")
                    break
        base_ticker = (
            owned.kdf_base
            if owned.dex_side is DexSide.SELL_ARRR
            else owned.kdf_rel
        )
        if owned.dex_side is DexSide.SELL_ARRR and maker_coin == base_ticker:
            raw_quantity = maker_amount
        elif owned.dex_side is DexSide.BUY_ARRR and taker_coin == base_ticker:
            raw_quantity = taker_amount
        else:
            raise KdfReconciliationError(
                f"swap coins do not match owned order side {owned.dex_side.value}"
            )
        try:
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise KdfReconciliationError("swap base quantity is invalid") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise KdfReconciliationError("swap base quantity must be positive")
        return quantity

    @staticmethod
    def _swap_payload(swap: OwnedSwap) -> dict[str, Any]:
        return {
            "swap_uuid": swap.swap_uuid,
            "order_uuid": swap.order_uuid,
            "dex_side": swap.dex_side.value,
            "arrr_quantity": str(swap.arrr_quantity),
            "base_quantity": str(swap.arrr_quantity),
            "state": swap.state.value,
            "last_event": swap.last_event,
            "acknowledged": swap.acknowledged,
            "market_id": swap.market_id,
            "inventory_pool": swap.inventory_pool,
        }

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            self._ready = False
            self._consecutive_failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            callback = self._pause_callback
        if callback is not None:
            try:
                callback("kdf_reconciliation_failed")
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except KdfReconciliationError:
                pass
            self._wake.wait(self.interval_seconds)
            self._wake.clear()
