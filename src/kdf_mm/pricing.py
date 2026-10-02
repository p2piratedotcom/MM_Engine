from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Iterable

from .models import (
    ZERO,
    DexSide,
    HedgeSide,
    LiquidityWalk,
    OrderBook,
    PriceLevel,
    QuoteLimits,
    QuotePlan,
    QuotePolicy,
)


class InsufficientLiquidity(ValueError):
    pass


def floor_to_step(value: Decimal, step: Decimal | None) -> Decimal:
    if step is None:
        return value
    if step <= ZERO:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def walk_book(levels: Iterable[PriceLevel], quantity: Decimal) -> LiquidityWalk:
    if quantity <= ZERO:
        raise ValueError("quantity must be positive")

    remaining = quantity
    filled = ZERO
    quote_amount = ZERO
    limit_price: Decimal | None = None

    for level in levels:
        taken = min(level.quantity, remaining)
        if taken <= ZERO:
            continue
        filled += taken
        quote_amount += taken * level.price
        remaining -= taken
        limit_price = level.price
        if remaining == ZERO:
            break

    vwap = quote_amount / filled if filled > ZERO else None
    return LiquidityWalk(quantity, filled, quote_amount, vwap, limit_price)


def quantity_within_slippage(
    levels: Iterable[PriceLevel],
    *,
    side: HedgeSide,
    max_slippage: Decimal,
) -> Decimal:
    rows = tuple(levels)
    if not rows:
        return ZERO
    if max_slippage < ZERO or max_slippage >= Decimal("1"):
        raise ValueError("max_slippage must be in [0, 1)")

    best = rows[0].price
    if side is HedgeSide.BUY:
        boundary = best * (Decimal("1") + max_slippage)
        accepted = (level.quantity for level in rows if level.price <= boundary)
    else:
        boundary = best * (Decimal("1") - max_slippage)
        accepted = (level.quantity for level in rows if level.price >= boundary)
    return sum(accepted, start=ZERO)


def build_quote_plan(
    *,
    dex_side: DexSide,
    book: OrderBook,
    limits: QuoteLimits,
    policy: QuotePolicy,
    kdf_quote_ticker: str,
    arrr_quantity_step: Decimal | None = None,
    market_id: str = "",
    quote_currency: str = "USDT",
    inventory_pool: str = "",
    base_ticker: str = "ARRR",
) -> QuotePlan:
    if not kdf_quote_ticker:
        raise ValueError("kdf_quote_ticker is required")
    hedge_side = HedgeSide.BUY if dex_side is DexSide.SELL_ARRR else HedgeSide.SELL
    levels = book.asks if hedge_side is HedgeSide.BUY else book.bids
    depth_quantity = quantity_within_slippage(
        levels,
        side=hedge_side,
        max_slippage=limits.max_slippage,
    )
    daily_cap = limits.daily_base_volume * limits.max_daily_volume_fraction
    quantity = min(
        limits.user_quantity,
        limits.kdf_quantity,
        limits.cex_balance_quantity,
        daily_cap,
        depth_quantity,
    )
    quantity = floor_to_step(quantity, arrr_quantity_step)
    if quantity <= ZERO:
        raise InsufficientLiquidity(
            f"no hedgeable {base_ticker} quantity is available"
        )

    walk = walk_book(levels, quantity)
    if not walk.complete or walk.vwap is None or walk.limit_price is None:
        raise InsufficientLiquidity("the MEXC book cannot fill the proposed quantity")

    edge = policy.effective_edge_for(dex_side)
    multiplier = Decimal("1") + edge
    if multiplier <= ZERO:
        raise ValueError("effective quote offset must be greater than -1")
    if dex_side is DexSide.SELL_ARRR:
        human_price = walk.vwap * multiplier
        kdf_base = base_ticker
        kdf_rel = kdf_quote_ticker
        kdf_price = human_price
        kdf_volume = quantity
    else:
        human_price = walk.vwap * multiplier
        kdf_base = kdf_quote_ticker
        kdf_rel = base_ticker
        kdf_price = Decimal("1") / human_price
        kdf_volume = quantity * human_price

    return QuotePlan(
        dex_side=dex_side,
        hedge_side=hedge_side,
        arrr_quantity=quantity,
        reference_vwap=walk.vwap,
        cex_limit_price=walk.limit_price,
        human_price_usdt_per_arrr=human_price,
        kdf_base=kdf_base,
        kdf_rel=kdf_rel,
        kdf_price=kdf_price,
        kdf_volume=kdf_volume,
        effective_edge=edge,
        market_id=market_id,
        quote_currency=quote_currency,
        inventory_pool=inventory_pool or kdf_base,
        base_ticker=base_ticker,
        configured_premium=policy.premium,
        cex_taker_fee=policy.cex_taker_fee,
        risk_buffer=policy.risk_buffer,
    )


def synthetic_arrr_quote_book(
    *,
    arrr_usdt_book: OrderBook,
    quote_usdt_book: OrderBook,
) -> OrderBook:
    """Build an executable, conservative base/quote reference book.

    Selling the base on KDF is hedged at the base asks and values the received quote
    coin at its CEX bid. Buying the base uses the inverse path: base bids and quote
    asks. The depth cap is calculated separately by the controller.
    """
    if not quote_usdt_book.bids or not quote_usdt_book.asks:
        raise InsufficientLiquidity("the quote/USDT book is incomplete")
    quote_bid = quote_usdt_book.bids[0].price
    quote_ask = quote_usdt_book.asks[0].price
    observed = None
    timestamps = tuple(
        value
        for value in (
            arrr_usdt_book.observed_at_ms,
            quote_usdt_book.observed_at_ms,
        )
        if value is not None
    )
    if timestamps:
        observed = min(timestamps)
    return OrderBook(
        bids=tuple(
            PriceLevel(level.price / quote_ask, level.quantity)
            for level in arrr_usdt_book.bids
        ),
        asks=tuple(
            PriceLevel(level.price / quote_bid, level.quantity)
            for level in arrr_usdt_book.asks
        ),
        observed_at_ms=observed,
    )
