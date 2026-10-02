from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import DexSide


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Mapping between one KDF pair and its public MEXC references.

    The KDF ticker and the MEXC asset name are deliberately separate.  This
    keeps support for additional quote coins data-driven.
    """

    market_id: str
    quote_ticker: str
    base_ticker: str = "ARRR"
    arrr_cex_symbol: str = "ARRRUSDT"
    quote_cex_symbol: str | None = None
    cex_fee_legs: int = 1

    def __post_init__(self) -> None:
        if not self.market_id or not self.base_ticker or not self.quote_ticker or not self.arrr_cex_symbol:
            raise ValueError("market id, base, quote and CEX symbol are required")
        if self.market_id != f"{self.base_ticker}-{self.quote_ticker}":
            raise ValueError("market id must match its base and quote tickers")
        if self.cex_fee_legs not in (1, 2):
            raise ValueError("a market must use one or two CEX fee legs")
        if (self.quote_cex_symbol is None) != (self.cex_fee_legs == 1):
            raise ValueError("cross markets require a quote CEX symbol and two fee legs")

    @property
    def is_cross(self) -> bool:
        return self.quote_cex_symbol is not None

    @property
    def base_cex_symbol(self) -> str:
        """Neutral alias for the historical field name."""
        return self.arrr_cex_symbol

    @property
    def required_symbols(self) -> tuple[str, ...]:
        if self.quote_cex_symbol is None:
            return (self.arrr_cex_symbol,)
        return (self.arrr_cex_symbol, self.quote_cex_symbol)

    def pair_for(self, dex_side: DexSide) -> tuple[str, str]:
        if dex_side is DexSide.SELL_ARRR:
            return self.base_ticker, self.quote_ticker
        return self.quote_ticker, self.base_ticker

    def inventory_pool(self, dex_side: DexSide) -> str:
        # A maker spends the KDF base coin. Sell markets with the same strategy
        # base deliberately share one pool; buy markets use quote pools.
        return self.base_ticker if dex_side is DexSide.SELL_ARRR else self.quote_ticker


def default_market_specs(
    *,
    primary_symbol: str = "ARRRUSDT",
    primary_quote_ticker: str = "USDT-BEP20",
    base_ticker: str = "ARRR",
) -> tuple[MarketSpec, ...]:
    """Compatibility/example pair set; selection itself is fully generic."""
    return select_market_specs(
        (f"{base_ticker.upper()}-{primary_quote_ticker.upper()}", f"{base_ticker.upper()}-LTC"),
        primary_symbol=primary_symbol,
        primary_quote_ticker=primary_quote_ticker,
        base_ticker=base_ticker,
    )


def select_market_specs(
    market_ids: tuple[str, ...],
    *,
    primary_symbol: str,
    primary_quote_ticker: str,
    base_ticker: str = "ARRR",
    quote_cex_symbols: Mapping[str, str] | None = None,
) -> tuple[MarketSpec, ...]:
    """Build same-base market routes without a per-coin allow-list.

    The primary KDF quote uses ``primary_symbol`` directly. Every other plain
    ticker is valued through ``<TICKER>USDT`` on MEXC. Wrapped or differently
    named assets can provide an exact symbol in ``quote_cex_symbols``.
    """
    primary = primary_quote_ticker.strip().upper()
    base = base_ticker.strip().upper()
    base_symbol = primary_symbol.strip().upper()
    if not base:
        raise ValueError("base ticker is required")
    overrides = {
        str(ticker).strip().upper(): str(symbol).strip().upper()
        for ticker, symbol in (quote_cex_symbols or {}).items()
    }
    selected: list[MarketSpec] = []
    for market_id in market_ids:
        normalized = market_id.strip().upper()
        prefix = f"{base}-"
        if not normalized.startswith(prefix):
            raise ValueError(
                f"invalid market {market_id}: this strategy requires {base} as base"
            )
        quote_ticker = normalized[len(prefix) :]
        if not quote_ticker or quote_ticker == base:
            raise ValueError(f"invalid {base} market: {market_id}")
        quote_symbol = None
        fee_legs = 1
        if quote_ticker != primary:
            quote_symbol = overrides.get(quote_ticker, f"{quote_ticker}USDT")
            fee_legs = 2
        selected.append(
            MarketSpec(
                market_id=normalized,
                quote_ticker=quote_ticker,
                base_ticker=base,
                arrr_cex_symbol=base_symbol,
                quote_cex_symbol=quote_symbol,
                cex_fee_legs=fee_legs,
            )
        )
    if not selected:
        raise ValueError("configure at least one market")
    if len({spec.market_id for spec in selected}) != len(selected):
        raise ValueError("market ids must be unique")
    return tuple(selected)
