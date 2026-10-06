from __future__ import annotations

from .inventory_reservations import reserved_pool_volume

import logging
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .cancellation_recovery import CancellationRecovery, AUTOMATIC_SOURCES
from .coin_registry import CoinRegistry
from .activation_failover import EvmActivationFailover
from .coverage import CoverageError, CoverageGuard
from .kdf import KdfPreflightError, KdfRpcClient
from .publication_recovery import PublicationRecovery
from .rebalance_guard import rebalance_guard
from .market_data import MarketDataStore, StaleMarketData
from .markets import MarketSpec
from .models import DexSide, HedgeSide, QuoteLimits, QuotePlan, QuotePolicy
from .ownership import (
    OrderOwnershipStore,
    OwnedOrder,
    OwnedOrderStatus,
    OwnedSwapState,
)
from .pricing import (
    build_quote_plan,
    quantity_within_slippage,
    synthetic_arrr_quote_book,
)


class QuoteBelowMinimum(ValueError):
    pass


LOG = logging.getLogger(__name__)


class HedgeDepthError(ValueError):
    """Order-specific liquidity failure, not a global balance failure."""


class KdfCoinStateUnavailable(RuntimeError):
    def __init__(self, age):
        self.snapshot_age = age
        super().__init__('KDF coin activation state unavailable; waiting for a verified snapshot')


class ActiveOrderLimitError(RuntimeError):
    pass


class InventoryLimitError(ValueError):
    pass


class VpsController:
    def __init__(
        self,
        *,
        kdf: KdfRpcClient,
        market_data: MarketDataStore,
        ownership: OrderOwnershipStore,
        kdf_quote_ticker: str,
        premium: Decimal,
        cex_taker_fee: Decimal,
        risk_buffer: Decimal,
        max_slippage: Decimal,
        max_daily_volume_fraction: Decimal,
        coin_registry: CoinRegistry | None = None,
        markets: tuple[MarketSpec, ...] | None = None,
        market_data_by_symbol: Mapping[str, MarketDataStore] | None = None,
        base_ticker: str = "ARRR",
        mexc_base_asset: str | None = None,
        mexc_quote_asset: str = "USDT",
        coverage: CoverageGuard | None = None,
    ) -> None:
        self.kdf = kdf
        self.evm_activation = EvmActivationFailover(kdf)
        self.market_data = market_data
        self.ownership = ownership
        self.kdf_quote_ticker = kdf_quote_ticker
        self.premium = premium
        self.cex_taker_fee = cex_taker_fee
        self.risk_buffer = risk_buffer
        self.policy = QuotePolicy(premium, cex_taker_fee, risk_buffer)
        self.max_slippage = max_slippage
        self.max_daily_volume_fraction = max_daily_volume_fraction
        self.coin_registry = coin_registry
        self.base_ticker = base_ticker.strip().upper()
        if not self.base_ticker:
            raise ValueError("base ticker is required")
        self.mexc_base_asset = (
            mexc_base_asset or self.base_ticker.split("-", 1)[0]
        ).strip().upper()
        self.mexc_quote_asset = mexc_quote_asset.strip().upper()
        if not self.mexc_base_asset or not self.mexc_quote_asset:
            raise ValueError("MEXC coverage assets are required")
        self.coverage = coverage
        default_spec = MarketSpec(
            market_id=f"{self.base_ticker}-{kdf_quote_ticker}",
            quote_ticker=kdf_quote_ticker,
            base_ticker=self.base_ticker,
            arrr_cex_symbol=market_data.symbol,
        )
        selected = markets or (default_spec,)
        if len({spec.market_id for spec in selected}) != len(selected):
            raise ValueError("market ids must be unique")
        if {spec.base_ticker for spec in selected} != {self.base_ticker}:
            raise ValueError("all markets must use the configured base ticker")
        self.markets = {spec.market_id: spec for spec in selected}
        self.default_market_id = selected[0].market_id
        stores = {market_data.symbol: market_data}
        if market_data_by_symbol is not None:
            stores.update({key.upper(): value for key, value in market_data_by_symbol.items()})
        missing = {
            symbol
            for spec in selected
            for symbol in spec.required_symbols
            if symbol not in stores
        }
        if missing:
            raise ValueError(f"missing market data stores: {', '.join(sorted(missing))}")
        self.market_data_by_symbol = stores
        self._order_lock = threading.RLock()
        self.strategy_coverage = None
        self.strategy_min_volume = None
        self.strategy_siblings = None
        self.rebalance_lock_path = None
        self.publications = PublicationRecovery(ownership)
        self.cancellations = CancellationRecovery(ownership)
        self._coin_lock = threading.RLock()
        self._coin_snapshot = None
        self._coin_observed = 0.0
        self._coin_error = None
        self._coin_stop = threading.Event()
        self._coin_thread = None

    def market_spec(self, market_id: str | None = None) -> MarketSpec:
        selected = market_id or self.default_market_id
        try:
            return self.markets[selected]
        except KeyError as exc:
            raise KeyError(f"unknown market {selected}") from exc

    def inventory_pool(self, market_id: str, dex_side: DexSide) -> str:
        return self.market_spec(market_id).inventory_pool(dex_side)

    def status(self) -> dict[str, Any]:
        detailed = self.all_market_statuses()["markets"]
        market_states = {
            market_id: {
                "state": item["state"],
                "age_ms": item.get("age_ms"),
                "sequence": item.get("sequence"),
                "active": item.get("active", True),
            }
            for market_id, item in detailed.items()
        }
        primary = market_states[self.default_market_id]
        return {
            "service": "kdf-mm-vps-agent",
            "mode": "ORDERS_ENABLED" if self.kdf.orders_enabled else "SIMULATION",
            "kdf_quote_ticker": self.kdf_quote_ticker,
            "base_ticker": self.base_ticker,
            "hedge_symbol": self.market_spec().base_cex_symbol,
            "default_market_id": self.default_market_id,
            "market_data": primary,
            "markets": market_states,
            "owned_open_orders": len(self.ownership.active()),
            "coverage": self.coverage_status(),
        }

    def coverage_requirements(
        self,
        *,
        extra_plan: QuotePlan | None = None,
        extra_plans: Iterable[QuotePlan] = (),
        excluding_order_uuid: str | None = None,
        check_existing_depth: bool = True,
    ) -> dict[str, Decimal]:
        """Worst-case CEX funds if every owned KDF order can reach its hedge.

        Deliberately sum sibling orders even when KDF assigns them the same
        inventory pool.  The funded race test permits two protocols to start
        before one aborts, so OCO is not yet a hard atomic reservation primitive
        on which to base a smaller CEX balance.
        """
        required: dict[str, Decimal] = {}
        for order in self.ownership.active():
            if order.order_uuid == excluding_order_uuid:
                continue
            reservation = (getattr(self, 'strategy_reservation', None)
                           if not check_existing_depth else None) or self.strategy_coverage
            custom = reservation(order) if reservation else None
            if custom is not None:
                for asset, amount in custom.items():
                    required[asset] = required.get(asset, Decimal(0)) + amount
                continue
            base_quantity = (
                order.advertised_volume
                if order.dex_side is DexSide.SELL_ARRR
                else order.advertised_volume * order.kdf_price
            )
            self._add_coverage_requirement(
                required,
                dex_side=order.dex_side,
                base_quantity=base_quantity,
                market_id=order.market_id,
            )
        plans = tuple(extra_plans)
        if extra_plan is not None:
            plans = (*plans, extra_plan)
        for plan in plans:
            custom = self.strategy_coverage(plan) if self.strategy_coverage else None
            if custom is not None:
                for asset, amount in custom.items():
                    required[asset] = required.get(asset, Decimal(0)) + amount
                continue
            self._add_coverage_requirement(
                required,
                dex_side=plan.dex_side,
                base_quantity=plan.base_quantity,
                market_id=plan.market_id,
                cex_limit_price=plan.cex_limit_price,
            )
        return required

    def assert_repricing_targets_coverage(
        self, plans: Iterable[QuotePlan]
    ) -> None:
        """Validate the complete quote set before an automatic recovery.

        Circuit-breaker cancellation removes the active orders, so ordinary
        coverage status would otherwise see an empty requirement.  Recovery
        must instead reserve MEXC funds for every quote it is about to restore.
        """
        if self.coverage is None:
            return
        selected = tuple(plans)
        if not selected:
            raise CoverageError("nessun target repricing da coprire")
        try:
            required = self.coverage_requirements(extra_plans=selected)
        except Exception as exc:
            if self.coverage.status({}).get("override_active"):
                return
            raise CoverageError(f"copertura MEXC non calcolabile: {exc}") from exc
        blocked = self.coverage.block_reason(required)
        if blocked is not None:
            raise CoverageError(blocked)

    def coverage_status(self, *, check_existing_depth: bool = True) -> dict[str, Any]:
        if self.coverage is None:
            return {
                "state": "DISABLED",
                "required": False,
                "blocked": False,
                "reason": None,
                "free_balances": {},
                "required_balances": {},
                "gaps": {},
                "override_active": False,
            }
        try:
            return self.coverage.status(self.coverage_requirements(check_existing_depth=check_existing_depth))
        except Exception as exc:
            status = self.coverage.status({})
            if status.get("override_active"):
                status["calculation_error"] = str(exc)
                return status
            status.update(
                {
                    "state": "BLOCKED" if self.coverage.required else "MONITOR_ONLY",
                    "blocked": bool(self.coverage.required),
                    "reason": f"copertura MEXC non calcolabile: {exc}",
                    "calculation_error": str(exc),
                }
            )
            return status

    def coverage_block_reason(self) -> str | None:
        if self.coverage is None:
            return None
        status = self.coverage_status()
        return str(status["reason"]) if status.get("blocked") else None

    def enforce_coverage(self) -> bool:
        """Cancel only bot-owned orders when the live coverage lease is unsafe."""
        with self._order_lock:
            return self._enforce_coverage_locked()

    def _enforce_coverage_locked(self) -> bool:
        retired = False
        calculation_error = None
        lease = self.coverage.status({}) if self.coverage else {}
        if (self.strategy_coverage and self.kdf.orders_enabled and
                self.coverage and self.coverage.required and
                not lease.get('blocked') and not lease.get('override_active')):
            # A known depth shortfall belongs to one order. Unknown failures,
            # stale balances and aggregate funding gaps still fail closed globally.
            for order in tuple(self.ownership.active()):
                try:
                    self.strategy_coverage(order)
                except HedgeDepthError as exc:
                    self.cancel_owned_order(order.order_uuid, reason=f"copertura ordine: {exc}", source='hedge_depth')
                    retired = True
                except Exception as exc:
                    calculation_error = f"copertura MEXC non calcolabile: {exc}"
                    break
        # Do not recheck all individual depths as one global condition: a book
        # may change between the selective pass and this funds-only check.
        status = self.coverage_status(check_existing_depth=False)
        blocked = calculation_error or (str(status['reason']) if status.get('blocked') else None)
        if blocked is None:
            return not retired
        if self.kdf.orders_enabled:
            for order in tuple(self.ownership.active()):
                self.cancel_owned_order(order.order_uuid, reason=f"coverage circuit breaker: {blocked}", source='coverage')
        return False

    def kdf_status(self) -> dict[str, Any]:
        try:
            return {"reachable": True, "enabled_coins": self.kdf.enabled_coins(),
                    "activations": self.evm_activation.snapshot()}
        except Exception as exc:
            return {"reachable": False, "error": str(exc),
                    "activations": self.evm_activation.snapshot()}

    def enabled_tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._enabled_tickers() or ()))

    def activation_profile_tickers(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Split enabled coins into replayable and unsupported profile entries."""
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        supported: list[str] = []
        skipped: list[str] = []
        for ticker in self.enabled_tickers():
            try:
                self.coin_registry.require_automatic_activation(ticker)
            except ValueError:
                skipped.append(ticker)
            else:
                supported.append(ticker)
        return tuple(supported), tuple(skipped)

    def wallet_status(
        self, *, extra_tickers: tuple[str, ...] | list[str] = ()
    ) -> dict[str, Any]:
        balances: dict[str, Any] = {}
        enabled = self._enabled_tickers()
        tickers = [self.base_ticker]
        for spec in self.markets.values():
            if spec.quote_ticker not in tickers:
                tickers.append(spec.quote_ticker)
            if self.coin_registry is not None:
                try:
                    dependencies = self.coin_registry.dependency_tickers(
                        spec.quote_ticker
                    )
                except Exception:
                    dependencies = ()
                for ticker in dependencies:
                    if ticker not in tickers:
                        tickers.append(ticker)
        for ticker in extra_tickers:
            selected = str(ticker).strip().upper()
            if selected and selected not in tickers:
                tickers.append(selected)
        for ticker in sorted(enabled or ()):
            if ticker not in tickers:
                tickers.append(ticker)
        for ticker in tickers:
            try:
                result = self.kdf.balance(ticker)
                balances[ticker] = {
                    "available": True,
                    "address": str(result.get("address", "")) if isinstance(result, Mapping) else "",
                    "balance": str(result.get("balance", "0")) if isinstance(result, Mapping) else "0",
                }
            except Exception as exc:
                balances[ticker] = {"available": False, "error": str(exc)}
        return {"balances": balances}

    def market_activity(self) -> dict[str, Any]:
        """Resolve price subscriptions from enabled coins and live bot state."""
        try:
            enabled = self._enabled_tickers()
            coin_state_available = True
        except KdfCoinStateUnavailable:
            # Preserve public subscriptions, not permission to publish. Trade
            # paths reject unknown activation state; exposure guards still run.
            with self._coin_lock:
                enabled = self._coin_snapshot or set()
            coin_state_available = False
        open_market_ids = {order.market_id for order in self.ownership.active()}
        swap_market_ids = {
            swap.market_id
            for swap in self.ownership.swaps(limit=10_000)
            if swap.state is OwnedSwapState.ACTIVE
        }
        live_market_ids = open_market_ids | swap_market_ids
        result: dict[str, Any] = {}
        for market_id, spec in self.markets.items():
            coins_enabled = (
                True
                if enabled is None
                else {spec.base_ticker, spec.quote_ticker}.issubset(enabled)
            )
            kept_for_live_trade = market_id in live_market_ids
            active = coins_enabled or kept_for_live_trade
            result[market_id] = {
                "active": active,
                "coins_enabled": coins_enabled if coin_state_available else None,
                "coin_state_available": coin_state_available,
                "kept_for_live_trade": kept_for_live_trade,
                "quote_ticker": spec.quote_ticker,
                "base_ticker": spec.base_ticker,
                "required_coins": [spec.base_ticker, spec.quote_ticker],
                "required_symbols": list(spec.required_symbols),
            }
        return {
            "enabled_coins_known": enabled is not None,
            "enabled_coins": sorted(enabled or ()),
            "markets": result,
        }

    def required_market_data_symbols(self) -> tuple[str, ...]:
        activity = self.market_activity()["markets"]
        return tuple(
            dict.fromkeys(
                symbol
                for market_id, spec in self.markets.items()
                if activity[market_id]["active"]
                for symbol in spec.required_symbols
            )
        )

    def inventory_status(self) -> dict[str, Any]:
        pools = tuple(
            dict.fromkeys(
                spec.inventory_pool(side)
                for spec in self.markets.values()
                for side in DexSide
            )
        )
        result: dict[str, Any] = {}
        for pool in pools:
            open_orders = self.ownership.active_for_pool(pool)
            advertised = sum(
                (order.advertised_volume for order in open_orders), start=Decimal("0")
            )
            item: dict[str, Any] = {
                "pool": pool,
                "open_order_count": len(open_orders),
                "advertised_kdf_volume": str(advertised),
                "order_uuids": [order.order_uuid for order in open_orders],
                "markets": sorted({order.market_id for order in open_orders}),
            }
            try:
                maximum = self.kdf.max_maker_volume(pool)
                max_volume = Decimal(_numeric_decimal(maximum, "volume"))
                item.update(
                    {
                        "available": True,
                        "max_maker_volume": str(max_volume),
                        "unadvertised_kdf_volume": str(
                            max(Decimal("0"), max_volume - advertised)
                        ),
                        "overbooked": advertised > max_volume,
                        "balance": _numeric_decimal(maximum, "balance"),
                        "locked_by_swaps": _numeric_decimal(
                            maximum, "locked_by_swaps"
                        ),
                    }
                )
            except Exception as exc:
                item.update({"available": False, "error": str(exc)})
            result[pool] = item
        return {"pools": result}

    def market_status(self, market_id: str | None = None) -> dict[str, Any]:
        spec = self.market_spec(market_id)
        primary = self.market_data_by_symbol[spec.arrr_cex_symbol].current()
        if spec.quote_cex_symbol is None:
            book = primary.order_book()
            sequences = [primary.sequence]
            ages = [self.market_data_by_symbol[spec.arrr_cex_symbol].age_ms()]
        else:
            quote = self.market_data_by_symbol[spec.quote_cex_symbol].current()
            book = synthetic_arrr_quote_book(
                arrr_usdt_book=primary.order_book(),
                quote_usdt_book=quote.order_book(),
            )
            sequences = [primary.sequence, quote.sequence]
            ages = [
                self.market_data_by_symbol[spec.arrr_cex_symbol].age_ms(),
                self.market_data_by_symbol[spec.quote_cex_symbol].age_ms(),
            ]
        best_bid = book.bids[0]
        best_ask = book.asks[0]
        midpoint = (best_bid.price + best_ask.price) / Decimal("2")
        spread = best_ask.price - best_bid.price
        spread_fraction = spread / midpoint if midpoint > 0 else Decimal("0")
        daily_cap = primary.base_volume_24h * self.max_daily_volume_fraction
        sell_arrr_depth = quantity_within_slippage(
            book.asks, side=HedgeSide.BUY, max_slippage=self.max_slippage
        )
        buy_arrr_depth = quantity_within_slippage(
            book.bids, side=HedgeSide.SELL, max_slippage=self.max_slippage
        )
        if spec.quote_cex_symbol is not None:
            quote = self.market_data_by_symbol[spec.quote_cex_symbol].current()
            quote_book = quote.order_book()
            sell_quote_depth = quantity_within_slippage(
                quote_book.bids,
                side=HedgeSide.SELL,
                max_slippage=self.max_slippage,
            )
            buy_quote_depth = quantity_within_slippage(
                quote_book.asks,
                side=HedgeSide.BUY,
                max_slippage=self.max_slippage,
            )
            sell_arrr_depth = min(sell_arrr_depth, sell_quote_depth / best_ask.price)
            buy_arrr_depth = min(buy_arrr_depth, buy_quote_depth / best_bid.price)
        return {
            "market_id": spec.market_id,
            "base_ticker": spec.base_ticker,
            "quote_ticker": spec.quote_ticker,
            "price_unit": f"{spec.quote_ticker}/{spec.base_ticker}",
            "symbol": "+".join(spec.required_symbols),
            "symbols": list(spec.required_symbols),
            "sequence": min(sequences),
            "sequences": sequences,
            "observed_at_ms": book.observed_at_ms,
            "age_ms": max((age for age in ages if age is not None), default=None),
            "best_bid": str(best_bid.price),
            "best_ask": str(best_ask.price),
            "midpoint": str(midpoint),
            "spread": str(spread),
            "spread_fraction": str(spread_fraction),
            "base_volume_24h": str(primary.base_volume_24h),
            "buy_capacity_arrr": str(primary.buy_capacity_arrr),
            "sell_capacity_arrr": str(primary.sell_capacity_arrr),
            "max_slippage": str(self.max_slippage),
            "daily_volume_cap_arrr": str(daily_cap),
            "suggested_sell_arrr_max": str(min(sell_arrr_depth, daily_cap)),
            "suggested_buy_arrr_max": str(min(buy_arrr_depth, daily_cap)),
            "suggested_sell_base_max": str(min(sell_arrr_depth, daily_cap)),
            "suggested_buy_base_max": str(min(buy_arrr_depth, daily_cap)),
            "quantity_step": str(primary.quantity_step),
            "price_step": str(primary.price_step),
            "min_quote_amount": str(primary.min_quote_amount),
            "cex_fee_legs": spec.cex_fee_legs,
        }

    def quote_usdt_valuation(
        self, market_id: str, dex_side: DexSide
    ) -> dict[str, Any]:
        """Return the executable historical quote/USDT rate for a signed event."""
        spec = self.market_spec(market_id)
        if spec.quote_cex_symbol is None:
            return {
                "quote_usdt_rate": "1",
                "quote_usdt_symbol": spec.arrr_cex_symbol,
                "quote_usdt_side": "DIRECT",
                "quote_usdt_observed_at_ms": self.market_data_by_symbol[
                    spec.arrr_cex_symbol
                ].current().observed_at_ms,
            }
        snapshot = self.market_data_by_symbol[spec.quote_cex_symbol].current()
        book = snapshot.order_book()
        if dex_side is DexSide.SELL_ARRR:
            rate = book.bids[0].price
            side = "BID"
        else:
            rate = book.asks[0].price
            side = "ASK"
        return {
            "quote_usdt_rate": str(rate),
            "quote_usdt_symbol": spec.quote_cex_symbol,
            "quote_usdt_side": side,
            "quote_usdt_observed_at_ms": snapshot.observed_at_ms,
        }

    def all_market_statuses(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        activity = self.market_activity()["markets"]
        for market_id in self.markets:
            if not activity[market_id]["active"]:
                result[market_id] = {
                    "state": "INACTIVE",
                    "reason": "coin_not_enabled_and_no_live_trade",
                    **activity[market_id],
                }
                continue
            try:
                result[market_id] = {
                    "state": "FRESH",
                    **activity[market_id],
                    **self.market_status(market_id),
                }
            except StaleMarketData as exc:
                result[market_id] = {
                    "state": "UNAVAILABLE",
                    **activity[market_id],
                    "error": str(exc),
                }
        return {"default_market_id": self.default_market_id, "markets": result}

    def ingest_market_snapshot(self, payload: Mapping[str, Any]):
        symbol = str(payload.get("symbol", "")).upper()
        try:
            store = self.market_data_by_symbol[symbol]
        except KeyError as exc:
            raise ValueError(f"unconfigured market-data symbol: {symbol}") from exc
        return store.ingest(payload)

    def activate_arrr(self) -> Any:
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        return self.kdf.enable_z_coin(
            ticker="ARRR", activation_params=self.coin_registry.arrr_activation_params()
        )

    def arrr_activation_status(self, task_id: int) -> Any:
        return self.kdf.enable_z_coin_status(task_id, forget_if_finished=False)

    def activate_quote_asset(self) -> Any:
        return self.activate_coin(self.kdf_quote_ticker)

    def deactivate_coin(self, ticker: str) -> Any:
        selected = ticker.strip().upper()
        if not selected:
            raise ValueError("provide one non-empty ticker")
        enabled = self._enabled_tickers()
        if enabled is not None and selected not in enabled:
            raise ValueError(f"{selected} is not active")
        return self.kdf.disable_coin(selected)

    def activate_coin(self, ticker: str) -> Any:
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        selected = ticker.strip().upper()
        protocol = self.coin_registry.require_automatic_activation(selected)
        if protocol == "ZHTLC":
            return self.kdf.enable_z_coin(
                ticker=selected,
                activation_params=self.coin_registry.zhtlc_activation_params(
                    selected
                ),
            )
        if protocol == "UTXO":
            return self.kdf.electrum(
                **self.coin_registry.utxo_activation_params(selected)
            )
        if protocol == "ETH":
            return self.evm_activation.start(
                self.coin_registry.evm_platform_activation_params(selected)
            )
        if protocol == "ERC20":
            dependencies = self.coin_registry.dependency_tickers(selected)
            enabled = self._enabled_tickers() or set()
            if dependencies and set(dependencies).issubset(enabled):
                return self.kdf.enable_erc20(
                    self.coin_registry.evm_token_only_activation_params(selected)
                )
            return self.evm_activation.start(
                self.coin_registry.evm_token_activation_params(selected)
            )
        raise RuntimeError(
            f"automatic activation for KDF protocol {protocol} is not implemented"
        )

    def activate_coins(self, tickers: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Activate an idempotent set, batching EVM tokens by platform."""
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        selected = tuple(
            dict.fromkeys(str(ticker).strip().upper() for ticker in tickers)
        )
        if not selected or any(not ticker for ticker in selected):
            raise ValueError("provide at least one non-empty ticker")

        protocols = {
            ticker: self.coin_registry.require_automatic_activation(ticker)
            for ticker in selected
        }
        enabled = self._enabled_tickers() or set()
        already_enabled = tuple(ticker for ticker in selected if ticker in enabled)
        pending = tuple(ticker for ticker in selected if ticker not in enabled)
        activations: list[dict[str, Any]] = []

        for ticker in pending:
            protocol = protocols[ticker]
            if protocol in {"ETH", "ERC20"}:
                continue
            response = self.activate_coin(ticker)
            activations.append(
                _activation_payload(
                    ticker=ticker,
                    tickers=(ticker,),
                    protocol=protocol,
                    response=response,
                )
            )

        evm_groups: dict[str, list[str]] = {}
        for ticker in pending:
            protocol = protocols[ticker]
            if protocol == "ETH":
                evm_groups.setdefault(ticker, [])
            elif protocol == "ERC20":
                dependencies = self.coin_registry.dependency_tickers(ticker)
                if len(dependencies) != 1:
                    raise ValueError(f"{ticker} has no single EVM platform")
                evm_groups.setdefault(dependencies[0], []).append(ticker)

        for platform, tokens in evm_groups.items():
            unique_tokens = tuple(dict.fromkeys(tokens))
            if platform in enabled:
                for token in unique_tokens:
                    response = self.kdf.enable_erc20(
                        self.coin_registry.evm_token_only_activation_params(token)
                    )
                    activations.append(
                        _activation_payload(
                            ticker=token,
                            tickers=(token,),
                            protocol="ERC20",
                            response=response,
                        )
                    )
                continue
            self.coin_registry.require_automatic_activation(platform)
            response = self.evm_activation.start(
                self.coin_registry.evm_platform_activation_params(
                    platform, token_tickers=unique_tokens
                )
            )
            activations.append(
                _activation_payload(
                    ticker=platform,
                    tickers=(platform, *unique_tokens),
                    protocol="ETH",
                    response=response,
                )
            )

        pending_tasks = [
            {"ticker": item["ticker"], "task_id": item["task_id"]}
            for item in activations
            if item.get("task_id") is not None
        ]
        if pending_tasks:
            state = "STARTED"
        elif activations:
            state = "COMPLETED"
        else:
            state = "ALREADY_ACTIVE"
        return {
            "state": state,
            "requested": list(selected),
            "already_enabled": list(already_enabled),
            "activations": activations,
            "pending_tasks": pending_tasks,
        }

    def coin_catalog(self, query: str = "", *, limit: int = 50) -> dict[str, Any]:
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        if limit <= 0 or limit > 200:
            raise ValueError("coin catalog limit must be in [1, 200]")
        needle = query.strip().upper()
        coins: list[dict[str, Any]] = []
        supported_total = 0
        matched_total = 0
        for ticker in self.coin_registry.tickers():
            try:
                protocol = self.coin_registry.require_automatic_activation(ticker)
            except ValueError:
                continue
            supported_total += 1
            if needle and needle not in ticker:
                continue
            matched_total += 1
            if len(coins) < limit:
                coins.append(
                    {
                        "ticker": ticker,
                        "protocol": protocol,
                        "dependencies": list(
                            self.coin_registry.dependency_tickers(ticker)
                        ),
                    }
                )
        return {
            "query": needle,
            "supported_total": supported_total,
            "matched_total": matched_total,
            "coins": coins,
            "truncated": matched_total > len(coins),
        }

    def quote_activation_status(self, task_id: int) -> Any:
        return self.activation_status(self.kdf_quote_ticker, task_id)

    def activation_status(self, ticker: str, task_id: int) -> Any:
        if self.coin_registry is None:
            raise RuntimeError("coin registry is not configured")
        protocol = self.coin_registry.protocol_type(ticker.strip().upper())
        if protocol == "ZHTLC":
            return self.kdf.enable_z_coin_status(task_id, forget_if_finished=False)
        if protocol in {"ETH", "ERC20"}:
            tracked = self.evm_activation.status(task_id)
            if tracked is not None:
                return tracked
            return self.kdf.enable_evm_with_tokens_status(
                task_id, forget_if_finished=False
            )
        raise ValueError(f"{ticker} activation is synchronous and has no task status")

    def preview_quote(
        self,
        *,
        dex_side: DexSide,
        requested_quantity: Decimal,
        kdf_available_quantity: Decimal,
        premium: Decimal | None = None,
        market_id: str | None = None,
    ) -> QuotePlan:
        spec = self.market_spec(market_id)
        primary = self.market_data_by_symbol[spec.arrr_cex_symbol].current()
        minimum_cex_notional = primary.min_quote_amount
        if spec.quote_cex_symbol is None:
            book = primary.order_book()
            cex_capacity = (
                primary.buy_capacity_arrr
                if dex_side is DexSide.SELL_ARRR
                else primary.sell_capacity_arrr
            )
        else:
            quote = self.market_data_by_symbol[spec.quote_cex_symbol].current()
            minimum_cex_notional = max(
                minimum_cex_notional, quote.min_quote_amount
            )
            book = synthetic_arrr_quote_book(
                arrr_usdt_book=primary.order_book(),
                quote_usdt_book=quote.order_book(),
            )
            quote_book = quote.order_book()
            if dex_side is DexSide.SELL_ARRR:
                quote_capacity = quantity_within_slippage(
                    quote_book.bids,
                    side=HedgeSide.SELL,
                    max_slippage=self.max_slippage,
                ) / book.asks[0].price
                cex_capacity = min(primary.buy_capacity_arrr, quote_capacity)
            else:
                quote_capacity = quantity_within_slippage(
                    quote_book.asks,
                    side=HedgeSide.BUY,
                    max_slippage=self.max_slippage,
                ) / book.bids[0].price
                cex_capacity = min(primary.sell_capacity_arrr, quote_capacity)
        plan = build_quote_plan(
            dex_side=dex_side,
            book=book,
            limits=QuoteLimits(
                user_quantity=requested_quantity,
                kdf_quantity=kdf_available_quantity,
                cex_balance_quantity=cex_capacity,
                daily_base_volume=primary.base_volume_24h,
                max_daily_volume_fraction=self.max_daily_volume_fraction,
                max_slippage=self.max_slippage,
            ),
            policy=QuotePolicy(
                self.premium if premium is None else premium,
                self.cex_taker_fee * spec.cex_fee_legs,
                self.risk_buffer,
            ),
            kdf_quote_ticker=spec.quote_ticker,
            arrr_quantity_step=primary.quantity_step,
            market_id=spec.market_id,
            quote_currency=spec.quote_ticker,
            inventory_pool=spec.inventory_pool(dex_side),
            base_ticker=spec.base_ticker,
        )
        base_usdt_value = primary.order_book().asks[0].price * plan.base_quantity
        if base_usdt_value < minimum_cex_notional:
            raise QuoteBelowMinimum("proposed MEXC hedge is below the current minimum")
        return plan

    def publish_quote(self, plan: QuotePlan, *, before_write=None) -> OwnedOrder:
        with self._order_lock, rebalance_guard(self.rebalance_lock_path):
            started = time.monotonic()
            siblings = self.active_orders_for_market_side(plan.market_id, plan.dex_side)
            permitted = bool(siblings and self.strategy_siblings and self.strategy_siblings(plan, siblings))
            if siblings and not permitted:
                raise ActiveOrderLimitError(
                    f"one owned order is already open for {plan.market_id} {plan.dex_side.value}"
                )
            self._assert_market_fresh(plan.market_id)
            self._assert_pool_capacity(plan)
            self._assert_coverage(plan)
            minimum = self.strategy_min_volume(plan) if self.strategy_min_volume else None
            if before_write is not None:
                before_write()
            intent_id = None
            def record_intent(before):
                nonlocal intent_id
                max_age = min(self.market_data_by_symbol[s].max_age_ms
                              for s in self.market_spec(plan.market_id).required_symbols)
                if (time.monotonic() - started) * 1000 >= max_age:
                    raise ValueError('preflight troppo lento: ricalcolare il piano prima di pubblicare')
                self._assert_market_fresh(plan.market_id)
                self._assert_coverage(plan)
                if plan.strategy_id:
                    intent_id = self.publications.begin(plan, before, minimum)
            result = self.kdf.set_price(
                base=plan.kdf_base,
                rel=plan.kdf_rel,
                price=plan.kdf_price,
                volume=plan.kdf_volume,
                **({"min_volume": minimum} if minimum is not None else {}),
                **({"allowed_existing_uuids": tuple(o.order_uuid for o in siblings)} if permitted else {}),
                **({'before_send': record_intent} if plan.strategy_id else {}),
            )
            if not isinstance(result, Mapping) or not result.get("uuid"):
                raise RuntimeError("KDF setprice response does not contain an order UUID")
            owned = self.ownership.register(
                str(result["uuid"]), plan, min_volume=minimum,
            )
            if intent_id:
                self.publications.state(intent_id, 'REGISTERED', order_uuid=owned.order_uuid)
            return owned

    def update_owned_quote(self, order_uuid: str, plan: QuotePlan, *, before_write=None) -> OwnedOrder:
        with self._order_lock, rebalance_guard(self.rebalance_lock_path):
            self._assert_market_fresh(plan.market_id)
            owned = self.ownership.get(order_uuid)
            if owned is None or owned.status is not OwnedOrderStatus.OPEN:
                raise KeyError(order_uuid)
            if (owned.market_id, owned.dex_side, owned.kdf_base, owned.kdf_rel) != (
                plan.market_id,
                plan.dex_side,
                plan.kdf_base,
                plan.kdf_rel,
            ):
                raise ValueError("updated quote changes the owned order market, pair or side")
            self._assert_pool_capacity(plan, excluding_order_uuid=order_uuid)
            self._assert_coverage(plan, excluding_order_uuid=order_uuid)
            minimum = self.strategy_min_volume(plan) if self.strategy_min_volume else None
            delta = plan.kdf_volume - owned.advertised_volume
            if before_write is not None:
                # KDF applies volume_delta to max_base_vol, whereas the
                # reconciler records available_amount (which can be lower).
                # Read the actual baseline before persisting the write intent.
                try:
                    observed = self.kdf._maker_order_snapshot(timeout=5.0).get(order_uuid)
                    if (not isinstance(observed, Mapping) or observed.get('base') != owned.kdf_base
                            or observed.get('rel') != owned.kdf_rel or observed.get('matches')
                            or observed.get('started_swaps')):
                        raise ValueError('ordine assente, cambiato o con swap in corso')
                    old_price = Decimal(str(observed['price']))
                    old_max = Decimal(str(observed['max_base_vol']))
                    if not old_price.is_finite() or old_price <= 0 or not old_max.is_finite() or old_max <= 0:
                        raise ValueError('termini ordine KDF non validi')
                except Exception as exc:
                    raise KdfPreflightError(f'lettura KDF prima dell’aggiornamento non riuscita: {exc}') from exc
                delta = plan.kdf_volume - old_max
                # Readback may spend several seconds waiting for KDF. Check
                # current exposure again before recording/sending a mutation.
                self._assert_market_fresh(plan.market_id)
                self._assert_coverage(plan, excluding_order_uuid=order_uuid)
                before_write(owned, minimum, old_price, old_max)
                if (old_price == plan.kdf_price and delta == 0
                        and (minimum is None or Decimal(str(observed['min_base_vol'])) == minimum)):
                    # available_amount may be smaller than max_base_vol. Do
                    # not keep resending a no-op update for that difference.
                    available = Decimal(str(observed['available_amount']))
                    if not available.is_finite() or available <= 0 or available > old_max:
                        raise KdfPreflightError('KDF available_amount non valido')
                    return self.ownership.update_quote(
                        order_uuid, kdf_price=old_price, kdf_volume=available,
                        kdf_max_volume=old_max,
                        kdf_min_volume=minimum,
                    )
            self.kdf.update_maker_order(
                order_uuid=order_uuid,
                new_price=plan.kdf_price,
                volume_delta=delta,
                **({"min_volume": minimum} if minimum is not None else {}),
            )
            return self.ownership.update_quote(
                order_uuid, kdf_price=plan.kdf_price, kdf_volume=plan.kdf_volume,
                kdf_max_volume=plan.kdf_volume,
                kdf_min_volume=minimum,
            )

    def cancel_owned_order(self, order_uuid: str, *, reason='Cancellazione richiesta dall’utente', source='manual', strategy_id='') -> OwnedOrder:
        with self._order_lock:
            owned = self.ownership.get(order_uuid)
            if owned is None or owned.status is not OwnedOrderStatus.OPEN:
                raise KeyError(order_uuid)
            if not reason or not source:
                raise ValueError('Motivo e origine della cancellazione obbligatori')
            strategy_id = strategy_id or getattr(self, 'order_strategy_id', lambda uuid: '')(order_uuid) or ''
            resume = (source in AUTOMATIC_SOURCES and bool(strategy_id)
                      and bool(getattr(self, 'cancellation_resume_requested', lambda _sid: False)(strategy_id)))
            self.cancellations.begin(order_uuid, strategy_id, source, reason, resume_requested=resume)
            if source == 'strategy_update_recovery':
                self.cancellations.state(order_uuid, 'DELEGATED')
            self.ownership.record_order_event(order_uuid, 'CANCEL_REQUESTED' , source=source,
                                             reason=reason, strategy_id=strategy_id)
            try:
                self.kdf.cancel_order(order_uuid)
            except Exception as exc:
                self.cancellations.failed(order_uuid, str(exc))
                self.ownership.record_order_event(order_uuid, 'CANCEL_FAILED_OR_UNCERTAIN', source=source,
                    reason=reason, strategy_id=strategy_id, detail=str(exc))
                raise
            cancelled = self.ownership.mark(order_uuid, OwnedOrderStatus.CANCELLED, error=reason,
                                            source=source, strategy_id=strategy_id)
            self.cancellations.confirmed(order_uuid)
            callback = getattr(self, "note_strategy_withdrawal", None)
            if callback is not None and strategy_id:
                try:
                    callback(strategy_id, source)
                except Exception:
                    # The KDF cancel is already confirmed. A cooldown audit
                    # failure must not turn it into an uncertain write.
                    LOG.exception("could not persist strategy safety cooldown")
            return cancelled

    def cancel_sibling_orders(
        self, matched: OwnedOrder, swap_uuid: str
    ) -> tuple[OwnedOrder, ...]:
        """Apply strict local OCO to every other order spending the same pool."""
        cancelled: list[OwnedOrder] = []
        with self._order_lock:
            for sibling in self.ownership.active_for_pool(matched.inventory_pool):
                if sibling.order_uuid == matched.order_uuid:
                    continue
                status = OwnedOrderStatus.CANCELLED
                try:
                    cancelled.append(self.cancel_owned_order(sibling.order_uuid,
                        reason=f"shared inventory claimed by swap {swap_uuid}", source='shared_inventory'))
                    continue
                except Exception:
                    # KDF can win this race itself by cancelling a sibling after
                    # its balance handler notices the first match.
                    status = _historical_order_state(
                        self.kdf.order_status(sibling.order_uuid)
                    )
                    if status is None:
                        raise
                cancelled.append(
                    self.ownership.mark(
                        sibling.order_uuid,
                        status,
                        error=f"shared inventory claimed by swap {swap_uuid}",
                        source='shared_inventory',
                    )
                )
        return tuple(cancelled)

    def active_orders_for_market_side(
        self, market_id: str, dex_side: DexSide
    ) -> tuple[OwnedOrder, ...]:
        return self.ownership.active_for_market_side(
            market_id or self.default_market_id, dex_side
        )

    def active_orders_for_side(self, dex_side: DexSide) -> tuple[OwnedOrder, ...]:
        return self.active_orders_for_market_side(self.default_market_id, dex_side)

    def cancel_all_owned(self, *, reason='Arresto del servizio', source='shutdown') -> tuple[OwnedOrder, ...]:
        with self._order_lock:
            cancelled: list[OwnedOrder] = []
            for order in self.ownership.active():
                try:
                    cancelled.append(self.cancel_owned_order(order.order_uuid, reason=reason, source=source))
                except Exception as exc:
                    self.ownership.mark(order.order_uuid, OwnedOrderStatus.OPEN, error=str(exc))
                    raise
            return tuple(cancelled)

    def enforce_fresh_market_data(self) -> bool:
        """Cancel only owned orders whose market reference has gone stale."""
        with self._order_lock:
            return self._enforce_fresh_market_data_locked()

    def _enforce_fresh_market_data_locked(self) -> bool:
        all_fresh = True
        activity = self.market_activity()["markets"]
        for market_id in self.markets:
            if not activity[market_id]["active"]:
                continue
            try:
                self._assert_market_fresh(market_id)
            except StaleMarketData as exc:
                all_fresh = False
                for order in tuple(self.ownership.active()):
                    if order.market_id == market_id:
                        self.cancel_owned_order(order.order_uuid, reason=f"market-data circuit breaker: {exc}", source='market_data')
        return all_fresh

    def refresh_coin_state(self):
        reader = getattr(self.kdf, 'enabled_coins', None)
        if reader is None:
            return
        try:
            payload = (reader(timeout=3.0) if isinstance(self.kdf, KdfRpcClient) else reader())
            if (not isinstance(payload, Mapping) or not isinstance(payload.get('coins'), list)
                    or any(not isinstance(item, Mapping) or not isinstance(item.get('ticker'), str)
                           or not item['ticker'] for item in payload['coins'])):
                raise ValueError('invalid enabled coin response')
            snapshot = frozenset(item['ticker'] for item in payload['coins'])
            with self._coin_lock:
                self._coin_snapshot = snapshot
                self._coin_observed = time.monotonic()
                self._coin_error = None
        except Exception as exc:
            with self._coin_lock:
                self._coin_error = type(exc).__name__
            LOG.warning('KDF_COIN_SNAPSHOT unavailable error_class=%s', type(exc).__name__)

    def start_coin_state_worker(self):
        if self._coin_thread and self._coin_thread.is_alive():
            return
        self.refresh_coin_state()  # Initial read before any trading/order lock.
        self._coin_stop.clear()
        def refresh():
            while not self._coin_stop.wait(2.0):
                self.refresh_coin_state()
        self._coin_thread = threading.Thread(target=refresh, name='kdf-coin-snapshot', daemon=True)
        self._coin_thread.start()

    def stop_coin_state_worker(self):
        self._coin_stop.set()
        if self._coin_thread:
            self._coin_thread.join(timeout=4.0)

    def _enabled_tickers(self) -> set[str] | None:
        if getattr(self.kdf, 'enabled_coins', None) is None:
            return None  # Offline adapters predate activation-aware feeds.
        if self._coin_thread is None:
            self.refresh_coin_state()  # Synchronous external/test adapter mode.
        with self._coin_lock:
            age = time.monotonic() - self._coin_observed if self._coin_snapshot is not None else float('inf')
            if self._coin_error or age > 8.0:
                raise KdfCoinStateUnavailable(age)
            return set(self._coin_snapshot)

    def _assert_market_fresh(self, market_id: str) -> None:
        for symbol in self.market_spec(market_id).required_symbols:
            self.market_data_by_symbol[symbol].current()

    def _assert_pool_capacity(
        self,
        plan: QuotePlan,
        *,
        excluding_order_uuid: str | None = None,
    ) -> None:
        max_maker_volume = getattr(self.kdf, "max_maker_volume", None)
        if max_maker_volume is None:
            return
        maximum = Decimal(_numeric_decimal(max_maker_volume(plan.inventory_pool), "volume"))
        already_advertised = reserved_pool_volume(
            self.ownership.active_for_pool(plan.inventory_pool), plan.inventory_pool,
            excluding=(excluding_order_uuid,),
        )
        requested_total = already_advertised + plan.kdf_volume
        if requested_total > maximum:
            raise InventoryLimitError(
                f"pool {plan.inventory_pool} would advertise {requested_total}; "
                f"KDF max_maker_vol is {maximum} "
                f"({already_advertised} already advertised)"
            )

    def _assert_coverage(
        self,
        plan: QuotePlan,
        *,
        excluding_order_uuid: str | None = None,
    ) -> None:
        if self.coverage is None:
            return
        try:
            required = self.coverage_requirements(
                extra_plan=plan,
                excluding_order_uuid=excluding_order_uuid,
                check_existing_depth=False,
            )
        except Exception as exc:
            if self.coverage.status({}).get("override_active"):
                return
            raise CoverageError(f"copertura MEXC non calcolabile: {exc}") from exc
        blocked = self.coverage.block_reason(required)
        if blocked is not None:
            raise CoverageError(blocked)

    def _add_coverage_requirement(
        self,
        required: dict[str, Decimal],
        *,
        dex_side: DexSide,
        base_quantity: Decimal,
        market_id: str,
        cex_limit_price: Decimal | None = None,
    ) -> None:
        if base_quantity <= 0:
            raise ValueError("coverage base quantity must be positive")
        fee_multiplier = Decimal("1") + self.cex_taker_fee
        if dex_side is DexSide.SELL_ARRR:
            if cex_limit_price is None:
                spec = self.market_spec(market_id)
                book = self.market_data_by_symbol[spec.base_cex_symbol].current().order_book()
                if not book.asks:
                    raise ValueError("MEXC ask book is empty")
                if quantity_within_slippage(
                    book.asks,
                    side=HedgeSide.BUY,
                    max_slippage=self.max_slippage,
                ) < base_quantity:
                    raise ValueError("MEXC ask depth cannot cover active KDF orders")
                cex_limit_price = book.asks[0].price * (
                    Decimal("1") + self.max_slippage
                )
            asset = self.mexc_quote_asset
            amount = base_quantity * cex_limit_price * fee_multiplier
        else:
            if cex_limit_price is None:
                spec = self.market_spec(market_id)
                book = self.market_data_by_symbol[spec.base_cex_symbol].current().order_book()
                if quantity_within_slippage(
                    book.bids,
                    side=HedgeSide.SELL,
                    max_slippage=self.max_slippage,
                ) < base_quantity:
                    raise ValueError("MEXC bid depth cannot cover active KDF orders")
            asset = self.mexc_base_asset
            amount = base_quantity * fee_multiplier
        required[asset] = required.get(asset, Decimal("0")) + amount


def _activation_payload(
    *, ticker: str, tickers: tuple[str, ...], protocol: str, response: Any
) -> dict[str, Any]:
    task_id = (
        int(response["task_id"])
        if isinstance(response, Mapping) and response.get("task_id") is not None
        else None
    )
    return {
        "ticker": ticker,
        "tickers": list(tickers),
        "protocol": protocol,
        "state": "STARTED" if task_id is not None else "COMPLETED",
        "task_id": task_id,
        "result": response,
    }


def quote_plan_payload(plan: QuotePlan) -> dict[str, str]:
    raw = asdict(plan)
    result = {
        key: value.value if hasattr(value, "value") else str(value)
        for key, value in raw.items()
    }
    result["human_price_quote_per_arrr"] = str(plan.human_price_quote_per_arrr)
    result["human_price_quote_per_base"] = str(plan.human_price_quote_per_base)
    result["base_quantity"] = str(plan.base_quantity)
    return result


def _numeric_decimal(payload: Any, key: str) -> str:
    if not isinstance(payload, Mapping) or key not in payload:
        raise ValueError(f"KDF max_maker_vol omitted {key}")
    value = payload[key]
    if isinstance(value, Mapping):
        value = value.get("decimal")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"KDF max_maker_vol returned invalid {key}")
    return str(parsed)


def _historical_order_state(payload: Any) -> OwnedOrderStatus | None:
    if not isinstance(payload, Mapping):
        return None
    order = payload.get("order", payload)
    if not isinstance(order, Mapping):
        return None
    reason = order.get("cancellation_reason", payload.get("cancellation_reason"))
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
