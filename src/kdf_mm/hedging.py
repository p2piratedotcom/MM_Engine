from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol

from .journal import HedgeAttempt, HedgeJournal, HedgeRecord, HedgeState, JournalConflict
from .mexc import MexcError
from .models import DexSide, HedgeSide


OPEN_ORDER_STATUSES = {"NEW", "PARTIALLY_FILLED"}
TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "PARTIALLY_CANCELED"}


class MexcHedgeApi(Protocol):
    def place_limit_order(
        self,
        *,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> Mapping[str, Any]: ...

    def query_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]: ...

    def cancel_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]: ...


class HedgeExecutor:
    """Esegue una copertura senza ritentare alla cieca un ordine incerto."""

    def __init__(
        self,
        *,
        journal: HedgeJournal,
        mexc: MexcHedgeApi,
        symbol: str = "ARRRUSDT",
    ) -> None:
        self.journal = journal
        self.mexc = mexc
        self.symbol = symbol

    def prepare_attempt(
        self,
        *,
        swap_uuid: str,
        dex_side: DexSide,
        target_quantity: Decimal,
        sequence: int,
        requested_quantity: Decimal,
        limit_price: Decimal,
    ) -> HedgeAttempt:
        hedge = self.journal.reserve(
            swap_uuid=swap_uuid,
            dex_side=dex_side,
            target_quantity=target_quantity,
        )
        remaining = hedge.target_quantity - self.journal.total_executed(swap_uuid)
        if requested_quantity > remaining:
            raise JournalConflict("attempt quantity exceeds the unhedged remainder")
        attempt = self.journal.create_attempt(
            swap_uuid=swap_uuid,
            sequence=sequence,
            requested_quantity=requested_quantity,
            limit_price=limit_price,
        )
        if hedge.state in {HedgeState.RESERVED, HedgeState.PARTIAL}:
            self.journal.transition(swap_uuid, HedgeState.HEDGE_READY)
        elif hedge.state is not HedgeState.HEDGE_READY:
            raise JournalConflict(f"cannot prepare an attempt while hedge is {hedge.state}")
        return attempt

    def execute_or_recover(self, *, swap_uuid: str, sequence: int) -> HedgeRecord:
        hedge = self._required_hedge(swap_uuid)
        attempt = self._required_attempt(swap_uuid, sequence)

        if hedge.state in {HedgeState.HEDGE_READY, HedgeState.TEST_VALIDATED}:
            allowed_status = (
                "PLANNED"
                if hedge.state is HedgeState.HEDGE_READY
                else "TEST_VALIDATED"
            )
            if attempt.status != allowed_status:
                raise JournalConflict(
                    "hedge state and attempt status are inconsistent"
                )
            self.journal.transition(swap_uuid, HedgeState.SUBMITTING)
            self.journal.update_attempt(
                swap_uuid=swap_uuid, sequence=sequence, status="SUBMITTING"
            )
            try:
                response = self.mexc.place_limit_order(
                    symbol=self.symbol,
                    side=attempt.hedge_side,
                    quantity=attempt.requested_quantity,
                    price=attempt.limit_price,
                    client_order_id=attempt.client_order_id,
                )
            except MexcError as exc:
                if exc.execution_unknown:
                    self.journal.update_attempt(
                        swap_uuid=swap_uuid, sequence=sequence, status="UNKNOWN"
                    )
                    return self.journal.transition(
                        swap_uuid, HedgeState.UNKNOWN, error=str(exc)
                    )
                self.journal.update_attempt(
                    swap_uuid=swap_uuid, sequence=sequence, status="REJECTED"
                )
                return self.journal.transition(
                    swap_uuid, HedgeState.FAILED, error=str(exc)
                )

            order_id = response.get("orderId")
            self.journal.update_attempt(
                swap_uuid=swap_uuid,
                sequence=sequence,
                status="SUBMITTED",
                mexc_order_id=str(order_id) if order_id is not None else None,
            )
            self.journal.transition(swap_uuid, HedgeState.SUBMITTED)

        hedge = self._required_hedge(swap_uuid)
        if hedge.state in {
            HedgeState.SUBMITTING,
            HedgeState.SUBMITTED,
            HedgeState.UNKNOWN,
        }:
            return self._query_and_close_remainder(swap_uuid, sequence)
        return hedge

    def _query_and_close_remainder(self, swap_uuid: str, sequence: int) -> HedgeRecord:
        attempt = self._required_attempt(swap_uuid, sequence)
        try:
            snapshot = self.mexc.query_order(
                symbol=self.symbol, client_order_id=attempt.client_order_id
            )
            status = str(snapshot.get("status", "UNKNOWN"))
            if status in OPEN_ORDER_STATUSES:
                snapshot = self.mexc.cancel_order(
                    symbol=self.symbol, client_order_id=attempt.client_order_id
                )
                status = str(snapshot.get("status", "UNKNOWN"))
        except MexcError as exc:
            self.journal.update_attempt(
                swap_uuid=swap_uuid, sequence=sequence, status="UNKNOWN"
            )
            current = self._required_hedge(swap_uuid)
            if current.state is not HedgeState.UNKNOWN:
                return self.journal.transition(
                    swap_uuid, HedgeState.UNKNOWN, error=str(exc)
                )
            return current

        executed = _decimal_field(snapshot, "executedQty")
        quote = _decimal_field(
            snapshot,
            "cummulativeQuoteQty",
            "cumulativeQuoteQty",
        )
        order_id = snapshot.get("orderId")
        self.journal.update_attempt(
            swap_uuid=swap_uuid,
            sequence=sequence,
            status=status,
            mexc_order_id=str(order_id) if order_id is not None else None,
            executed_quantity=executed,
            quote_quantity=quote,
        )

        if status not in TERMINAL_ORDER_STATUSES:
            current = self._required_hedge(swap_uuid)
            if current.state is HedgeState.UNKNOWN:
                return current
            return self.journal.transition(
                swap_uuid,
                HedgeState.UNKNOWN,
                error=f"MEXC order remains in unexpected state {status}",
            )

        total = self.journal.total_executed(swap_uuid)
        target = self._required_hedge(swap_uuid).target_quantity
        next_state = HedgeState.FILLED if total == target else HedgeState.PARTIAL
        return self.journal.transition(
            swap_uuid,
            next_state,
            filled_quantity=total,
        )

    def _required_hedge(self, swap_uuid: str) -> HedgeRecord:
        record = self.journal.get(swap_uuid)
        if record is None:
            raise KeyError(swap_uuid)
        return record

    def _required_attempt(self, swap_uuid: str, sequence: int) -> HedgeAttempt:
        attempt = self.journal.get_attempt(swap_uuid, sequence)
        if attempt is None:
            raise KeyError((swap_uuid, sequence))
        return attempt


def _decimal_field(payload: Mapping[str, Any], *names: str) -> Decimal:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return Decimal(str(value))
    return Decimal("0")
