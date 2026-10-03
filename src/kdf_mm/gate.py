from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from .http import JsonTransport, TransportError, UrllibJsonTransport
from .mexc import (
    LiveTradingDisabled,
    MexcError,
    MexcSymbolRules,
    MexcTimeSync,
    SymbolCheck,
)
from .models import HedgeSide, OrderBook


class GateError(MexcError):
    """Gate error with the same execution-uncertainty contract used by hedging."""


class GateClient:
    supports_read_deadlines = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = "https://api.gateio.ws/api/v4",
        transport: JsonTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
        trading_enabled: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibJsonTransport()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.trading_enabled = trading_enabled
        self.timeout = timeout
        self._time_offset_ms = 0
        self._time_lock = threading.RLock()

    @staticmethod
    def _pair(symbol: str) -> str:
        value = symbol.strip().upper().replace("-", "_")
        if "_" not in value:
            if not value.endswith("USDT") or len(value) <= 4:
                raise ValueError(f"simbolo Gate non riconosciuto: {symbol}")
            value = value[:-4] + "_USDT"
        return value

    @staticmethod
    def _symbol(pair: str) -> str:
        return pair.replace("_", "").upper()

    @staticmethod
    def _text(client_order_id: str) -> str:
        if not client_order_id:
            raise ValueError("client_order_id richiesto")
        # Gate requires a t- prefix and a short restricted identifier. Hashing
        # is deterministic, so recovery after a timeout queries the same order.
        return "t-" + hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()[:24]

    def server_time(self, *, timeout: float | None = None) -> Mapping[str, Any]:
        raw = self._public("GET", "/spot/time", timeout=timeout, total_timeout=timeout)
        if not isinstance(raw, Mapping):
            raise GateError("Gate time response is not an object")
        value = raw.get("server_time_ms")
        if value is None:
            value = Decimal(str(raw.get("server_time"))) * 1000
        return {"serverTime": int(value)}

    def synchronize_time(self, *, max_round_trip_ms: int = 2_000) -> MexcTimeSync:
        started = self.clock_ms()
        payload = self.server_time(timeout=min(self.timeout, max_round_trip_ms / 1000))
        finished = self.clock_ms()
        round_trip = finished - started
        if round_trip < 0 or round_trip > max_round_trip_ms:
            raise GateError(f"Gate time synchronization took {round_trip} ms")
        server = int(payload["serverTime"])
        midpoint = started + round_trip // 2
        with self._time_lock:
            self._time_offset_ms = server - midpoint
        return MexcTimeSync(server, midpoint, server - midpoint, round_trip)

    def currency_pair(self, symbol: str, *, timeout: float | None = None,
                      total_timeout: float | None = None) -> Mapping[str, Any]:
        return self._public("GET", f"/spot/currency_pairs/{quote(self._pair(symbol))}",
                            timeout=timeout, total_timeout=total_timeout)

    def check_symbol(self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
                     total_timeout: float | None = None) -> SymbolCheck:
        try:
            raw = self.currency_pair(symbol, timeout=timeout, total_timeout=total_timeout)
        except GateError as exc:
            if exc.status == 404:
                return SymbolCheck(symbol.upper(), False, False, (), ("symbol not returned by Gate",))
            raise
        allowed = str(raw.get("trade_status", "")).lower() == "tradable"
        problems = () if allowed else (f"Gate trade_status is {raw.get('trade_status')!r}",)
        return SymbolCheck(symbol.upper(), True, allowed, ("LIMIT",), problems, raw)

    def symbol_rules(self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
                     total_timeout: float | None = None) -> MexcSymbolRules:
        check = self.check_symbol(symbol, timeout=timeout, total_timeout=total_timeout)
        if not check.listed or not check.spot_trading_allowed or check.problems:
            raise GateError(f"Gate {symbol} non negoziabile: {'; '.join(check.problems)}")
        raw = check.raw or {}
        try:
            base = str(raw["base"]).upper()
            quote_asset = str(raw["quote"]).upper()
            amount_precision = int(raw["amount_precision"])
            price_precision = int(raw["precision"])
            min_quote = Decimal(str(raw.get("min_quote_amount") or "0"))
            if min_quote <= 0:
                min_base = Decimal(str(raw.get("min_base_amount") or "0"))
                tickers = self.ticker_24h(symbol, timeout=timeout, total_timeout=total_timeout)
                min_quote = min_base * Decimal(str(tickers.get("last") or "0"))
            max_quote_raw = raw.get("max_quote_amount")
            max_quote = Decimal(str(max_quote_raw)) if max_quote_raw not in (None, "", "0", 0) else None
            quantity_step = Decimal(1).scaleb(-amount_precision)
            price_step = Decimal(1).scaleb(-price_precision)
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise GateError("Gate currency-pair rules are invalid") from exc
        if min_quote <= 0 or quantity_step <= 0 or price_step <= 0:
            raise GateError("Gate currency-pair limits are invalid")
        return MexcSymbolRules(symbol.upper(), base, quote_asset, quantity_step,
                               price_step, min_quote, max_quote, ("LIMIT",), 1)

    def order_book(self, symbol: str = "ARRRUSDT", *, limit: int = 100,
                   timeout: float | None = None,
                   total_timeout: float | None = None) -> OrderBook:
        if limit <= 0 or limit > 5000:
            raise ValueError("Gate depth limit must be in [1, 5000]")
        observed = self.clock_ms()
        raw = self._public("GET", "/spot/order_book",
                           {"currency_pair": self._pair(symbol), "limit": min(limit, 100)},
                           timeout=timeout, total_timeout=total_timeout)
        return OrderBook.from_mexc(dict(raw), observed_at_ms=observed)

    def ticker_24h(self, symbol: str = "ARRRUSDT", *, timeout: float | None = None,
                   total_timeout: float | None = None) -> Mapping[str, Any]:
        raw = self._public("GET", "/spot/tickers", {"currency_pair": self._pair(symbol)},
                           timeout=timeout, total_timeout=total_timeout)
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], Mapping):
            raise GateError("Gate ticker response is invalid")
        row = dict(raw[0])
        row["volume"] = row.get("base_volume", row.get("volume", "0"))
        return row

    def self_symbols(self, *, timeout: float | None = None,
                     total_timeout: float | None = None) -> Mapping[str, Any]:
        rows = self._public("GET", "/spot/currency_pairs", timeout=timeout,
                            total_timeout=total_timeout)
        if not isinstance(rows, list):
            raise GateError("Gate currency-pair list is invalid")
        return {"data": [self._symbol(str(row["id"])) for row in rows
                         if isinstance(row, Mapping)
                         and str(row.get("trade_status", "")).lower() == "tradable"
                         and isinstance(row.get("id"), str)]}

    def account(self, *, timeout: float | None = None,
                total_timeout: float | None = None) -> Mapping[str, Any]:
        rows = self._signed("GET", "/spot/accounts", timeout=timeout,
                            total_timeout=total_timeout)
        if not isinstance(rows, list):
            raise GateError("Gate account response is invalid")
        return {"canTrade": True, "accountType": "SPOT", "balances": [
            {"asset": str(row.get("currency", "")).upper(),
             "free": str(row.get("available", "0")),
             "locked": str(row.get("locked", "0"))}
            for row in rows if isinstance(row, Mapping)
        ]}

    def trade_fee(self, symbol: str = "ARRRUSDT") -> Mapping[str, Any]:
        raw = self._signed("GET", "/wallet/fee", {"currency_pair": self._pair(symbol)})
        if not isinstance(raw, Mapping):
            raise GateError("Gate fee response is invalid")
        return {"symbol": symbol.upper(), "makerCommission": raw.get("maker_fee"),
                "takerCommission": raw.get("taker_fee"), **dict(raw)}

    def open_orders(self, symbol: str | None = None) -> Any:
        params = {"currency_pair": self._pair(symbol) if symbol else "!all", "status": "open"}
        return [self._normalize_order(row) for row in self._signed("GET", "/spot/orders", params)]

    def account_trades(self, *, symbol: str, order_id: str | None = None,
                       start_time_ms: int | None = None, end_time_ms: int | None = None,
                       limit: int = 100) -> Any:
        params: dict[str, object] = {"currency_pair": self._pair(symbol), "limit": min(limit, 1000)}
        if order_id:
            params["order_id"] = order_id
        if start_time_ms:
            params["from"] = start_time_ms // 1000
        if end_time_ms:
            params["to"] = end_time_ms // 1000
        rows = self._signed("GET", "/spot/my_trades", params)
        return [{"id": str(row.get("id")), "orderId": str(row.get("order_id")),
                 "symbol": symbol.upper(), "price": str(row.get("price", "0")),
                 "qty": str(row.get("amount", "0")),
                 "quoteQty": str(row.get("total") or
                                  Decimal(str(row.get("price", "0"))) *
                                  Decimal(str(row.get("amount", "0")))),
                 "commission": str(row.get("fee", "0")),
                 "commissionAsset": str(row.get("fee_currency", "")),
                 "time": int(Decimal(str(row.get("create_time_ms") or "0")))}
                for row in rows if isinstance(row, Mapping)]

    def test_limit_order(self, *, symbol: str, side: HedgeSide, quantity: Decimal,
                         price: Decimal, client_order_id: str) -> Mapping[str, Any]:
        # Gate has no non-matching test endpoint. The common preflight already
        # validates pair rules, permissions, funds and book depth; this method
        # deliberately performs no write.
        if quantity <= 0 or price <= 0 or not client_order_id:
            raise ValueError("invalid Gate limit order")
        return {"validated": True, "request_sent": False}

    def place_limit_order(self, *, symbol: str, side: HedgeSide, quantity: Decimal,
                          price: Decimal, client_order_id: str) -> Mapping[str, Any]:
        if not self.trading_enabled:
            raise LiveTradingDisabled("live Gate trading is disabled")
        body = {"text": self._text(client_order_id), "currency_pair": self._pair(symbol),
                "type": "limit", "account": "spot", "side": side.value.lower(),
                "amount": format(quantity, "f"), "price": format(price, "f"),
                "time_in_force": "gtc"}
        raw = self._signed("POST", "/spot/orders", body=body,
                           execution_may_be_unknown=True)
        return self._normalize_order(raw, local_client_id=client_order_id)

    def query_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        raw = self._signed("GET", f"/spot/orders/{quote(self._text(client_order_id))}",
                           {"currency_pair": self._pair(symbol)})
        return self._normalize_order(raw, local_client_id=client_order_id)

    def cancel_order(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        if not self.trading_enabled:
            raise LiveTradingDisabled("live Gate trading is disabled")
        raw = self._signed("DELETE", f"/spot/orders/{quote(self._text(client_order_id))}",
                           {"currency_pair": self._pair(symbol)},
                           execution_may_be_unknown=True)
        return self._normalize_order(raw, local_client_id=client_order_id)

    def _normalize_order(self, raw: Mapping[str, Any], *, local_client_id: str | None = None) -> dict[str, Any]:
        amount = Decimal(str(raw.get("amount") or "0"))
        left = Decimal(str(raw.get("left") or "0"))
        executed = max(Decimal("0"), amount - left)
        gate_status = str(raw.get("status", "")).lower()
        finish_as = str(raw.get("finish_as", "")).lower()
        if gate_status == "open":
            status = "PARTIALLY_FILLED" if executed > 0 else "NEW"
        elif gate_status == "closed" and amount > 0 and left == 0 and finish_as in {"filled", "ioc", ""}:
            status = "FILLED"
        else:
            status = "CANCELED"
        pair = str(raw.get("currency_pair", ""))
        return {"orderId": str(raw.get("id", "")),
                "clientOrderId": local_client_id or str(raw.get("text", "")),
                "symbol": self._symbol(pair), "side": str(raw.get("side", "")).upper(),
                "status": status, "executedQty": str(executed),
                "cummulativeQuoteQty": str(raw.get("filled_total") or "0"),
                "price": str(raw.get("price") or "0"), "origQty": str(amount),
                "fee": str(raw.get("fee") or "0"),
                "feeCurrency": str(raw.get("fee_currency") or ""),
                "raw_status": gate_status, "finish_as": finish_as}

    def _public(self, method: str, path: str, params: Mapping[str, object] | None = None,
                *, timeout: float | None = None, total_timeout: float | None = None) -> Any:
        query_string = urlencode([(k, str(v)) for k, v in (params or {}).items()])
        url = f"{self.base_url}{path}" + (f"?{query_string}" if query_string else "")
        return self._send(method, url, headers={}, timeout=timeout, total_timeout=total_timeout)

    def _signed(self, method: str, path: str, params: Mapping[str, object] | None = None,
                *, body: Mapping[str, object] | None = None,
                execution_may_be_unknown: bool = False,
                timeout: float | None = None, total_timeout: float | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise GateError("Gate API credentials are required")
        query_string = urlencode([(k, str(v)) for k, v in (params or {}).items()])
        body_bytes = (json.dumps(dict(body), separators=(",", ":"), sort_keys=True).encode("utf-8")
                      if body is not None else b"")
        with self._time_lock:
            timestamp = str((self.clock_ms() + self._time_offset_ms) // 1000)
        body_hash = hashlib.sha512(body_bytes).hexdigest()
        canonical = f"{method}\n/api/v4{path}\n{query_string}\n{body_hash}\n{timestamp}"
        signature = hmac.new(self.api_secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
        headers = {"KEY": self.api_key, "Timestamp": timestamp, "SIGN": signature,
                   "Content-Type": "application/json"}
        url = f"{self.base_url}{path}" + (f"?{query_string}" if query_string else "")
        return self._send(method, url, headers=headers, body=body_bytes or None,
                          execution_may_be_unknown=execution_may_be_unknown,
                          timeout=timeout, total_timeout=total_timeout)

    def _send(self, method: str, url: str, *, headers: Mapping[str, str],
              body: bytes | None = None, execution_may_be_unknown: bool = False,
              timeout: float | None = None, total_timeout: float | None = None) -> Any:
        try:
            kwargs = dict(method=method, url=url, headers=headers, body=body,
                          timeout=self.timeout if timeout is None else min(timeout, self.timeout))
            if total_timeout is not None:
                kwargs["total_timeout"] = total_timeout
            return self.transport.request(**kwargs)
        except TransportError as exc:
            unknown = execution_may_be_unknown and (exc.status is None or exc.status >= 500)
            detail = ""
            if isinstance(exc.payload, Mapping):
                label = exc.payload.get("label")
                message = exc.payload.get("message") or exc.payload.get("detail")
                detail = f": Gate {label}: {message}" if label or message else ""
            raise GateError(f"{exc}{detail}", status=exc.status, payload=exc.payload,
                            execution_unknown=unknown) from exc
