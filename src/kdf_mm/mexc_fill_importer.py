from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .journal import EconomicFee, HedgeJournal, MexcTradeFill
from .models import HedgeSide


ZERO = Decimal("0")
TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "PARTIALLY_CANCELED"}


class MexcFillImportError(RuntimeError):
    pass


class MexcFillReadApi(Protocol):
    def synchronize_time(self, *, max_round_trip_ms: int = 2_000) -> Any: ...

    def query_order(
        self, *, symbol: str, client_order_id: str
    ) -> Mapping[str, Any]: ...

    def account_trades(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class MexcFillImportResult:
    swap_uuid: str
    sequence: int
    order_id: str
    order_status: str
    fills_returned: int
    fills_added: int
    fills_already_present: int
    executed_quantity: Decimal
    quote_quantity: Decimal
    commissions_added: int
    commissions_already_present: int
    complete: bool

    def payload(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


class MexcFillImporter:
    """Imports one terminal MEXC order without any trading mutation."""

    def __init__(
        self,
        *,
        journal: HedgeJournal,
        mexc: MexcFillReadApi,
        symbol: str = "ARRRUSDT",
        max_time_round_trip_ms: int = 2_000,
    ) -> None:
        if not symbol:
            raise ValueError("MEXC fill symbol is required")
        if max_time_round_trip_ms <= 0:
            raise ValueError("MEXC time round trip limit must be positive")
        self.journal = journal
        self.mexc = mexc
        self.symbol = symbol.upper()
        self.max_time_round_trip_ms = max_time_round_trip_ms

    def import_swap(self, swap_uuid: str, *, sequence: int = 1) -> MexcFillImportResult:
        if not swap_uuid or sequence <= 0:
            raise ValueError("swap UUID and positive sequence are required")
        received = self.journal.received_event_for_swap(swap_uuid)
        attempt = self.journal.get_attempt(swap_uuid, sequence)
        if received is None or attempt is None:
            raise MexcFillImportError(
                "swap or MEXC attempt is missing from the Desktop journal"
            )
        if received.hedge_symbol != self.symbol:
            raise MexcFillImportError("journal hedge symbol does not match importer")

        self.mexc.synchronize_time(
            max_round_trip_ms=self.max_time_round_trip_ms
        )
        order = self.mexc.query_order(
            symbol=self.symbol,
            client_order_id=attempt.client_order_id,
        )
        verified = self._verify_order(order, attempt)
        raw_trades = self.mexc.account_trades(
            symbol=self.symbol,
            order_id=verified["order_id"],
            limit=100,
        )
        if not isinstance(raw_trades, list) or any(
            not isinstance(item, Mapping) for item in raw_trades
        ):
            raise MexcFillImportError("MEXC account trade list is invalid")
        if len(raw_trades) >= 100:
            raise MexcFillImportError(
                "MEXC returned the 100-trade limit; completeness cannot be proven"
            )

        fills = self._verify_fills(raw_trades, attempt, verified)
        fill_quantity = sum((item.quantity for item in fills), start=ZERO)
        quote_quantity = sum((item.quote_quantity for item in fills), start=ZERO)
        if not _close(fill_quantity, verified["executed_quantity"]):
            raise MexcFillImportError(
                "MEXC fills do not reconcile with the order executed quantity"
            )
        if not _close(quote_quantity, verified["quote_quantity"]):
            raise MexcFillImportError(
                "MEXC fills do not reconcile with the order quote quantity"
            )

        all_stored_fills = {
            (item.symbol, item.trade_id): item
            for item in self.journal.mexc_trade_fills()
        }
        stored_fills = {
            (item.symbol, item.trade_id): item
            for item in all_stored_fills.values()
            if item.swap_uuid == swap_uuid and item.sequence == sequence
        }
        returned_fills = {(item.symbol, item.trade_id): item for item in fills}
        extra_fill_keys = stored_fills.keys() - returned_fills.keys()
        if extra_fill_keys:
            raise MexcFillImportError(
                "stored MEXC fills are absent from the verified exchange response"
            )
        for key, fill in returned_fills.items():
            existing = all_stored_fills.get(key)
            if existing is not None and existing != fill:
                raise MexcFillImportError(
                    "stored MEXC fill differs from the verified exchange response"
                )

        stored_fees = {
            item.fee_key: item for item in self.journal.economic_fees()
        }
        for fill in fills:
            if fill.commission <= ZERO:
                continue
            assert fill.commission_asset is not None
            fee_key = f"mexc:{fill.symbol}:{fill.trade_id}:commission"
            expected_fee = EconomicFee(
                swap_uuid=swap_uuid,
                fee_key=fee_key,
                venue="MEXC",
                asset=fill.commission_asset,
                amount=fill.commission,
                source="MEXC_MY_TRADES",
                occurred_at_ms=fill.traded_at_ms,
            )
            existing_fee = stored_fees.get(fee_key)
            if existing_fee is not None and existing_fee != expected_fee:
                raise MexcFillImportError(
                    "stored MEXC commission differs from the verified exchange response"
                )

        self.journal.update_attempt(
            swap_uuid=swap_uuid,
            sequence=sequence,
            status=verified["status"],
            mexc_order_id=verified["order_id"],
            executed_quantity=verified["executed_quantity"],
            quote_quantity=verified["quote_quantity"],
        )
        added = 0
        commissions_added = 0
        commissions_already_present = 0
        for fill in fills:
            self.journal.record_mexc_trade_fill(
                swap_uuid=fill.swap_uuid,
                sequence=fill.sequence,
                trade_id=fill.trade_id,
                order_id=fill.order_id,
                client_order_id=fill.client_order_id,
                symbol=fill.symbol,
                side=fill.side,
                price=fill.price,
                quantity=fill.quantity,
                quote_quantity=fill.quote_quantity,
                commission=fill.commission,
                commission_asset=fill.commission_asset,
                traded_at_ms=fill.traded_at_ms,
            )
            if (fill.symbol, fill.trade_id) not in stored_fills:
                added += 1
            if fill.commission > ZERO:
                assert fill.commission_asset is not None
                fee_key = f"mexc:{fill.symbol}:{fill.trade_id}:commission"
                self.journal.record_economic_fee(
                    swap_uuid=swap_uuid,
                    fee_key=fee_key,
                    venue="MEXC",
                    asset=fill.commission_asset,
                    amount=fill.commission,
                    source="MEXC_MY_TRADES",
                    occurred_at_ms=fill.traded_at_ms,
                )
                if fee_key in stored_fees:
                    commissions_already_present += 1
                else:
                    commissions_added += 1

        return MexcFillImportResult(
            swap_uuid=swap_uuid,
            sequence=sequence,
            order_id=verified["order_id"],
            order_status=verified["status"],
            fills_returned=len(fills),
            fills_added=added,
            fills_already_present=len(fills) - added,
            executed_quantity=verified["executed_quantity"],
            quote_quantity=verified["quote_quantity"],
            commissions_added=commissions_added,
            commissions_already_present=commissions_already_present,
            complete=True,
        )

    def _verify_order(
        self, payload: Mapping[str, Any], attempt: Any
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise MexcFillImportError("MEXC order response is invalid")
        symbol = str(payload.get("symbol", "")).upper()
        if symbol != self.symbol:
            raise MexcFillImportError("MEXC order returned an unexpected symbol")
        client_order_id = payload.get("clientOrderId")
        if client_order_id not in (None, attempt.client_order_id):
            raise MexcFillImportError("MEXC order client ID does not match journal")
        order_id = _required_id(payload.get("orderId"), "orderId")
        if attempt.mexc_order_id is not None and attempt.mexc_order_id != order_id:
            raise MexcFillImportError("MEXC order ID does not match journal")
        status = str(payload.get("status", ""))
        if status not in TERMINAL_ORDER_STATUSES:
            raise MexcFillImportError(
                f"MEXC order is not terminal: {status or 'missing status'}"
            )
        executed = _non_negative(payload.get("executedQty"), "executedQty")
        quote = _non_negative(
            payload.get("cummulativeQuoteQty", payload.get("cumulativeQuoteQty")),
            "cummulativeQuoteQty",
        )
        if executed > attempt.requested_quantity:
            raise MexcFillImportError("MEXC order exceeds requested hedge quantity")
        if (executed == ZERO) != (quote == ZERO):
            raise MexcFillImportError("MEXC order execution totals are inconsistent")
        return {
            "order_id": order_id,
            "status": status,
            "executed_quantity": executed,
            "quote_quantity": quote,
        }

    def _verify_fills(
        self,
        rows: list[Mapping[str, Any]],
        attempt: Any,
        order: Mapping[str, Any],
    ) -> tuple[MexcTradeFill, ...]:
        seen: set[str] = set()
        fills: list[MexcTradeFill] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbol != self.symbol:
                raise MexcFillImportError("MEXC fill returned an unexpected symbol")
            trade_id = _required_id(row.get("id"), "trade id")
            if trade_id in seen:
                raise MexcFillImportError("MEXC returned a duplicate trade ID")
            seen.add(trade_id)
            if _required_id(row.get("orderId"), "fill orderId") != order["order_id"]:
                raise MexcFillImportError("MEXC fill order ID does not match")
            client_order_id = row.get("clientOrderId")
            if client_order_id not in (None, attempt.client_order_id):
                raise MexcFillImportError("MEXC fill client ID does not match")
            is_buyer = row.get("isBuyer")
            if not isinstance(is_buyer, bool):
                raise MexcFillImportError("MEXC fill omitted buyer side")
            side = HedgeSide.BUY if is_buyer else HedgeSide.SELL
            if side is not attempt.hedge_side:
                raise MexcFillImportError("MEXC fill side does not match hedge")
            price = _positive(row.get("price"), "price")
            quantity = _positive(row.get("qty"), "qty")
            quote_quantity = _positive(row.get("quoteQty"), "quoteQty")
            expected_quote = price * quantity
            if not _close(expected_quote, quote_quantity, absolute=Decimal("0.00000001")):
                raise MexcFillImportError("MEXC fill price and quote quantity disagree")
            commission = _non_negative(row.get("commission", "0"), "commission")
            raw_asset = row.get("commissionAsset")
            commission_asset = (
                str(raw_asset).upper() if isinstance(raw_asset, str) and raw_asset else None
            )
            if commission > ZERO and commission_asset is None:
                raise MexcFillImportError("MEXC fill omitted commission asset")
            traded_at_ms = _positive_integer(row.get("time"), "trade time")
            fills.append(
                MexcTradeFill(
                    swap_uuid=attempt.swap_uuid,
                    sequence=attempt.sequence,
                    trade_id=trade_id,
                    order_id=str(order["order_id"]),
                    client_order_id=attempt.client_order_id,
                    symbol=self.symbol,
                    side=side,
                    price=price,
                    quantity=quantity,
                    quote_quantity=quote_quantity,
                    commission=commission,
                    commission_asset=commission_asset,
                    traded_at_ms=traded_at_ms,
                )
            )
        return tuple(sorted(fills, key=lambda item: (item.traded_at_ms, item.trade_id)))


def _required_id(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MexcFillImportError(f"MEXC {name} is invalid")
    parsed = str(value)
    if not parsed or len(parsed) > 128:
        raise MexcFillImportError(f"MEXC {name} is invalid")
    return parsed


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MexcFillImportError(f"MEXC {name} is invalid") from exc
    if not parsed.is_finite():
        raise MexcFillImportError(f"MEXC {name} is not finite")
    return parsed


def _non_negative(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed < ZERO:
        raise MexcFillImportError(f"MEXC {name} is negative")
    return parsed


def _positive(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed <= ZERO:
        raise MexcFillImportError(f"MEXC {name} is not positive")
    return parsed


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MexcFillImportError(f"MEXC {name} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MexcFillImportError(f"MEXC {name} is invalid") from exc
    if parsed <= 0:
        raise MexcFillImportError(f"MEXC {name} is invalid")
    return parsed


def _close(
    left: Decimal,
    right: Decimal,
    *,
    absolute: Decimal = Decimal("0.000000000001"),
) -> bool:
    tolerance = max(absolute, abs(right) * Decimal("0.00000001"))
    return abs(left - right) <= tolerance
