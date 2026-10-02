from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .desktop_status import DesktopJournalStatus
from .ledger import (
    EconomicLedgerStatus,
    disabled_economic_ledger,
    failed_economic_ledger,
)
from .mexc_status import (
    MexcStatusSource,
    disabled_mexc_status,
    failed_mexc_status,
)
from .portfolio import (
    PortfolioPolicy,
    build_portfolio_snapshot,
    failed_portfolio_snapshot,
)


REMOTE_ENDPOINTS = {
    "service": "/v1/status",
    "kdf": "/v1/kdf/status",
    "supervisor": "/v1/kdf/supervisor",
    "markets": "/v1/markets",
    "orders": "/v1/orders",
    "inventory": "/v1/inventory",
    "coverage": "/v1/coverage",
    "repricing": "/v1/repricing",
    "reconciliation": "/v1/reconciliation",
    "event_delivery": "/v1/events/status",
    "wallet": "/v1/wallet",
}


class ReadApi(Protocol):
    def get(self, path: str) -> Any: ...


class ReadOnlyAgentApi:
    """Small GET-only client used by the desktop dashboard."""

    def __init__(self, *, base_url: str, token: str, timeout: float = 15.0) -> None:
        if not token:
            raise ValueError("KDF_MM_AGENT_TOKEN is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path: str) -> Any:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Agent HTTP {exc.code}: {message}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("KDF Agent non raggiungibile") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Risposta KDF Agent non valida") from exc


class DesktopDashboardSource:
    """Combines independent, read-only VPS and Desktop journal snapshots."""

    def __init__(
        self,
        *,
        api: ReadApi | None,
        desktop_journal: str | Path | None,
        mexc: MexcStatusSource | None = None,
        portfolio_policy: PortfolioPolicy | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        self.api = api
        self.mexc = mexc
        self.portfolio_policy = portfolio_policy or PortfolioPolicy()
        self.desktop = (
            DesktopJournalStatus(desktop_journal)
            if desktop_journal is not None
            else None
        )
        self.ledger = (
            EconomicLedgerStatus(desktop_journal)
            if desktop_journal is not None
            else None
        )
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "collected_at_ms": int(self.clock_ms()),
            "offline": self.api is None,
            "connected": False,
            "remote_errors": {},
            **_empty_remote_sections(),
            "desktop": (
                self.desktop.payload()
                if self.desktop is not None
                else _desktop_not_configured()
            ),
            "mexc": disabled_mexc_status(),
            "portfolio": {},
            "ledger": disabled_economic_ledger(),
        }
        if self.api is None and self.mexc is None:
            self._add_portfolio(result)
            self._add_ledger(result)
            return result

        errors: dict[str, str] = {}
        successful = 0
        worker_count = len(REMOTE_ENDPOINTS) + (1 if self.mexc is not None else 0)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending = {}
            if self.api is not None:
                pending.update(
                    {
                        executor.submit(self.api.get, path): section
                        for section, path in REMOTE_ENDPOINTS.items()
                    }
                )
            if self.mexc is not None:
                pending[executor.submit(self.mexc.payload)] = "mexc"
            for future in as_completed(pending):
                section = pending[future]
                try:
                    payload = future.result()
                    if not isinstance(payload, dict):
                        raise RuntimeError("risposta non strutturata")
                    result[section] = payload
                    if section != "mexc":
                        successful += 1
                except Exception as exc:  # isolate every read endpoint
                    if section == "mexc":
                        result["mexc"] = failed_mexc_status(exc)
                    else:
                        errors[section] = _safe_error(exc)

        result["connected"] = successful > 0
        result["remote_errors"] = errors
        self._add_portfolio(result)
        self._add_ledger(result)
        return result

    def _add_portfolio(self, result: dict[str, Any]) -> None:
        try:
            service = result.get("service", {})
            mexc = result.get("mexc", {})
            base_ticker = str(service.get("base_ticker") or "ARRR")
            kdf_quote_ticker = str(
                service.get("kdf_quote_ticker") or "USDT-BEP20"
            )
            result["portfolio"] = build_portfolio_snapshot(
                wallet=result.get("wallet", {}),
                mexc=mexc,
                markets=result.get("markets", {}),
                policy=self.portfolio_policy,
                base_ticker=base_ticker,
                kdf_quote_ticker=kdf_quote_ticker,
                cex_base_asset=str(
                    mexc.get("base_asset") or base_ticker.split("-", 1)[0]
                ),
                cex_quote_asset=str(mexc.get("quote_asset") or "USDT"),
                primary_market_id=str(
                    service.get("default_market_id")
                    or f"{base_ticker}-{kdf_quote_ticker}"
                ),
                primary_symbol=str(
                    mexc.get("symbol")
                    or service.get("hedge_symbol")
                    or f"{base_ticker.split('-', 1)[0]}USDT"
                ),
            )
        except Exception as exc:
            result["portfolio"] = failed_portfolio_snapshot(
                exc, self.portfolio_policy
            )

    def _add_ledger(self, result: dict[str, Any]) -> None:
        if self.ledger is None:
            return
        try:
            market = result.get("mexc", {}).get("market", {})
            mark_price = market.get("midpoint") if isinstance(market, dict) else None
            portfolio = result.get("portfolio", {})
            if mark_price is None and isinstance(portfolio, dict):
                portfolio_price = portfolio.get("price", {})
                if isinstance(portfolio_price, dict):
                    mark_price = portfolio_price.get("midpoint")
            totals = portfolio.get("totals", {}) if isinstance(portfolio, dict) else {}
            current_arrr = totals.get("arrr") if isinstance(totals, dict) else None
            assets = portfolio.get("assets", {}) if isinstance(portfolio, dict) else {}
            ledger = self.ledger.payload(
                mark_price_usdt=mark_price,
                current_arrr_quantity=current_arrr,
                base_asset=str(assets.get("base_ticker") or "ARRR"),
            )
        except Exception as exc:
            ledger = failed_economic_ledger(exc)
        result["ledger"] = ledger
        portfolio = result.get("portfolio")
        if isinstance(portfolio, dict) and ledger.get("available") is True:
            summary = ledger.get("summary", {})
            inventory = ledger.get("inventory_pnl", {})
            portfolio["pnl"] = {
                "available": True,
                "realized_usdt": summary.get("net_realized_usdt"),
                "unrealized_usdt": summary.get("unrealized_usdt"),
                "estimated_total_usdt": summary.get("estimated_total_pnl_usdt"),
                "complete": bool(
                    summary.get("net_realized_complete")
                    and summary.get("unrealized_complete")
                ),
                "reason": ledger.get("notice"),
                "inventory_available": bool(inventory.get("available")),
                "inventory_unrealized_usdt": inventory.get("unrealized_usdt"),
                "inventory_reason": inventory.get("reason"),
            }


def _empty_remote_sections() -> dict[str, dict[str, Any]]:
    return {section: {} for section in REMOTE_ENDPOINTS}


def _desktop_not_configured() -> dict[str, Any]:
    return {
        "available": False,
        "reason": "monitor Desktop non configurato",
        "delivery": {},
        "hedges": {},
        "alarms": [],
        "recent": [],
    }


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:300]
