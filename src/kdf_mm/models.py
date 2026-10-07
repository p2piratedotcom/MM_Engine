from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Sequence


ZERO = Decimal("0")


class DexSide(StrEnum):
    """Azione rispetto all'asset base configurato.

    I valori conservano il nome storico ARRR per non invalidare database,
    eventi firmati e installazioni esistenti. Il loro significato applicativo
    e SELL_BASE/BUY_BASE.
    """

    SELL_ARRR = "SELL_ARRR"
    BUY_ARRR = "BUY_ARRR"


class HedgeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.price <= ZERO:
            raise ValueError("price must be positive")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")


def _parse_levels(rows: Iterable[Sequence[str]], *, reverse: bool) -> tuple[PriceLevel, ...]:
    levels = tuple(PriceLevel(Decimal(row[0]), Decimal(row[1])) for row in rows)
    return tuple(sorted(levels, key=lambda level: level.price, reverse=reverse))


@dataclass(frozen=True, slots=True)
class OrderBook:
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    observed_at_ms: int | None = None

    @classmethod
    def from_mexc(cls, payload: dict, *, observed_at_ms: int | None = None) -> "OrderBook":
        return cls(
            bids=_parse_levels(payload.get("bids", ()), reverse=True),
            asks=_parse_levels(payload.get("asks", ()), reverse=False),
            observed_at_ms=observed_at_ms,
        )


@dataclass(frozen=True, slots=True)
class LiquidityWalk:
    requested_quantity: Decimal
    filled_quantity: Decimal
    quote_amount: Decimal
    vwap: Decimal | None
    limit_price: Decimal | None

    @property
    def complete(self) -> bool:
        return self.filled_quantity == self.requested_quantity


@dataclass(frozen=True, slots=True)
class QuoteLimits:
    user_quantity: Decimal
    kdf_quantity: Decimal
    cex_balance_quantity: Decimal
    daily_base_volume: Decimal
    max_daily_volume_fraction: Decimal
    max_slippage: Decimal

    def __post_init__(self) -> None:
        non_negative = (
            self.user_quantity,
            self.kdf_quantity,
            self.cex_balance_quantity,
            self.daily_base_volume,
            self.max_daily_volume_fraction,
            self.max_slippage,
        )
        if any(value < ZERO for value in non_negative):
            raise ValueError("quote limits cannot be negative")
        if self.max_slippage >= Decimal("1"):
            raise ValueError("max_slippage must be below 1")


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    premium: Decimal
    cex_taker_fee: Decimal
    risk_buffer: Decimal

    def effective_edge_for(self, dex_side: DexSide) -> Decimal:
        """Return the signed offset applied to the executable CEX reference.

        ``premium`` is a signed price displacement: positive quotes above the
        reference and negative quotes below it.  Hedge costs protect the maker
        in the economically favourable direction for each KDF side.
        """
        costs = self.cex_taker_fee + self.risk_buffer
        if dex_side is DexSide.SELL_ARRR:
            return self.premium + costs
        return self.premium - costs


@dataclass(frozen=True, slots=True)
class QuotePlan:
    dex_side: DexSide
    hedge_side: HedgeSide
    arrr_quantity: Decimal
    reference_vwap: Decimal
    cex_limit_price: Decimal
    human_price_usdt_per_arrr: Decimal
    kdf_base: str
    kdf_rel: str
    kdf_price: Decimal
    kdf_volume: Decimal
    effective_edge: Decimal
    market_id: str = ""
    quote_currency: str = "USDT"
    inventory_pool: str = ""
    base_ticker: str = "ARRR"
    configured_premium: Decimal = ZERO
    cex_taker_fee: Decimal = ZERO
    risk_buffer: Decimal = ZERO
    strategy_id: str = ""
    hedging_enabled: bool = True
    market_reference_required: bool = True

    def __post_init__(self):
        if type(self.hedging_enabled) is not bool or type(self.market_reference_required) is not bool:
            raise ValueError('Maker protection flags must be boolean')
        if self.hedging_enabled and not self.market_reference_required:
            raise ValueError('Hedged makers require a fresh market reference')

    @property
    def base_quantity(self) -> Decimal:
        """Nome neutrale; ``arrr_quantity`` resta per compatibilita."""
        return self.arrr_quantity

    @property
    def human_price_quote_per_arrr(self) -> Decimal:
        """General name; the legacy field is retained for API compatibility."""
        return self.human_price_usdt_per_arrr

    @property
    def human_price_quote_per_base(self) -> Decimal:
        return self.human_price_usdt_per_arrr

    def as_kdf_setprice(self, *, min_volume: Decimal | None = None) -> dict[str, object]:
        request: dict[str, object] = {
            "base": self.kdf_base,
            "rel": self.kdf_rel,
            "price": str(self.kdf_price),
            "volume": str(self.kdf_volume),
        }
        if min_volume is not None:
            request["min_volume"] = str(min_volume)
        return request
