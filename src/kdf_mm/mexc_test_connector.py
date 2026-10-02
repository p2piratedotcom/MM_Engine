from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping, Protocol

from .journal import (
    HedgeAttempt,
    HedgeJournal,
    HedgeState,
    JournalConflict,
    client_order_id_for,
)
from .mexc import MexcError, MexcSymbolRules, MexcTimeSync
from .models import HedgeSide, OrderBook
from .pricing import quantity_within_slippage, walk_book


class MexcTestConnectorError(RuntimeError):
    pass


class MexcTestApi(Protocol):
    def symbol_rules(self, symbol: str) -> MexcSymbolRules: ...

    def order_book(self, symbol: str, *, limit: int = 100) -> OrderBook: ...

    def synchronize_time(self, *, max_round_trip_ms: int) -> MexcTimeSync: ...

    def self_symbols(self) -> Mapping[str, Any]: ...

    def account(self) -> Mapping[str, Any]: ...

    def test_limit_order(
        self,
        *,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MexcTestPlan:
    swap_uuid: str
    symbol: str
    side: HedgeSide
    quantity: Decimal
    limit_price: Decimal
    maximum_notional: Decimal
    required_asset: str
    required_balance: Decimal
    available_balance: Decimal
    client_order_id: str

    def payload(self) -> dict[str, Any]:
        return {
            key: value.value if hasattr(value, "value") else str(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True, slots=True)
class MexcTestResult:
    state: HedgeState
    plan: MexcTestPlan
    request_sent: bool
    server_offset_ms: int | None
    round_trip_ms: int | None

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "request_sent": self.request_sent,
            "server_offset_ms": self.server_offset_ms,
            "round_trip_ms": self.round_trip_ms,
            "plan": self.plan.payload(),
        }


class MexcTestConnector:
    """Validates one journalled hedge through MEXC's non-matching test endpoint."""

    def __init__(
        self,
        *,
        journal: HedgeJournal,
        mexc: MexcTestApi,
        max_slippage: Decimal = Decimal("0.01"),
        fee_buffer: Decimal = Decimal("0.001"),
        depth_limit: int = 100,
        max_time_round_trip_ms: int = 2_000,
        require_acknowledged: bool = True,
    ) -> None:
        if max_slippage < 0 or max_slippage >= 1:
            raise ValueError("max_slippage must be in [0, 1)")
        if fee_buffer < 0 or fee_buffer >= 1:
            raise ValueError("fee_buffer must be in [0, 1)")
        if depth_limit <= 0 or depth_limit > 5000:
            raise ValueError("depth_limit must be in [1, 5000]")
        if max_time_round_trip_ms <= 0:
            raise ValueError("max_time_round_trip_ms must be positive")
        self.journal = journal
        self.mexc = mexc
        self.max_slippage = max_slippage
        self.fee_buffer = fee_buffer
        self.depth_limit = depth_limit
        self.max_time_round_trip_ms = max_time_round_trip_ms
        self.require_acknowledged = require_acknowledged

    def validate(
        self,
        swap_uuid: str,
        *,
        sequence: int = 1,
        requested_quantity: Decimal | None = None,
    ) -> MexcTestResult:
        if sequence <= 0 or sequence > 99:
            raise ValueError("hedge sequence must be in [1, 99]")
        received = self.journal.received_event_for_swap(swap_uuid)
        hedge = self.journal.get(swap_uuid)
        if received is None or hedge is None:
            raise MexcTestConnectorError("swap is not present in the Desktop journal")
        if self.require_acknowledged and not received.acknowledged:
            raise MexcTestConnectorError(
                "VPS event must be acknowledged before MEXC validation"
            )
        attempt = self.journal.get_attempt(swap_uuid, sequence)
        remaining = hedge.target_quantity - self.journal.total_executed(swap_uuid)
        quantity = remaining if requested_quantity is None else requested_quantity
        if quantity <= 0 or quantity > remaining:
            raise MexcTestConnectorError("invalid unhedged quantity for MEXC validation")
        expected_client_order_id = (
            received.client_order_id
            if sequence == 1
            else client_order_id_for(swap_uuid, sequence)
        )
        if hedge.state is HedgeState.TEST_VALIDATED:
            if attempt is None or attempt.status != "TEST_VALIDATED":
                raise MexcTestConnectorError(
                    "validated hedge has inconsistent attempt data"
                )
            return MexcTestResult(
                state=hedge.state,
                plan=self._stored_plan(
                    received.hedge_symbol,
                    attempt,
                    base_asset=str(
                        received.event.get(
                            "hedge_base_asset",
                            received.event.get("base_ticker", "ARRR"),
                        )
                    ),
                    quote_asset=str(
                        received.event.get("hedge_quote_asset", "USDT")
                    ),
                ),
                request_sent=False,
                server_offset_ms=None,
                round_trip_ms=None,
            )
        if hedge.state is HedgeState.TESTING:
            self._review_required(
                swap_uuid,
                "test request was interrupted; automatic resubmission is disabled",
            )
            raise MexcTestConnectorError(
                "interrupted MEXC test requires manual review"
            )
        if hedge.state not in {
            HedgeState.RESERVED,
            HedgeState.HEDGE_READY,
            HedgeState.PARTIAL,
        }:
            raise MexcTestConnectorError(
                f"cannot validate a hedge while it is {hedge.state.value}"
            )
        if attempt is not None and (
            attempt.client_order_id != expected_client_order_id
            or attempt.hedge_side is not hedge.hedge_side
            or attempt.requested_quantity != quantity
        ):
            self._review_required(
                swap_uuid, "stored MEXC attempt does not match the received swap"
            )
            raise MexcTestConnectorError(
                "stored MEXC attempt does not match the received swap"
            )

        try:
            rules = self.mexc.symbol_rules(received.hedge_symbol)
            self._validate_rules(
                rules,
                hedge.hedge_side,
                expected_symbol=received.hedge_symbol,
                expected_base_asset=str(
                    received.event.get(
                        "hedge_base_asset",
                        received.event.get("base_ticker", "ARRR"),
                    )
                ),
                expected_quote_asset=str(
                    received.event.get("hedge_quote_asset", "USDT")
                ),
            )
            book = self.mexc.order_book(
                received.hedge_symbol, limit=self.depth_limit
            )
            plan_without_balance = (
                self._plan_from_attempt(received.hedge_symbol, rules, attempt)
                if attempt is not None
                else self._plan_from_book(
                    swap_uuid=swap_uuid,
                    symbol=received.hedge_symbol,
                    side=hedge.hedge_side,
                    quantity=quantity,
                    client_order_id=expected_client_order_id,
                    rules=rules,
                    book=book,
                )
            )
            time_sync = self.mexc.synchronize_time(
                max_round_trip_ms=self.max_time_round_trip_ms
            )
            self._validate_api_symbols(
                self.mexc.self_symbols(), received.hedge_symbol
            )
            balances = self._account_balances(self.mexc.account())
            plan = self._with_balance(plan_without_balance, balances)
        except MexcTestConnectorError as exc:
            self._review_required(swap_uuid, str(exc))
            raise
        except (InvalidOperation, ArithmeticError, TypeError, ValueError) as exc:
            message = f"invalid MEXC preflight data: {exc}"
            self._review_required(swap_uuid, message)
            raise MexcTestConnectorError(message) from exc

        if attempt is None:
            try:
                attempt = self.journal.create_attempt(
                    swap_uuid=swap_uuid,
                    sequence=sequence,
                    requested_quantity=plan.quantity,
                    limit_price=plan.limit_price,
                )
                self.journal.transition(swap_uuid, HedgeState.HEDGE_READY)
            except JournalConflict as exc:
                self._review_required(swap_uuid, str(exc))
                raise MexcTestConnectorError(str(exc)) from exc
        elif hedge.state in {HedgeState.RESERVED, HedgeState.PARTIAL}:
            self.journal.transition(swap_uuid, HedgeState.HEDGE_READY)

        self.journal.update_attempt(
            swap_uuid=swap_uuid,
            sequence=sequence,
            status="TESTING",
        )
        self.journal.transition(swap_uuid, HedgeState.TESTING)
        try:
            response = self.mexc.test_limit_order(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                price=plan.limit_price,
                client_order_id=plan.client_order_id,
            )
            if not isinstance(response, Mapping):
                raise MexcError("MEXC test endpoint returned a non-object response")
        except MexcError as exc:
            uncertain = exc.status is None or (
                exc.status is not None and 500 <= exc.status <= 599
            )
            self.journal.update_attempt(
                swap_uuid=swap_uuid,
                sequence=sequence,
                status="TEST_UNCERTAIN" if uncertain else "TEST_REJECTED",
            )
            self._review_required(swap_uuid, str(exc))
            raise MexcTestConnectorError(
                "MEXC test was not confirmed; no automatic retry will be made"
            ) from exc

        self.journal.update_attempt(
            swap_uuid=swap_uuid,
            sequence=sequence,
            status="TEST_VALIDATED",
        )
        validated = self.journal.transition(
            swap_uuid, HedgeState.TEST_VALIDATED
        )
        return MexcTestResult(
            state=validated.state,
            plan=plan,
            request_sent=bool(response.get("request_sent", True)),
            server_offset_ms=time_sync.offset_ms,
            round_trip_ms=time_sync.round_trip_ms,
        )

    def _plan_from_book(
        self,
        *,
        swap_uuid: str,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        client_order_id: str,
        rules: MexcSymbolRules,
        book: OrderBook,
    ) -> MexcTestPlan:
        units = quantity / rules.quantity_step
        if units != units.to_integral_value():
            raise MexcTestConnectorError(
                "base quantity is not aligned with the current MEXC quantity step"
            )
        levels = book.asks if side is HedgeSide.BUY else book.bids
        capacity = quantity_within_slippage(
            levels, side=side, max_slippage=self.max_slippage
        )
        if capacity < quantity:
            raise MexcTestConnectorError(
                "MEXC depth within the slippage limit cannot cover this swap"
            )
        walked = walk_book(levels, quantity)
        if not walked.complete or walked.limit_price is None:
            raise MexcTestConnectorError("MEXC order book cannot cover this swap")
        rounding = ROUND_CEILING if side is HedgeSide.BUY else ROUND_FLOOR
        price_units = (walked.limit_price / rules.price_step).to_integral_value(
            rounding=rounding
        )
        limit_price = price_units * rules.price_step
        return self._base_plan(
            swap_uuid=swap_uuid,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            client_order_id=client_order_id,
            rules=rules,
        )

    def _plan_from_attempt(
        self,
        symbol: str,
        rules: MexcSymbolRules,
        attempt: HedgeAttempt,
    ) -> MexcTestPlan:
        return self._base_plan(
            swap_uuid=attempt.swap_uuid,
            symbol=symbol,
            side=attempt.hedge_side,
            quantity=attempt.requested_quantity,
            limit_price=attempt.limit_price,
            client_order_id=attempt.client_order_id,
            rules=rules,
        )

    def _base_plan(
        self,
        *,
        swap_uuid: str,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        limit_price: Decimal,
        client_order_id: str,
        rules: MexcSymbolRules,
    ) -> MexcTestPlan:
        if quantity <= 0 or limit_price <= 0:
            raise MexcTestConnectorError("MEXC order quantity and price must be positive")
        if (quantity / rules.quantity_step) != (
            quantity / rules.quantity_step
        ).to_integral_value():
            raise MexcTestConnectorError("stored quantity violates MEXC precision")
        if (limit_price / rules.price_step) != (
            limit_price / rules.price_step
        ).to_integral_value():
            raise MexcTestConnectorError("stored price violates MEXC precision")
        notional = quantity * limit_price
        if notional < rules.min_quote_amount:
            raise MexcTestConnectorError(
                "MEXC test order is below the minimum quote amount"
            )
        if rules.max_quote_amount is not None and notional > rules.max_quote_amount:
            raise MexcTestConnectorError(
                "MEXC test order is above the maximum quote amount"
            )
        required_asset = rules.quote_asset if side is HedgeSide.BUY else rules.base_asset
        required_balance = (
            notional * (Decimal("1") + self.fee_buffer)
            if side is HedgeSide.BUY
            else quantity * (Decimal("1") + self.fee_buffer)
        )
        return MexcTestPlan(
            swap_uuid=swap_uuid,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            maximum_notional=notional,
            required_asset=required_asset,
            required_balance=required_balance,
            available_balance=Decimal("0"),
            client_order_id=client_order_id,
        )

    @staticmethod
    def _validate_rules(
        rules: MexcSymbolRules,
        side: HedgeSide,
        *,
        expected_symbol: str,
        expected_base_asset: str,
        expected_quote_asset: str,
    ) -> None:
        if rules.symbol != expected_symbol:
            raise MexcTestConnectorError(
                "MEXC symbol rules do not match the signed hedge symbol"
            )
        if (
            rules.base_asset != expected_base_asset
            or rules.quote_asset != expected_quote_asset
        ):
            raise MexcTestConnectorError(
                "MEXC symbol assets do not match the signed hedge route"
            )
        if "LIMIT" not in rules.order_types or not rules.allows(side):
            raise MexcTestConnectorError(
                "MEXC does not currently allow the required LIMIT side"
            )

    @staticmethod
    def _validate_api_symbols(payload: Mapping[str, Any], symbol: str) -> None:
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
            raise MexcTestConnectorError("MEXC API symbol response is invalid")
        if symbol not in rows:
            raise MexcTestConnectorError(
                f"API key is not enabled for MEXC symbol {symbol}"
            )

    @staticmethod
    def _account_balances(payload: Mapping[str, Any]) -> dict[str, Decimal]:
        if payload.get("canTrade") is not True or payload.get("accountType") != "SPOT":
            raise MexcTestConnectorError("MEXC account is not enabled for Spot trading")
        rows = payload.get("balances")
        if not isinstance(rows, list):
            raise MexcTestConnectorError("MEXC account balances are missing")
        balances: dict[str, Decimal] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise MexcTestConnectorError("MEXC returned an invalid balance row")
            asset = row.get("asset")
            if not isinstance(asset, str) or not asset or asset in balances:
                raise MexcTestConnectorError("MEXC returned invalid balance assets")
            try:
                free = Decimal(str(row.get("free")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise MexcTestConnectorError("MEXC returned an invalid free balance") from exc
            if not free.is_finite() or free < 0:
                raise MexcTestConnectorError("MEXC returned an invalid free balance")
            balances[asset] = free
        return balances

    @staticmethod
    def _with_balance(
        plan: MexcTestPlan, balances: Mapping[str, Decimal]
    ) -> MexcTestPlan:
        available = balances.get(plan.required_asset, Decimal("0"))
        if available < plan.required_balance:
            raise MexcTestConnectorError(
                f"insufficient free {plan.required_asset} balance for the hedge test"
            )
        return MexcTestPlan(
            swap_uuid=plan.swap_uuid,
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            limit_price=plan.limit_price,
            maximum_notional=plan.maximum_notional,
            required_asset=plan.required_asset,
            required_balance=plan.required_balance,
            available_balance=available,
            client_order_id=plan.client_order_id,
        )

    def _stored_plan(
        self,
        symbol: str,
        attempt: HedgeAttempt,
        *,
        base_asset: str,
        quote_asset: str,
    ) -> MexcTestPlan:
        return MexcTestPlan(
            swap_uuid=attempt.swap_uuid,
            symbol=symbol,
            side=attempt.hedge_side,
            quantity=attempt.requested_quantity,
            limit_price=attempt.limit_price,
            maximum_notional=attempt.requested_quantity * attempt.limit_price,
            required_asset=(
                quote_asset if attempt.hedge_side is HedgeSide.BUY else base_asset
            ),
            required_balance=Decimal("0"),
            available_balance=Decimal("0"),
            client_order_id=attempt.client_order_id,
        )

    def _review_required(self, swap_uuid: str, message: str) -> None:
        hedge = self.journal.get(swap_uuid)
        if hedge is not None and hedge.state in {
            HedgeState.RESERVED,
            HedgeState.HEDGE_READY,
            HedgeState.PARTIAL,
            HedgeState.TESTING,
            HedgeState.TEST_VALIDATED,
        }:
            self.journal.transition(
                swap_uuid,
                HedgeState.REVIEW_REQUIRED,
                error=message,
            )
