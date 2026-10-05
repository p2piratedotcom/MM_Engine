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

    def __post_init__(self) -> None:
        object.__setattr__(self, "cex", normalize_cex(self.cex))
        if not self.auto_fraction.is_finite() or not ZERO < self.auto_fraction <= 1:
            raise ValueError("percentuale custom auto: maggiore di 0 e al massimo 100%")
        if not self.strategy_id or len(self.strategy_id) > 100:
            raise ValueError("identificativo strategia richiesto (max 100 caratteri)")
        if self.base.asset == "USDT" or self.base.asset == self.quote.asset:
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
            "plan": {k: str(v) for k, v in asdict(self.plan).items()},
            "caps_sold": {k: str(v) for k, v in self.caps_sold.items()},
            "hedge_legs": list(self.hedge_legs),
            "evidence": self.evidence,
            "valuations_usdt": {k: str(v) for k, v in self.valuations_usdt.items()},
            "reference_type": "BEST_EXECUTABLE_BID_ASK",
            "quantity_policy": self.quantity_policy,
            "price_unit": f"{self.plan.quote_currency}/{self.plan.base_ticker}",
            "quantity_unit": self.plan.kdf_base,
            "notice": "Limite stimato dallo snapshot, non garanzia di liquidità futura. USDT non equivale necessariamente a USD.",
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
) -> StrategyPreview | dict[str, Any]:
    """Size both hedge legs in their actual units, with 50% book headroom.

    A fixed quantity is all-or-nothing. No hidden shrinking. All balances must
    be NET of other strategies, active swaps and gas reserves by the caller.
    """
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
              'limits': {key: str(value) for key, value in caps.items()},
              'snapshot_ms': min(s.observed_at_ms for s in snapshots.values())}
    if diagnostics_only:
        return report
    if not funding_target and (desired is None or desired <= 0 or desired > allowed):
        title = "quantità fissa non coperta" if spec.quantity_mode == "fixed" else "nessuna quantità coperta disponibile"
        reasons = []
        for key, cap in caps.items():
            if cap != allowed:
                continue
            if key.startswith("MEXC_USDT_per_"):
                reasons.append(f"USDT Spot {spec.cex} netti disponibili: {cex_free.get('USDT', ZERO)} (necessari per comprare {spec.sold.asset})")
            elif key.startswith("MEXC_"):
                asset = key.removeprefix("MEXC_")
                reasons.append(f"{asset} Spot {spec.cex} netti disponibili: {cex_free.get(asset, ZERO)} (necessari per vendere {asset} nella copertura)")
            elif key == "KDF_disponibile":
                reasons.append(f"saldo KDF vendibile residuo: {kdf_free} {spec.sold.ticker}; i livelli della stessa coppia si sommano, le coppie diverse condividono i fondi KDF")
            else:
                reasons.append(f"{limit_description(key)}: {cap} {spec.sold.ticker}")
        suggestion = (f"Il passo di quantità {spec.cex} impedisce un hedge con residuo entro 0.01 USDT "
                      "alla quantità disponibile; attendere più capacità o scegliere una route più precisa."
                      if precision_maximum < minimum <= raw_maximum else
                      f"Puoi provare al massimo {maximum} {spec.sold.ticker} con questo snapshot."
                      if maximum > 0 and maximum >= minimum else
                      f"Minimo copribile {spec.cex}: {minimum} {spec.sold.ticker}, superiore al massimo {maximum}. "
                      "Nessuna quantità pubblicabile: ridurre l'importo non basta; attendere più liquidità o liberare/rifornire la risorsa limitante.")
        raise ValueError(f"{spec.sold.ticker} → {spec.bought.ticker}: {title}. "
                         + "; ".join(reasons) + f". Massimo coperto: {maximum} {spec.sold.ticker}. "
                         + suggestion + ' ' + funds_report(shortages)
                         + f" I saldi {spec.cex} sono al netto della copertura degli altri ordini.")
    # Finite decimal KDF volume; sub-step hedge residuals are explicit and never
    # silently declared covered. The executor must account for every remainder.
    quantity = desired.quantize(D(".00000001"), rounding=ROUND_DOWN)
    if quantity <= 0 or (spec.quantity_mode == "fixed" and quantity != desired):
        raise ValueError("quantità non rappresentabile (precisione massima 8 decimali)")
    if precision_safe_sold_quantity(quantity, legs, books) != quantity:
        raise ValueError(
            f"{spec.sold.ticker} → {spec.bought.ticker}: quantità non copribile con "
            f"residuo di precisione {spec.cex} entro 0.01 USDT. "
            "Ridurre la quantità o usare la modalità automatica."
        )
    payload_legs = []
    for route, side, units, boundary, snap in legs:
        exact = quantity * units
        book = books[route.symbol]
        walk = walk_book(book.asks if side is HedgeSide.BUY else book.bids, exact)
        if not funding_target and (not walk.complete or quantity < minimum):
            raise ValueError(f"{spec.sold.ticker} → {spec.bought.ticker}: richiesti {quantity} {spec.sold.ticker}; "
                             f"minimo copribile {spec.cex} {minimum}, massimo {maximum} {spec.sold.ticker}. "
                             + (f"Importo sotto il minimo dell'hedge {spec.cex}, non necessariamente fondi mancanti. "
                                "Aumentare la quantità (o la percentuale custom auto) e gli eventuali tetti/budget "
                                "almeno al minimo indicato; poi ricontrollare la copertura residua."
                                if quantity < minimum else
                                "Il book non consente un hedge valido: attendere più liquidità o liberare la profondità impegnata dagli altri ordini.")
                             + ' ' + funds_report(shortages))
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


def funds_report(rows):
    return ' '.join(f"MEXC {r['asset']}: necessari {r['required']}, disponibili netti {r['available']}, mancanti {r['missing']}." for r in rows)


def limit_description(key):
    if key.endswith('_profondità_50%'):
        side, asset, _ = key.split('_', 2)
        return f"Profondità residua MEXC per {'comprare' if side == 'BUY' else 'vendere'} {asset} (50% entro l'impatto consentito)"
    return {'percentuale_auto': 'Quantità scelta con la percentuale custom auto',
            'KDF_disponibile': 'Saldo KDF residuo', 'limite_utente': 'Massimo impostato',
            'budget_residuo': 'Budget residuo', 'limite_24h_residuo': 'Limite giornaliero residuo'}.get(key, key)
