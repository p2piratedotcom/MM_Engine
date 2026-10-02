from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .credentials import LinuxSecretService, SecretServiceError
from .mexc import MexcClient, MexcError, MexcTimeSync
from .models import OrderBook


DISPLAY_ASSETS = ("ARRR", "USDT")
DISPLAY_SYMBOL = "ARRRUSDT"


class MexcReadApi(Protocol):
    trading_enabled: bool
    transfers_enabled: bool

    def synchronize_time(self, *, max_round_trip_ms: int) -> MexcTimeSync: ...

    def account(self) -> Mapping[str, Any]: ...

    def self_symbols(self) -> Mapping[str, Any]: ...

    def open_orders(self, symbol: str | None = None) -> Any: ...

    def order_book(self, symbol: str, *, limit: int = 100) -> OrderBook: ...


class MexcStatusSource(Protocol):
    def payload(self) -> dict[str, Any]: ...


class MexcPrivateStatus:
    """Builds a display-safe snapshot using only MEXC read endpoints."""

    def __init__(
        self,
        client: MexcReadApi,
        *,
        symbol: str = DISPLAY_SYMBOL,
        base_asset: str = "ARRR",
        quote_asset: str = "USDT",
        refresh_interval_seconds: float = 10.0,
        max_time_round_trip_ms: int = 2_000,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not symbol or not base_asset or not quote_asset:
            raise ValueError("MEXC symbol and assets are required")
        if refresh_interval_seconds < 1:
            raise ValueError("MEXC refresh interval must be at least one second")
        if max_time_round_trip_ms <= 0:
            raise ValueError("MEXC time round trip limit must be positive")
        self.client = client
        self.symbol = symbol.upper()
        self.base_asset = base_asset.upper()
        self.quote_asset = quote_asset.upper()
        if self.base_asset == self.quote_asset:
            raise ValueError("MEXC base and quote assets must be different")
        self.display_assets = (self.base_asset, self.quote_asset)
        self.refresh_interval_seconds = refresh_interval_seconds
        self.max_time_round_trip_ms = max_time_round_trip_ms
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._cached: dict[str, Any] | None = None
        self._cached_at: float | None = None

    def payload(self) -> dict[str, Any]:
        now = self.monotonic()
        with self._lock:
            if (
                self._cached is not None
                and self._cached_at is not None
                and now - self._cached_at < self.refresh_interval_seconds
            ):
                return self._cached
            result = self._collect()
            self._cached = result
            self._cached_at = now
            return result

    def _collect(self) -> dict[str, Any]:
        result = _empty_payload(
            configured=True,
            symbol=self.symbol,
            display_assets=self.display_assets,
        )
        result["observed_at_ms"] = int(self.clock_ms())
        result["safety"] = {
            "live_trading_enabled": bool(self.client.trading_enabled),
            "live_transfers_enabled": bool(self.client.transfers_enabled),
        }
        errors: dict[str, str] = {}
        successful = 0

        try:
            book = self.client.order_book(self.symbol, limit=5)
            if not book.bids or not book.asks:
                raise ValueError(f"MEXC {self.symbol} order book is empty")
            bid = book.bids[0].price
            ask = book.asks[0].price
            result["market"] = {
                "available": True,
                "best_bid": format(bid, "f"),
                "best_ask": format(ask, "f"),
                "midpoint": format((bid + ask) / Decimal("2"), "f"),
                "observed_at_ms": book.observed_at_ms,
            }
            successful += 1
        except (MexcError, InvalidOperation, OSError, TypeError, ValueError) as exc:
            errors["market"] = _safe_error(exc)

        try:
            synced = self.client.synchronize_time(
                max_round_trip_ms=self.max_time_round_trip_ms
            )
            result["time_sync"] = {
                "available": True,
                "offset_ms": synced.offset_ms,
                "round_trip_ms": synced.round_trip_ms,
            }
            successful += 1
        except (MexcError, OSError, TypeError, ValueError) as exc:
            errors["time_sync"] = _safe_error(exc)

        try:
            account = self.client.account()
            if not isinstance(account, Mapping):
                raise ValueError("MEXC account response is not an object")
            result["balances"] = _balances(account, self.display_assets)
            result["account"] = {
                "account_type": str(account.get("accountType") or "-")[:40],
                "can_trade": _optional_boolean(account.get("canTrade")),
                "can_deposit": _optional_boolean(account.get("canDeposit")),
                "can_withdraw": _optional_boolean(account.get("canWithdraw")),
                "permissions": _text_list(account.get("permissions")),
            }
            result["available"] = True
            successful += 1
        except (MexcError, InvalidOperation, OSError, TypeError, ValueError) as exc:
            errors["account"] = _safe_error(exc)

        try:
            symbol_payload = self.client.self_symbols()
            symbols = _self_symbols(symbol_payload)
            result["api_symbols"] = symbols
            result["symbol_allowed"] = self.symbol in symbols
            successful += 1
        except (MexcError, OSError, TypeError, ValueError) as exc:
            errors["api_symbols"] = _safe_error(exc)

        try:
            open_orders = self.client.open_orders(self.symbol)
            result["open_orders"] = _open_orders(open_orders, self.symbol)
            successful += 1
        except (MexcError, OSError, TypeError, ValueError) as exc:
            errors["open_orders"] = _safe_error(exc)

        result["connected"] = successful > 0
        result["errors"] = errors
        return result


class KeyringMexcStatus:
    """Lazily opens Secret Service outside the GUI thread and caches the client."""

    def __init__(
        self,
        *,
        profile: str,
        base_url: str,
        symbol: str = DISPLAY_SYMBOL,
        base_asset: str = "ARRR",
        quote_asset: str = "USDT",
        timeout: float = 5.0,
    ) -> None:
        self.profile = profile
        self.base_url = base_url
        self.timeout = timeout
        self.symbol = symbol.upper()
        self.base_asset = base_asset.upper()
        self.quote_asset = quote_asset.upper()
        self._lock = threading.RLock()
        self._delegate: MexcPrivateStatus | None = None

    def payload(self) -> dict[str, Any]:
        with self._lock:
            if self._delegate is None:
                try:
                    credentials = LinuxSecretService(
                        profile=self.profile
                    ).load_mexc()
                    self._delegate = MexcPrivateStatus(
                        MexcClient(
                            api_key=credentials.api_key,
                            api_secret=credentials.api_secret,
                            base_url=self.base_url,
                            trading_enabled=False,
                            transfers_enabled=False,
                            timeout=self.timeout,
                        ),
                        symbol=self.symbol,
                        base_asset=self.base_asset,
                        quote_asset=self.quote_asset,
                    )
                except (SecretServiceError, ValueError) as exc:
                    result = _empty_payload(
                        configured=True,
                        symbol=self.symbol,
                        display_assets=(self.base_asset, self.quote_asset),
                    )
                    result["reason"] = _safe_error(exc)
                    result["errors"] = {"keyring": _safe_error(exc)}
                    return result
            return self._delegate.payload()


def disabled_mexc_status() -> dict[str, Any]:
    result = _empty_payload(configured=False)
    result["reason"] = "monitor MEXC disattivato"
    return result


def failed_mexc_status(exc: Exception) -> dict[str, Any]:
    result = _empty_payload(configured=True)
    result["reason"] = "lettura MEXC non riuscita"
    result["errors"] = {"monitor": _safe_error(exc)}
    return result


def _empty_payload(
    *,
    configured: bool,
    symbol: str = DISPLAY_SYMBOL,
    display_assets: tuple[str, ...] = DISPLAY_ASSETS,
) -> dict[str, Any]:
    return {
        "configured": configured,
        "available": False,
        "connected": False,
        "reason": None,
        "observed_at_ms": None,
        "symbol": symbol,
        "base_asset": display_assets[0],
        "quote_asset": display_assets[1],
        "symbol_allowed": None,
        "api_symbols": [],
        "account": {
            "account_type": "-",
            "can_trade": None,
            "can_deposit": None,
            "can_withdraw": None,
            "permissions": [],
        },
        "balances": {
            asset: {"free": "0", "locked": "0", "total": "0", "present": False}
            for asset in display_assets
        },
        "open_orders": [],
        "market": {
            "available": False,
            "best_bid": None,
            "best_ask": None,
            "midpoint": None,
            "observed_at_ms": None,
        },
        "time_sync": {"available": False, "offset_ms": None, "round_trip_ms": None},
        "safety": {
            "live_trading_enabled": False,
            "live_transfers_enabled": False,
        },
        "errors": {},
    }


def _balances(
    account: Mapping[str, Any], assets: tuple[str, ...] = DISPLAY_ASSETS
) -> dict[str, dict[str, Any]]:
    raw = account.get("balances", [])
    if not isinstance(raw, list):
        raise ValueError("MEXC balances are not a list")
    selected = {
        asset: {"free": "0", "locked": "0", "total": "0", "present": False}
        for asset in assets
    }
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("MEXC balance entry is invalid")
        asset = str(item.get("asset", "")).upper()
        if asset not in selected:
            continue
        free = _decimal(item.get("free", "0"), "free balance")
        locked = _decimal(item.get("locked", "0"), "locked balance")
        if free < 0 or locked < 0:
            raise ValueError("MEXC balance cannot be negative")
        selected[asset] = {
            "free": format(free, "f"),
            "locked": format(locked, "f"),
            "total": format(free + locked, "f"),
            "present": True,
        }
    return selected


def _self_symbols(payload: Mapping[str, Any]) -> list[str]:
    if not isinstance(payload, Mapping):
        raise ValueError("MEXC selfSymbols response is not an object")
    raw = payload.get("data")
    if not isinstance(raw, list):
        raise ValueError("MEXC selfSymbols data is not a list")
    return sorted({str(item).upper() for item in raw if str(item).strip()})


def _open_orders(payload: Any, expected_symbol: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("MEXC open orders response is not a list")
    result: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("MEXC open order entry is invalid")
        symbol = str(item.get("symbol", "")).upper()
        if symbol and symbol != expected_symbol:
            raise ValueError("MEXC returned an order for an unexpected symbol")
        result.append(
            {
                "symbol": symbol or expected_symbol,
                "side": str(item.get("side", "-"))[:12],
                "type": str(item.get("type", "-"))[:24],
                "price": _display_decimal(item.get("price", "0")),
                "original_quantity": _display_decimal(
                    item.get("origQty", item.get("origOty", "0"))
                ),
                "executed_quantity": _display_decimal(
                    item.get("executedQty", "0")
                ),
                "status": str(item.get("status", "-"))[:24],
                "client_order_id": str(item.get("clientOrderId", "-"))[:80],
                "updated_at_ms": _optional_integer(item.get("updateTime")),
            }
        )
    return result


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid MEXC {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid MEXC {field}")
    return parsed


def _display_decimal(value: Any) -> str:
    return format(_decimal(value, "order number"), "f")


def _optional_boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:80] for item in value]


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:300]
