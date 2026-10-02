from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode

from .http import JsonTransport, TransportError, UrllibJsonTransport
from .models import HedgeSide, OrderBook


class MexcOrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"
    IMMEDIATE_OR_CANCEL = "IMMEDIATE_OR_CANCEL"
    FILL_OR_KILL = "FILL_OR_KILL"


class MexcError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: Any = None,
        execution_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.execution_unknown = execution_unknown


class LiveTradingDisabled(MexcError):
    pass


class LiveTransfersDisabled(MexcError):
    pass


def _api_value(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_params(params: Mapping[str, object] | Iterable[tuple[str, object]]) -> str:
    items = params.items() if isinstance(params, Mapping) else params
    return urlencode([(key, _api_value(value)) for key, value in items])


def sign_query(query: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SymbolCheck:
    symbol: str
    listed: bool
    spot_trading_allowed: bool
    order_types: tuple[str, ...]
    problems: tuple[str, ...]
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MexcTimeSync:
    server_time_ms: int
    local_midpoint_ms: int
    offset_ms: int
    round_trip_ms: int


@dataclass(frozen=True, slots=True)
class MexcSymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    quantity_step: Decimal
    price_step: Decimal
    min_quote_amount: Decimal
    max_quote_amount: Decimal | None
    order_types: tuple[str, ...]
    trade_side_type: int

    def allows(self, side: HedgeSide) -> bool:
        return self.trade_side_type == 1 or (
            self.trade_side_type == 2 and side is HedgeSide.BUY
        ) or (self.trade_side_type == 3 and side is HedgeSide.SELL)


class MexcClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = "https://api.mexc.com",
        transport: JsonTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = 5000,
        trading_enabled: bool = False,
        transfers_enabled: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if recv_window_ms <= 0 or recv_window_ms > 60_000:
            raise ValueError("recv_window_ms must be in [1, 60000]")
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibJsonTransport()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.recv_window_ms = recv_window_ms
        self.trading_enabled = trading_enabled
        self.transfers_enabled = transfers_enabled
        self.timeout = timeout
        self._time_offset_ms = 0
        self._time_lock = threading.RLock()

    def server_time(self, *, timeout: float | None = None) -> Mapping[str, Any]:
        return self._public(
            "GET", "/api/v3/time", timeout=timeout,
            total_timeout=timeout,
        )

    def synchronize_time(self, *, max_round_trip_ms: int = 2_000) -> MexcTimeSync:
        if max_round_trip_ms <= 0:
            raise ValueError("max_round_trip_ms must be positive")
        started = self.clock_ms()
        payload = self.server_time(timeout=min(self.timeout, max_round_trip_ms / 1000))
        finished = self.clock_ms()
        if finished < started:
            raise MexcError("local clock moved backwards during MEXC time sync")
        round_trip = finished - started
        if round_trip > max_round_trip_ms:
            raise MexcError(
                f"MEXC time synchronization took {round_trip} ms, above the limit"
            )
        if not isinstance(payload, Mapping):
            raise MexcError("MEXC time response is not an object")
        server_time = payload.get("serverTime")
        if not isinstance(server_time, int) or isinstance(server_time, bool):
            raise MexcError("MEXC time response is invalid")
        midpoint = started + round_trip // 2
        offset = server_time - midpoint
        with self._time_lock:
            self._time_offset_ms = offset
        return MexcTimeSync(
            server_time_ms=server_time,
            local_midpoint_ms=midpoint,
            offset_ms=offset,
            round_trip_ms=round_trip,
        )

    def exchange_info(
        self, symbol: str, *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Mapping[str, Any]:
        return self._public(
            "GET", "/api/v3/exchangeInfo", {"symbol": symbol},
            timeout=timeout, total_timeout=total_timeout,
        )

    def order_book(
        self, symbol: str = "ARRRUSDT", *, limit: int = 100,
        timeout: float | None = None, total_timeout: float | None = None,
    ) -> OrderBook:
        if limit <= 0 or limit > 5000:
            raise ValueError("MEXC depth limit must be in [1, 5000]")
        observed_at = self.clock_ms()
        payload = self._public(
            "GET", "/api/v3/depth", {"symbol": symbol, "limit": limit},
            timeout=timeout, total_timeout=total_timeout,
        )
        return OrderBook.from_mexc(dict(payload), observed_at_ms=observed_at)

    def ticker_24h(
        self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Mapping[str, Any]:
        return self._public(
            "GET", "/api/v3/ticker/24hr", {"symbol": symbol},
            timeout=timeout, total_timeout=total_timeout,
        )

    def check_symbol(
        self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> SymbolCheck:
        payload = self.exchange_info(
            symbol, timeout=timeout, total_timeout=total_timeout,
        )
        symbols = payload.get("symbols", [])
        match = next((item for item in symbols if item.get("symbol") == symbol), None)
        if match is None:
            return SymbolCheck(symbol, False, False, (), ("symbol not returned by exchangeInfo",))

        problems: list[str] = []
        allowed = bool(match.get("isSpotTradingAllowed"))
        order_types = tuple(str(value) for value in match.get("orderTypes", ()))
        if not allowed:
            problems.append("API spot trading is not allowed")
        if MexcOrderType.LIMIT.value not in order_types:
            problems.append("LIMIT is not advertised")
        if match.get("tradeSideType") not in (None, "1", 1):
            problems.append(f"tradeSideType is {match.get('tradeSideType')!r}, not two-sided")
        return SymbolCheck(symbol, True, allowed, order_types, tuple(problems), match)

    def symbol_rules(
        self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> MexcSymbolRules:
        check = self.check_symbol(symbol, timeout=timeout, total_timeout=total_timeout)
        if not check.listed or not check.spot_trading_allowed or check.problems:
            detail = "; ".join(check.problems) or "symbol is unavailable"
            raise MexcError(f"MEXC {symbol} is not tradable: {detail}")
        if not isinstance(check.raw, Mapping):
            raise MexcError("MEXC exchangeInfo omitted symbol rules")
        raw = check.raw
        try:
            base_asset = _required_text(raw, "baseAsset")
            quote_asset = _required_text(raw, "quoteAsset")
            quantity_raw = raw.get("baseSizePrecision")
            if quantity_raw in (None, "", "0", 0):
                quantity_raw = Decimal(1).scaleb(-int(raw["baseAssetPrecision"]))
            quantity_step = _positive_decimal(quantity_raw)
            price_digits = int(
                raw.get("quoteAssetPrecision", raw.get("quotePrecision"))
            )
            if price_digits < 0 or price_digits > 18:
                raise ValueError("price precision is outside [0, 18]")
            price_step = Decimal(1).scaleb(-price_digits)
            min_quote = _positive_decimal(raw["quoteAmountPrecision"])
            max_quote_raw = raw.get("maxQuoteAmount")
            max_quote = (
                _positive_decimal(max_quote_raw)
                if max_quote_raw not in (None, "", "0", 0)
                else None
            )
            trade_side_type = int(raw.get("tradeSideType", 1))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise MexcError("MEXC symbol rules are invalid") from exc
        if trade_side_type not in {1, 2, 3}:
            raise MexcError("MEXC symbol is closed for API trading")
        return MexcSymbolRules(
            symbol=symbol.upper(),
            base_asset=base_asset,
            quote_asset=quote_asset,
            quantity_step=quantity_step,
            price_step=price_step,
            min_quote_amount=min_quote,
            max_quote_amount=max_quote,
            order_types=check.order_types,
            trade_side_type=trade_side_type,
        )

    def self_symbols(
        self, *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Mapping[str, Any]:
        return self._signed(
            "GET", "/api/v3/selfSymbols", timeout=timeout,
            total_timeout=total_timeout,
        )

    def account(
        self, *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Mapping[str, Any]:
        return self._signed(
            "GET", "/api/v3/account", timeout=timeout,
            total_timeout=total_timeout,
        )

    def open_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self._signed("GET", "/api/v3/openOrders", params)

    def account_trades(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
    ) -> Any:
        if not symbol:
            raise ValueError("MEXC trade symbol is required")
        if limit <= 0 or limit > 100:
            raise ValueError("MEXC account trade limit must be in [1, 100]")
        if start_time_ms is not None and start_time_ms <= 0:
            raise ValueError("MEXC trade start time must be positive")
        if end_time_ms is not None and end_time_ms <= 0:
            raise ValueError("MEXC trade end time must be positive")
        if (
            start_time_ms is not None
            and end_time_ms is not None
            and start_time_ms > end_time_ms
        ):
            raise ValueError("MEXC trade start time cannot follow end time")
        params: dict[str, object] = {"symbol": symbol.upper(), "limit": limit}
        if order_id:
            params["orderId"] = order_id
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return self._signed("GET", "/api/v3/myTrades", params)

    def trade_fee(self, symbol: str = "ARRRUSDT") -> Mapping[str, Any]:
        return self._signed("GET", "/api/v3/tradeFee", {"symbol": symbol})

    def currency_information(self) -> list[Mapping[str, Any]]:
        return self._signed("GET", "/api/v3/capital/config/getall")

    def query_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        return self._signed(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )

    def test_limit_order(
        self,
        *,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> Mapping[str, Any]:
        return self._signed(
            "POST",
            "/api/v3/order/test",
            self._limit_params(symbol, side, quantity, price, client_order_id),
        )

    def place_limit_order(
        self,
        *,
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> Mapping[str, Any]:
        if not self.trading_enabled:
            raise LiveTradingDisabled("live MEXC trading is disabled")
        return self._signed(
            "POST",
            "/api/v3/order",
            self._limit_params(symbol, side, quantity, price, client_order_id),
            execution_may_be_unknown=True,
        )

    def cancel_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        if not self.trading_enabled:
            raise LiveTradingDisabled("live MEXC trading is disabled")
        return self._signed(
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            execution_may_be_unknown=True,
        )

    def withdraw(
        self,
        *,
        coin: str,
        network: str,
        address: str,
        amount: Decimal,
        withdraw_order_id: str,
        memo: str | None = None,
    ) -> Mapping[str, Any]:
        if not self.transfers_enabled:
            raise LiveTransfersDisabled("live MEXC transfers are disabled")
        params: dict[str, object] = {
            "coin": coin,
            "netWork": network,
            "address": address,
            "amount": amount,
            "withdrawOrderId": withdraw_order_id,
        }
        if memo:
            params["memo"] = memo
        return self._signed(
            "POST",
            "/api/v3/capital/withdraw",
            params,
            execution_may_be_unknown=True,
        )

    @staticmethod
    def _limit_params(
        symbol: str,
        side: HedgeSide,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> dict[str, object]:
        if quantity <= 0 or price <= 0:
            raise ValueError("limit quantity and price must be positive")
        if not client_order_id:
            raise ValueError("client_order_id is required")
        return {
            "symbol": symbol,
            "side": side.value,
            "type": MexcOrderType.LIMIT.value,
            "quantity": quantity,
            "price": price,
            "newClientOrderId": client_order_id,
        }

    def _public(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *, timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Any:
        query = encode_params(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        return self._send(
            method, url, headers={}, timeout=timeout,
            total_timeout=total_timeout,
        )

    def _signed(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        execution_may_be_unknown: bool = False,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Any:
        if not self.api_key or not self.api_secret:
            raise MexcError("MEXC API credentials are required")

        signed: list[tuple[str, object]] = list((params or {}).items())
        signed.append(("recvWindow", self.recv_window_ms))
        with self._time_lock:
            timestamp = self.clock_ms() + self._time_offset_ms
        signed.append(("timestamp", timestamp))
        unsigned_query = encode_params(signed)
        signature = sign_query(unsigned_query, self.api_secret)
        body_query = f"{unsigned_query}&signature={signature}"
        headers = {"X-MEXC-APIKEY": self.api_key}
        if method == "GET":
            return self._send(
                method,
                f"{self.base_url}{path}?{body_query}",
                headers=headers,
                execution_may_be_unknown=execution_may_be_unknown,
                timeout=timeout,
                total_timeout=total_timeout,
            )
        headers["Content-Type"] = "application/json"
        return self._send(
            method,
            f"{self.base_url}{path}?{body_query}",
            headers=headers,
            execution_may_be_unknown=execution_may_be_unknown,
            timeout=timeout,
            total_timeout=total_timeout,
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        execution_may_be_unknown: bool = False,
        timeout: float | None = None,
        total_timeout: float | None = None,
    ) -> Any:
        try:
            request_kwargs = dict(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout=self.timeout if timeout is None else min(timeout, self.timeout),
            )
            if total_timeout is not None:
                request_kwargs["total_timeout"] = total_timeout
            return self.transport.request(**request_kwargs)
        except TransportError as exc:
            unknown = execution_may_be_unknown and (
                exc.status is None or 500 <= exc.status <= 599
            )
            raise MexcError(
                _transport_error_message(exc),
                status=exc.status,
                payload=exc.payload,
                execution_unknown=unknown,
            ) from exc


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing MEXC symbol field {name}")
    return value


def _positive_decimal(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("MEXC value must be a positive finite decimal")
    return parsed


def _transport_error_message(error: TransportError) -> str:
    message = str(error)
    if not isinstance(error.payload, Mapping):
        return message
    code = error.payload.get("code")
    detail = error.payload.get("msg") or error.payload.get("message")
    if not isinstance(code, (str, int)) or isinstance(code, bool):
        return message
    if not isinstance(detail, str) or not detail or len(detail) > 300:
        return f"{message}: MEXC code {code}"
    return f"{message}: MEXC code {code}: {detail}"
