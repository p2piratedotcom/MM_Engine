"""Coin-neutral strategy definitions and conservative, side-aware sizing.

All quantities in a strategy are expressed in the coin SOLD on KDF. Prices
are quote/base; USDT is a valuation currency, not an assumed USD peg.
This module is pure: a preview never sends an order or reserves money.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from .market_data import MarketSnapshot
from .models import DexSide, HedgeSide, QuotePlan
from .pricing import walk_book
from .venues import normalize_cex

D = Decimal
ZERO = D("0")
ONE = D("1")
MAX_HEDGE_DUST_USDT = D(".01")
AUTO_REENTRY_MIN_FACTOR = D("1.25")


def precision_safe_sold_quantity(
    quantity: Decimal,
    legs: list[tuple["AssetRoute", HedgeSide, Decimal, Decimal, MarketSnapshot]],
    books: Mapping[str, Any],
) -> Decimal:
    """Reduce an automatic quote until every full-fill hedge fits MEXC steps.

    This does not make arbitrary partial fills exact. Markets with a coarse
    hedge step must additionally quote their whole volume as the minimum fill.
    """
    from decimal import ROUND_CEILING, ROUND_FLOOR

    quantum = D(".00000001")
    candidate = quantity.quantize(quantum, rounding=ROUND_FLOOR)
    for _ in range(256):
        if candidate <= 0:
            return ZERO
        reduced = candidate
        for route, side, units, _, snap in legs:
            exact = candidate * units
            step = snap.quantity_step
            rounded = (exact / step).to_integral_value(
                rounding=ROUND_CEILING if side is HedgeSide.BUY else ROUND_FLOOR
            ) * step
            best = books[route.symbol].asks[0].price if side is HedgeSide.BUY else books[route.symbol].bids[0].price
            if abs(rounded - exact) * best <= MAX_HEDGE_DUST_USDT:
                continue
            lower_exact = (exact / step).to_integral_value(rounding=ROUND_FLOOR) * step
            # A SELL leg must land at or just above its lower lot boundary;
            # flooring the KDF eight-decimal amount would land just below it
            # and leave almost one entire MEXC lot unhedged.
            lower_sold = (lower_exact / units).quantize(
                quantum, rounding=ROUND_FLOOR if side is HedgeSide.BUY else ROUND_CEILING
            )
            reduced = min(reduced, lower_sold)
        if reduced == candidate:
            return candidate
        candidate = reduced
    # Fail closed if two incommensurate CEX steps cannot be aligned promptly.
    return ZERO


def number(value: Any, name: str, *, positive: bool = True) -> Decimal:
    value = D(str(value))
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise ValueError(f"{name}: valore {'positivo' if positive else 'non negativo'} richiesto")
    return value


@dataclass(frozen=True)
class AssetRoute:
    ticker: str
    asset: str
    symbol: str | None

    def __post_init__(self) -> None:
        if not self.ticker or not self.asset or any(
            not c.isalnum() and c not in "-_" for c in self.ticker + self.asset
        ):
            raise ValueError("ticker/asset non valido")
        # KDF config IDs are case-sensitive (e.g. BTC-segwit). Exchange asset
        # codes are a separate namespace and still require uppercase.
        if self.asset != self.asset.upper():
            raise ValueError("asset CEX deve essere maiuscolo")
        if self.symbol != (None if self.asset == "USDT" else self.asset + "USDT"):
            raise ValueError("la route deve essere Spot asset/USDT; USDT non usa un book")

    @classmethod
    def parse(cls, ticker: str, asset: str | None = None) -> "AssetRoute":
        ticker = ticker.strip().upper()
        # Network suffixes are NOT guessed. An explicit mapping is required.
        if asset is None and "-" in ticker:
            raise ValueError(f"specificare l'asset Spot CEX per {ticker}")
        selected = (asset or ticker).strip().upper()
        return cls(ticker, selected, None if selected == "USDT" else selected + "USDT")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    base: AssetRoute
    quote: AssetRoute
    side: DexSide
    premium: Decimal
    price_mode: str
    fixed_price: Decimal | None
    quantity_mode: str
    max_sold: Decimal | None
    replenish: bool
    total_sold_budget: Decimal
    daily_sold_cap: Decimal | None
    fixed_sold: Decimal | None = None
    impact: Decimal = D("0.01")
    depth_fraction: Decimal = D("0.50")
    quantity_threshold: Decimal = D("0.10")
    price_threshold: Decimal = D("0.0025")
    update_seconds: Decimal = D("60")
    confirmations: int = 3
    scale_group: str = ""
    auto_fraction: Decimal = D("1")
    cex: str = "MEXC"
    hedging_enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.hedging_enabled) is not bool:
            raise ValueError("hedging_enabled must be a boolean")
        object.__setattr__(self, "cex", normalize_cex(self.cex))
        if not self.auto_fraction.is_finite() or not ZERO < self.auto_fraction <= 1:
            raise ValueError("percentuale custom auto: maggiore di 0 e al massimo 100%")
        if not self.strategy_id or len(self.strategy_id) > 100:
            raise ValueError("identificativo strategia richiesto (max 100 caratteri)")
        if (self.base.asset == "USDT" or self.base.asset == self.quote.asset) and self.requires_reference:
            raise ValueError("base non-USDT e asset distinti richiesti")
        if self.base.ticker == self.quote.ticker:
            raise ValueError("coin distinte richieste")
        if self.price_mode not in {"auto", "fixed"} or self.quantity_mode not in {"auto", "fixed"}:
            raise ValueError("modalità consentite: auto, fixed")
        if self.price_mode == "fixed":
            number(self.fixed_price, "prezzo fisso")
        if self.quantity_mode == "fixed":
            number(self.fixed_sold, "quantità fissa")
            if self.max_sold is not None and self.fixed_sold > self.max_sold:
                raise ValueError("quantità fissa superiore al massimo per ordine")
        for name in ("total_sold_budget", "update_seconds"):
            number(getattr(self, name), name)
        for name in ("max_sold", "daily_sold_cap"):
            if getattr(self, name) is not None:
                number(getattr(self, name), name)
        if type(self.replenish) is not bool:
            raise ValueError("replenish deve essere booleano")
        if not self.premium.is_finite() or abs(self.premium) >= 1:
            raise ValueError("premium deve essere compreso tra -100% e +100%")
        for name in ("impact", "depth_fraction", "quantity_threshold", "price_threshold"):
            value = number(getattr(self, name), name, positive=False)
            if value >= 1:
                raise ValueError(f"{name} deve essere minore di 1")
        if self.impact > D(".01") or not ZERO < self.depth_fraction <= D(".5"):
            raise ValueError("impatto massimo 1%; quota di profondità massima 50%")
        if self.confirmations < 3 or self.update_seconds < 1:
            raise ValueError("almeno 3 snapshot e intervallo positivo richiesti")

    @property
    def requires_reference(self):
        return self.hedging_enabled or self.price_mode == "auto"

    @property
    def market_id(self) -> str:
        return f"{self.base.ticker}-{self.quote.ticker}"

    @property
    def sold(self) -> AssetRoute:
        return self.base if self.side is DexSide.SELL_ARRR else self.quote

    @property
    def bought(self) -> AssetRoute:
        return self.quote if self.side is DexSide.SELL_ARRR else self.base

    def payload(self) -> dict[str, Any]:
        raw = asdict(self)
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in raw.items()}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StrategySpec":
        raw = dict(payload)
        raw.setdefault("cex", "MEXC")
        for key in ("base", "quote"):
            raw[key] = AssetRoute(**raw[key])
        raw["side"] = DexSide(raw["side"])
        for key in ("premium", "fixed_price", "max_sold", "total_sold_budget", "daily_sold_cap",
                    "fixed_sold", "impact", "depth_fraction", "quantity_threshold", "price_threshold", "update_seconds", "auto_fraction"):
            if key in raw and raw[key] is not None:
                raw[key] = D(str(raw[key]))
        return cls(**raw)


@dataclass(frozen=True)
class StrategyPreview:
    plan: QuotePlan
    caps_sold: Mapping[str, Decimal]
    hedge_legs: tuple[dict[str, str], ...]
    evidence: tuple[tuple[str, int], ...]
    valuations_usdt: Mapping[str, Decimal] = field(default_factory=dict)
    quantity_policy: str = "max auto"

    def payload(self) -> dict[str, Any]:
        return {
            "plan": {k: v if type(v) is bool else str(v) for k, v in asdict(self.plan).items()},
            "hedging_enabled": self.plan.hedging_enabled,
            "caps_sold": {k: str(v) for k, v in self.caps_sold.items()},
            "hedge_legs": list(self.hedge_legs),
            "evidence": self.evidence,
            "valuations_usdt": {k: str(v) for k, v in self.valuations_usdt.items()},
            "reference_type": ("FIXED_KDF_PRICE" if not self.plan.market_reference_required else
                               "CEX_PRICE_ONLY" if not self.plan.hedging_enabled else "BEST_EXECUTABLE_BID_ASK"),
            "quantity_policy": self.quantity_policy,
            "price_unit": f"{self.plan.quote_currency}/{self.plan.base_ticker}",
            "quantity_unit": self.plan.kdf_base,
            "notice": ("No hedging: swaps change wallet inventory without a compensating CEX trade." if not self.plan.hedging_enabled else
                       "Limite stimato dallo snapshot, non garanzia di liquidità futura. USDT non equivale necessariamente a USD."),
        }


def opposite_spec(spec: StrategySpec, preview: StrategyPreview, opposite_price: Decimal) -> StrategySpec:
    """Same canonical base quantity, never the same number of unlike coins."""
    sold = preview.plan.base_quantity * opposite_price if spec.side is DexSide.SELL_ARRR else preview.plan.base_quantity
    sold = sold.quantize(D(".00000001"), rounding=ROUND_DOWN)
    factor = sold / preview.plan.kdf_volume
    return replace(spec, strategy_id=spec.strategy_id + "-opposto",
                   side=DexSide.BUY_ARRR if spec.side is DexSide.SELL_ARRR else DexSide.SELL_ARRR,
                   premium=-spec.premium, fixed_price=opposite_price if spec.price_mode == "fixed" else None,
                   max_sold=(sold / spec.auto_fraction if spec.quantity_mode == 'auto' else sold)
                            if spec.max_sold is not None or spec.quantity_mode == "fixed" else None,
                   fixed_sold=sold if spec.quantity_mode == "fixed" else None,
                   total_sold_budget=(spec.total_sold_budget * factor).quantize(D(".00000001"), rounding=ROUND_DOWN),
                   daily_sold_cap=(spec.daily_sold_cap * factor).quantize(D(".00000001"), rounding=ROUND_DOWN) if spec.daily_sold_cap is not None else None)


def preview_strategy(
    spec: StrategySpec, snapshots: Mapping[str, MarketSnapshot], *,
    kdf_free: Decimal, cex_free: Mapping[str, Decimal],
    remaining_budget: Decimal | None = None, daily_remaining: Decimal | None = None,
    fee: Decimal = D(".001"), buffer: Decimal = D(".001"),
    daily_volume_fraction: Decimal = D(".01"),
    reserved_hedges: Mapping[tuple[str, str], Decimal] | None = None,
    diagnostics_only: bool = False,
    funding_target: bool = False,
    funding_context: Mapping[str, Mapping[str, Any]] | None = None,
) -> StrategyPreview | dict[str, Any]:
    """Size both hedge legs in their actual units, with 50% book headroom.

    A fixed quantity is all-or-nothing. No hidden shrinking. All balances must
    be NET of other strategies, active swaps and gas reserves by the caller.
    """
    if not spec.hedging_enabled:
        return preview_unhedged(spec, snapshots, kdf_free=kdf_free,
                                remaining_budget=remaining_budget, daily_remaining=daily_remaining,
                                diagnostics_only=diagnostics_only)
    for value, label in ((kdf_free, "saldo KDF"), (fee, "commissione"), (buffer, "buffer"),
                         (daily_volume_fraction, "frazione volume giornaliero")):
        number(value, label, positive=False)
    for value in cex_free.values():
        number(value, f"saldo {spec.cex}", positive=False)
    books = {r.symbol: snapshots[r.symbol].order_book() for r in (spec.base, spec.quote) if r.symbol}
    for book in books.values():
        if not book.bids or not book.asks or book.bids[0].price >= book.asks[0].price:
            raise ValueError(f"book {spec.cex} vuoto o incrociato")
    # Use executable sides, never last trade/24h average.
    base_book = books[spec.base.symbol]
    sell_base = spec.side is DexSide.SELL_ARRR
    bp = base_book.asks[0].price if sell_base else base_book.bids[0].price
    qp = ONE
    if spec.quote.symbol:
        qb = books[spec.quote.symbol]
        qp = qb.bids[0].price if sell_base else qb.asks[0].price
    reference = bp / qp
    legs_count = 1 + int(spec.quote.symbol is not None)
    edge = spec.premium + (fee * legs_count + buffer) * (1 if sell_base else -1)
    human_price = spec.fixed_price if spec.price_mode == "fixed" else reference * (ONE + edge)
    if human_price is None or human_price <= 0:
        raise ValueError("prezzo risultante non positivo")
    rate = human_price if sell_base else ONE / human_price  # bought per sold
    caps = {
        "limite_utente": spec.max_sold,
        "KDF_disponibile": kdf_free,
        "budget_residuo": remaining_budget if remaining_budget is not None else (None if spec.replenish else spec.total_sold_budget),
        "limite_24h_residuo": daily_remaining if daily_remaining is not None else spec.daily_sold_cap,
    }
    caps = {key: value for key, value in caps.items() if value is not None}
    legs = []
    depth_details = []
    for route, side, units in ((spec.sold, HedgeSide.BUY, ONE), (spec.bought, HedgeSide.SELL, rate)):
        if route.symbol is None:
            continue
        snap = snapshots[route.symbol]
        book = books[route.symbol]
        levels = book.asks if side is HedgeSide.BUY else book.bids
        boundary = levels[0].price * (ONE + spec.impact if side is HedgeSide.BUY else ONE - spec.impact)
        # Round inward: the actual limit must never exceed the 1% boundary.
        ticks = boundary / snap.price_step
        from decimal import ROUND_CEILING, ROUND_FLOOR
        boundary = ticks.to_integral_value(rounding=ROUND_FLOOR if side is HedgeSide.BUY else ROUND_CEILING) * snap.price_step
        depth = sum((level.quantity for level in levels if
                     (level.price <= boundary if side is HedgeSide.BUY else level.price >= boundary)), ZERO)
        reserved = (reserved_hedges or {}).get((route.symbol, side.value), ZERO)
        caps[f"{side.value}_{route.asset}_profondità_50%"] = max(ZERO, depth * spec.depth_fraction - reserved) / units
        depth_details.append({
            'asset': route.asset, 'side': side.value,
            'visible_within_impact': str(depth),
            'allowed_fraction': str(spec.depth_fraction),
            'allowed_before_reservations': str(depth * spec.depth_fraction),
            'reserved_for_other_hedges': str(reserved),
            'remaining': str(max(ZERO, depth * spec.depth_fraction - reserved)),
            'minimum_hedge_quantity_units': str(units),
        })
        caps[f"{route.asset}_volume_24h"] = snap.base_volume_24h * daily_volume_fraction / units
        if side is HedgeSide.BUY:
            caps[f"{spec.cex}_USDT_per_{route.asset}"] = max(ZERO,
                cex_free.get("USDT", ZERO) / (boundary * (ONE + fee)) - snap.quantity_step) / units
        else:
            caps[f"{spec.cex}_{route.asset}"] = cex_free.get(route.asset, ZERO) / ((ONE + fee) * units)
        legs.append((route, side, units, boundary, snap))
    allowed = min(caps.values())
    if spec.quantity_mode == 'auto' and spec.auto_fraction < 1:
        caps['percentuale_auto'] = allowed * spec.auto_fraction
        allowed = caps['percentuale_auto']
    raw_maximum = max(ZERO, allowed).quantize(D('.00000001'), rounding=ROUND_DOWN)
    precision_maximum = precision_safe_sold_quantity(raw_maximum, legs, books)
    maximum = raw_maximum
    if spec.quantity_mode == "auto":
        maximum = precision_maximum
    desired = spec.fixed_sold if spec.quantity_mode == "fixed" else maximum
    minimum = max(((snap.min_quote_amount / (books[route.symbol].bids[0].price * (ONE - spec.impact))
                    + snap.quantity_step) / units for route, _, units, _, snap in legs), default=ZERO)
    from decimal import ROUND_CEILING
    minimum = minimum.quantize(D('.00000001'), rounding=ROUND_CEILING)
    requested = spec.fixed_sold if spec.quantity_mode == 'fixed' else max(maximum, minimum)
    shortages = []
    for route, side, units, boundary, snap in legs:
        qty = (requested or ZERO) * units
        qty = (qty / snap.quantity_step).to_integral_value(rounding=ROUND_CEILING) * snap.quantity_step
        asset = 'USDT' if side is HedgeSide.BUY else route.asset
        required = qty * (boundary if side is HedgeSide.BUY else ONE) * (ONE + fee)
        available = cex_free.get(asset, ZERO)
        shortages.append({'asset': asset, 'required': str(required), 'available': str(available),
                          'missing': str(max(ZERO, required - available))})
    limiting = [key for key, cap in caps.items() if cap == allowed]
    if precision_maximum < raw_maximum:
        limiting.append(f"precisione_hedge_{spec.cex}")
    report = {'cex': spec.cex, 'maximum': str(maximum), 'minimum': str(minimum),
              'feasible_maximum': str(precision_maximum if precision_maximum >= minimum else ZERO),
              'coin': spec.sold.ticker, 'limiting': limiting, 'funds': shortages,
              'quantity_for_funds': str(requested),
              'funding_context': dict(funding_context or {}),
              'market_depth': depth_details,
              'limits': {key: str(value) for key, value in caps.items()},
              'snapshot_ms': min(s.observed_at_ms for s in snapshots.values())}
    if diagnostics_only:
        return report
    if (not funding_target and any(D(row['missing']) > ZERO for row in shortages)
            and (desired is None or desired <= 0 or desired > allowed or desired < minimum)):
        raise ValueError(funding_explanation(spec, shortages, funding_context or {},
                                            requested, desired, minimum, maximum, limiting))
    if not funding_target and (desired is None or desired <= 0 or desired > allowed):
        raise ValueError(capacity_explanation(spec, shortages, requested, desired,
                                             minimum, maximum, limiting, depth_details))
    # Finite decimal KDF volume; sub-step hedge residuals are explicit and never
    # silently declared covered. The executor must account for every remainder.
    quantity = desired.quantize(D(".00000001"), rounding=ROUND_DOWN)
    if quantity <= 0 or (spec.quantity_mode == "fixed" and quantity != desired):
        raise ValueError("Quantity cannot be represented with the maximum precision of 8 decimal places.")
    if precision_safe_sold_quantity(quantity, legs, books) != quantity:
        raise ValueError(
            f"{spec.sold.ticker} → {spec.bought.ticker}: the quantity cannot be hedged "
            f"within the 0.01 USDT rounding-residual limit on {spec.cex}. "
            "Reduce the quantity or use automatic sizing."
        )
    payload_legs = []
    for route, side, units, boundary, snap in legs:
        exact = quantity * units
        book = books[route.symbol]
        walk = walk_book(book.asks if side is HedgeSide.BUY else book.bids, exact)
        if not funding_target and quantity < minimum:
            raise ValueError(capacity_explanation(spec, shortages, requested, quantity,
                                                 minimum, maximum, limiting, depth_details))
        if not funding_target and not walk.complete:
            heading = f"Not enough market depth to complete the hedge on {spec.cex}"
            remedy = 'Wait for more market depth or release other confirmed hedge reservations, then repeat the preview.'
            raise ValueError(
                f"{heading}\n{spec.sold.ticker} → {spec.bought.ticker}\n"
                f"Requested quantity: {preview_amount(quantity)} {spec.sold.ticker}.\n"
                f"Minimum: {preview_amount(minimum)} {spec.sold.ticker}. Maximum now: {preview_amount(maximum)} {spec.sold.ticker}.\n"
                f"Funds check for {preview_amount(requested)} {spec.sold.ticker}:\n"
                + funds_report(shortages, spec.cex) + '\n' + remedy
            )
        payload_legs.append({"cex": spec.cex, "symbol": route.symbol, "asset": route.asset, "side": side.value,
                             "quantity": str(exact), "limit_price": str(boundary),
                             "quantity_step": str(snap.quantity_step), "snapshot_ms": str(snap.observed_at_ms)})
    base_quantity = quantity if sell_base else quantity / human_price
    primary_limit = next(D(leg["limit_price"]) for leg in payload_legs if leg["symbol"] == spec.base.symbol)
    plan = QuotePlan(spec.side, HedgeSide.BUY if sell_base else HedgeSide.SELL,
                     base_quantity, reference, primary_limit, human_price,
                     spec.sold.ticker, spec.bought.ticker, rate, quantity,
                     edge if spec.price_mode == "auto" else human_price / reference - ONE,
                     spec.market_id, spec.quote.ticker, spec.sold.ticker, spec.base.ticker,
                     spec.premium, fee * legs_count, buffer)
    evidence = tuple(sorted((s, snapshots[s].sequence) for s in books))
    def midpoint(route):
        if route.symbol is None:
            return ONE
        book = books[route.symbol]
        return (book.bids[0].price + book.asks[0].price) / 2
    valuations = {"prezzo_base": human_price * midpoint(spec.quote),
                  "totale_venduto": quantity * midpoint(spec.sold),
                  "totale_ricevuto": quantity * rate * midpoint(spec.bought)}
    policy = ('fixed' if spec.quantity_mode == 'fixed' else
              f'custom auto {(spec.auto_fraction * 100).normalize():f}%' if spec.auto_fraction < 1 else 'max auto')
    return StrategyPreview(plan, caps, tuple(payload_legs), evidence, valuations, policy)


def preview_amount(value):
    """Compact English display only; never reuse rounded values in a plan."""
    number = D(value)
    rounded = number.quantize(D('0.00000001'))
    text = format(rounded, 'f').rstrip('0').rstrip('.')
    return ('≈ ' if rounded != number else '') + (text or '0')


def capacity_explanation(spec, funds, evaluated, desired, minimum, maximum, limiting, depths):
    depth_keys = [key for key in limiting if key.endswith('_profondità_50%')]
    heading = (f"Not enough available market depth on {spec.cex}" if depth_keys else
               'The quantity does not meet the current maker limits')
    lines = [heading, f"{spec.sold.ticker} → {spec.bought.ticker}", '']
    for row in depths:
        key = f"{row['side']}_{row['asset']}_profondità_50%"
        if key not in depth_keys:
            continue
        asset = row['asset']
        verb = 'buy' if row['side'] == 'BUY' else 'sell'
        percent = preview_amount(D(row['allowed_fraction']) * D(100))
        impact = preview_amount(spec.impact * D(100))
        lines.extend([
            f"To hedge this maker, {asset} must be available to {verb} on {spec.cex}.",
            f"Book quantity within the allowed {impact}% price impact: {preview_amount(row['visible_within_impact'])} {asset}.",
            f"Usable under the {percent}% depth limit: {preview_amount(row['allowed_before_reservations'])} {asset}.",
            f"Reserved for other hedge obligations: {preview_amount(row['reserved_for_other_hedges'])} {asset}.",
            f"Remaining for this maker: {preview_amount(row['remaining'])} {asset}.",
            f"Minimum needed for this hedge leg: {preview_amount(minimum * D(row['minimum_hedge_quantity_units']))} {asset}.",
            '',
        ])
    if desired is not None:
        label = 'Quantity calculated in Auto mode' if spec.quantity_mode == 'auto' else 'Requested fixed quantity'
        lines.append(f"{label}: {preview_amount(desired)} {spec.sold.ticker}.")
    lines.extend([
        f"Minimum maker quantity: {preview_amount(minimum)} {spec.sold.ticker}.",
        f"Maximum allowed now: {preview_amount(maximum)} {spec.sold.ticker}.",
    ])
    if maximum < minimum:
        lines.append('No valid maker quantity is currently available: the maximum is below the minimum. Reducing the quantity will not help.')
    elif desired is not None and desired > maximum:
        lines.append(f"Requested quantity: {preview_amount(desired)} {spec.sold.ticker}. Reduce it to the allowed maximum or release the limiting capacity.")
    else:
        lines.append('The requested quantity must meet the hedge minimum and all current limits.')
    if limiting:
        lines.append('Limiting conditions: ' + ', '.join(funding_limit_label(key, spec.cex) for key in limiting) + '.')
    lines.extend(['', f"Funds check for {preview_amount(evaluated)} {spec.sold.ticker}:"])
    for row in funds:
        status = 'sufficient' if D(row['missing']) == ZERO else 'insufficient'
        lines.append(f"{row['asset']}: {status}; required {preview_amount(row['required'])}, net available {preview_amount(row['available'])}, missing {preview_amount(row['missing'])}.")
    if all(D(row['missing']) == ZERO for row in funds):
        lines.append('Funds are sufficient for this evaluated quantity. The blocker is a quantity or market-capacity limit, not missing CEX funds.')
        if depth_keys:
            lines.append('Adding funds alone does not increase the remaining market depth.')
    lines.extend(['', 'Possible solutions:'])
    if depth_keys:
        lines.extend([
            '• Wait for more market depth within the allowed price range, then retry.',
            '• Reduce or pause another maker using the same CEX market and hedge side. Capacity is released only after the change or withdrawal is confirmed; pending obligations may still reserve it.',
            '• Choose another supported CEX with enough market depth, available funds and the required hedge markets.',
        ])
    else:
        lines.append('• Check the limiting quantity setting, remaining wallet balance, budget, daily limit or custom auto percentage shown above, then retry.')
    lines.append('The existing price impact and depth safety limits remain unchanged. A new preview must confirm all requirements.')
    return '\n'.join(lines)


def funding_limit_label(key, venue):
    labels = {
        'percentuale_auto': 'custom auto percentage',
        'KDF_disponibile': 'remaining sellable KDF balance',
        'limite_utente': 'maximum quantity setting',
        'budget_residuo': 'remaining budget',
        'limite_24h_residuo': 'remaining daily limit',
    }
    if key in labels:
        return labels[key]
    if key.startswith(f'{venue}_USDT_per_'):
        return f"available USDT to buy {key.removeprefix(venue + '_USDT_per_')} on {venue}"
    if key.startswith(f'{venue}_'):
        return f"available {key[len(venue) + 1:]} on {venue}"
    if key.endswith('_profondità_50%'):
        side, asset, _ = key.split('_', 2)
        return f"remaining {asset} {'buy' if side == 'BUY' else 'sell'} book depth within the allowed impact"
    if key.endswith('_volume_24h'):
        return f"{key.removesuffix('_volume_24h')} rolling 24-hour volume limit"
    if key.startswith('precisione_hedge_'):
        return f"hedge quantity precision on {venue}"
    return 'another configured quantity limit'


def funding_explanation(spec, rows, context, evaluated, desired, minimum, maximum, limiting):
    """Presentation only: preserve exact amounts and all sizing/hold guards."""
    missing_assets = ', '.join(r['asset'] for r in rows if D(r['missing']) > ZERO)
    lines = [f"Insufficient {missing_assets} coverage on {spec.cex}",
             f"{spec.sold.ticker} → {spec.bought.ticker}"]
    for row in rows:
        asset = row['asset']
        details = context.get(asset)
        lines.append('')
        lines.append(f"{asset} on {spec.cex}")
        if details is not None:
            lines.append(f"Free exchange balance: {details['free_balance']} {asset}.")
            count = int(details['maker_count'])
            if count:
                noun = 'maker order' if count == 1 else 'maker orders'
                lines.append(f"{details['maker_reserved']} {asset} reserved to cover {count} existing {noun}.")
            else:
                lines.append(f"Reserved for existing maker orders: {details['maker_reserved']} {asset}.")
            if D(details['other_reserved']) > ZERO:
                lines.append(f"Other reservations (pending operations or unattributed obligations): {details['other_reserved']} {asset}.")
        lines.append(f"Available for this maker: {row['available']} {asset}.")
        lines.append(f"Required: {row['required']} {asset}. Missing: {row['missing']} {asset}.")
        if D(row['missing']) == ZERO:
            lines.append(f"{asset} coverage is sufficient for the evaluated quantity.")
    basis = 'minimum hedge quantity' if evaluated == minimum and desired != minimum else 'requested quantity'
    lines.extend(['', f"Funds above are calculated for the {basis}: {evaluated} {spec.sold.ticker}.",
                  f"Requested preview quantity: {desired} {spec.sold.ticker}.",
                  f"Minimum hedge quantity: {minimum} {spec.sold.ticker}.",
                  f"Maximum allowed by current funds and limits: {maximum} {spec.sold.ticker}."])
    if maximum < minimum:
        lines.append("No valid hedge quantity is currently available: the maximum is below the minimum.")
    if limiting:
        lines.append("Current limiting constraints: " + ', '.join(funding_limit_label(key, spec.cex) for key in limiting) + '.')
    lines.extend(['', 'Possible solutions:',
                  f"• Increase the available {missing_assets} balance on {spec.cex}.",
                  '• Choose another supported CEX with available funds and the required hedge markets.'])
    if any(D(d.get('maker_reserved', '0')) > ZERO for d in context.values()):
        lines.extend(['• Pause an existing maker, wait for its order withdrawal to be confirmed, then repeat the preview.',
                      'Pausing releases coverage only after confirmed withdrawal, provided no swap or pending obligation still needs it.'])
    if desired is not None and desired < minimum:
        lines.append('The quantity must also reach the hedge minimum; increasing it alone does not resolve missing funds. Check quantity caps, budgets and any custom auto percentage.')
    lines.append('Repeat the preview after making changes; funds, market depth and other limits will be checked again.')
    return '\n'.join(lines)


def funds_report(rows, venue='MEXC'):
    return '\n'.join(f"{venue} {r['asset']}: required {preview_amount(r['required'])}, net available {preview_amount(r['available'])}, missing {preview_amount(r['missing'])}." for r in rows)


def limit_description(key):
    if key.endswith('_profondità_50%'):
        side, asset, _ = key.split('_', 2)
        return f"Profondità residua MEXC per {'comprare' if side == 'BUY' else 'vendere'} {asset} (50% entro l'impatto consentito)"
    return {'percentuale_auto': 'Quantità scelta con la percentuale custom auto',
            'KDF_disponibile': 'Saldo KDF residuo', 'limite_utente': 'Massimo impostato',
            'budget_residuo': 'Budget residuo', 'limite_24h_residuo': 'Limite giornaliero residuo'}.get(key, key)



def preview_unhedged(spec, snapshots, *, kdf_free, remaining_budget=None,
                      daily_remaining=None, diagnostics_only=False):
    """DEX-only capacity. Public books are used solely for automatic pricing."""
    number(kdf_free, 'KDF spendable capacity', positive=False)
    sell_base = spec.side is DexSide.SELL_ARRR
    reference = spec.fixed_price
    if spec.price_mode == 'auto':
        books = {r.symbol: snapshots[r.symbol].order_book() for r in (spec.base, spec.quote) if r.symbol}
        if any(not x.bids or not x.asks or x.bids[0].price >= x.asks[0].price for x in books.values()):
            raise ValueError('Price reference is empty or crossed')
        base = books[spec.base.symbol]
        bp = base.asks[0].price if sell_base else base.bids[0].price
        qp = ONE
        if spec.quote.symbol:
            book = books[spec.quote.symbol]
            qp = book.bids[0].price if sell_base else book.asks[0].price
        reference = bp / qp
    price = reference * (ONE+spec.premium) if spec.price_mode == 'auto' else spec.fixed_price
    number(price, 'DEX price')
    rate = price if sell_base else ONE/price
    caps = {'KDF_disponibile': kdf_free,
            'limite_utente': spec.max_sold,
            'budget_residuo': remaining_budget if remaining_budget is not None else
                              (None if spec.replenish else spec.total_sold_budget),
            'limite_24h_residuo': daily_remaining if daily_remaining is not None else spec.daily_sold_cap}
    caps = {k:v for k,v in caps.items() if v is not None}
    maximum = min(caps.values())
    if spec.quantity_mode == 'auto': maximum *= spec.auto_fraction
    maximum = max(ZERO, maximum).quantize(D('.00000001'), rounding=ROUND_DOWN)
    quantity = spec.fixed_sold if spec.quantity_mode == 'fixed' else maximum
    if diagnostics_only:
        return {'cex':spec.cex,'hedging_enabled':False,'maximum':str(maximum),'minimum':'0',
                'feasible_maximum':str(maximum),'coin':spec.sold.ticker,'funds':[],
                'limiting':[k for k,v in caps.items() if v == min(caps.values())],
                'limits':{k:str(v) for k,v in caps.items()}}
    if quantity is None or quantity <= 0 or quantity > min(caps.values()):
        raise ValueError(f'Insufficient KDF funds or maker budget; maximum {maximum} {spec.sold.ticker}')
    if quantity != quantity.quantize(D('.00000001'), rounding=ROUND_DOWN):
        raise ValueError('Maker quantity must be representable with eight decimals')
    plan = QuotePlan(spec.side, HedgeSide.BUY if sell_base else HedgeSide.SELL,
        quantity if sell_base else quantity/price, reference, reference, price,
        spec.sold.ticker, spec.bought.ticker, rate, quantity,
        spec.premium if spec.price_mode == 'auto' else ZERO,
        spec.market_id, spec.quote.ticker, spec.sold.ticker, spec.base.ticker,
        spec.premium, ZERO, ZERO, hedging_enabled=False,
        market_reference_required=spec.price_mode == 'auto')
    evidence = tuple(sorted((key, snap.sequence) for key,snap in snapshots.items()))
    return StrategyPreview(plan,caps,(),evidence,{},
                           'fixed' if spec.quantity_mode == 'fixed' else 'auto from KDF funds')
