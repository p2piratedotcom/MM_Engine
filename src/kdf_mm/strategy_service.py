"""Local strategy orchestration. Does not own KDF or MEXC private keys."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping

from .market_data import MarketDataStore
from .kdf import KdfPreflightError
from .markets import MarketSpec
from .models import DexSide, HedgeSide
from .ownership import OwnedOrderStatus, OwnedSwapState
from .outbox import _swap_terms
from .pricing import quantity_within_slippage
from .public_feed import MexcPublicFeed
from .reconciliation import KdfReconciliationError
from .strategy import (
    AUTO_REENTRY_MIN_FACTOR,
    MAX_HEDGE_DUST_USDT,
    StrategySpec,
    preview_strategy,
    opposite_spec,
)
from .strategy_store import StrategyStore
from .vps_controller import _numeric_decimal, HedgeDepthError
from .venues import coverage_asset_key, market_data_key, normalize_cex

D = Decimal
LEGACY_UPDATE_TIMEOUT = 'KDF RPC update_maker_order: remote API is unreachable (timeout)'
POST_SWAP_UPDATE_HELD = 'ordine assente, ma swap/stato finale da riconciliare'
LOG = logging.getLogger(__name__)


class StrategyService:
    def __init__(self, *, controller, store: StrategyStore, feed_group, public_client=None,
                 public_clients=None,
                 venue_fees=None, preview_balances=None,
                 reconciliation, repricing, clock=time.time, settlement=None) -> None:
        self.controller, self.store, self.feeds = controller, store, feed_group
        self.preview_balances = preview_balances
        clients = dict(public_clients or {})
        # None remains a supported injected test/offline adapter when all
        # required snapshots are already present in the controller.
        if public_client is not None or not clients:
            clients.setdefault("MEXC", public_client)
        self.public_clients = {normalize_cex(k): v for k, v in clients.items()}
        self.public_client = self.public_clients.get("MEXC", next(iter(self.public_clients.values())))
        self.venue_fees = {normalize_cex(k): D(str(v))
                           for k, v in (venue_fees or {}).items()}
        self.reconciliation, self.repricing = reconciliation, repricing
        self.clock, self.settlement = clock, settlement
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self._recovery_checked = {}
        self._publication_books: dict[str, dict[str, object]] = {}
        self._specs: dict[str, StrategySpec] = {}
        for row in store.rows():
            spec = StrategySpec.from_payload(row["spec"])
            self._register(spec, validate=False)
            self._specs[spec.strategy_id] = spec
            if row["state"] == "WRITING":
                if store.update_intent(spec.strategy_id):
                    store.update(spec.strategy_id, state="RECOVERING",
                                 detail="Aggiornamento KDF incerto: verifica automatica in corso")
                else:
                    store.update(spec.strategy_id, enabled=0, state="REVIEW_REQUIRED",
                                 detail="Riavvio durante scrittura KDF: riconciliare gli ordini prima di proseguire")
        controller.strategy_coverage = self.coverage_requirement
        # Keep other orders' funds reserved without making their depth failure
        # a reason to cancel the unrelated order currently being repriced.
        controller.strategy_reservation = lambda item: self.coverage_requirement(item, check_depth=False)
        controller.order_strategy_id = self.store.strategy_for_order
        controller.strategy_min_volume = self.minimum_volume
        controller.strategy_siblings = self.allow_siblings
        controller.note_strategy_withdrawal = store.note_safety_withdrawal

    @staticmethod
    def _key(spec: StrategySpec, symbol: str) -> str:
        return market_data_key(spec.cex, symbol)

    def _client(self, spec: StrategySpec):
        try:
            return self.public_clients[spec.cex]
        except KeyError as exc:
            raise ValueError(f"feed pubblico {spec.cex} non configurato") from exc

    @staticmethod
    def _coverage_key(spec: StrategySpec, asset: str) -> str:
        return coverage_asset_key(spec.cex, asset)

    def _fee(self, spec: StrategySpec) -> Decimal:
        return self.venue_fees.get(spec.cex, self.controller.cex_taker_fee)

    def minimum_volume(
        self, plan, spec_override=None, *, for_new_auto=False,
        depth_reentry=False,
    ):
        spec = spec_override or self._for_quote(plan)
        if spec is None:
            return None
        minimum = D(0)
        coarse_step = False
        for leg in self._legs(spec, D(1), plan.kdf_price):
            snap = self.controller.market_data_by_symbol[leg["market_data_key"]].current()
            book = snap.order_book()
            price = book.bids[0].price * (1 - spec.impact)
            minimum = max(minimum, (snap.min_quote_amount / price + snap.quantity_step) / D(leg["quantity"]))
            side = HedgeSide(leg["side"])
            best = book.asks[0].price if side is HedgeSide.BUY else book.bids[0].price
            coarse_step |= snap.quantity_step * best > MAX_HEDGE_DUST_USDT
            if depth_reentry and spec.quantity_mode == "auto":
                levels = book.asks if side is HedgeSide.BUY else book.bids
                raw_capacity = quantity_within_slippage(
                    levels, side=side, max_slippage=spec.impact,
                )
                reserved = D(0)
                for order in self.controller.ownership.active():
                    other = self._for_quote(order)
                    if other is None:
                        continue
                    for other_leg in self._legs(
                        other,
                        self._coverage_volume(order),
                        self._coverage_volume(order) * order.kdf_price,
                    ):
                        if (
                            other_leg["market_data_key"] == leg["market_data_key"]
                            and other_leg["side"] == leg["side"]
                        ):
                            reserved += D(other_leg["quantity"])
                requested = plan.kdf_volume * D(leg["quantity"])
                if (reserved + requested) * AUTO_REENTRY_MIN_FACTOR > raw_capacity:
                    raise ValueError(
                        f"ripubblicazione {spec.sold.ticker} → {spec.bought.ticker} sospesa: "
                        f"profondità {spec.cex} {side.value} {leg['asset']} {raw_capacity}, "
                        f"impegno {reserved + requested}; richiesto margine di rientro del 25%"
                    )
        minimum = minimum.quantize(D(".00000001"), rounding=ROUND_CEILING)
        if for_new_auto and spec.quantity_mode == "auto" and plan.kdf_volume < minimum * AUTO_REENTRY_MIN_FACTOR:
            raise ValueError(
                f"quantità automatica {plan.kdf_volume} {spec.sold.ticker} troppo vicina al minimo "
                f"{spec.cex} {minimum}: attesa margine di stabilità del 25% prima di ripubblicare"
            )
        if coarse_step:
            # A taker may otherwise choose any partial amount, whose hedge
            # could exceed the precision residual guard even when the full
            # quote was aligned. KDF accepts min_volume == volume.
            minimum = plan.kdf_volume
        if minimum > plan.kdf_volume:
            raise ValueError(f"quantità inferiore al minimo swap copribile su {spec.cex}")
        return minimum

    def _register(self, spec: StrategySpec, *, validate: bool) -> None:
        c = self.controller
        client = self._client(spec)
        if validate and not {spec.base.ticker, spec.quote.ticker} <= set(c.enabled_tickers()):
            raise ValueError("attivare entrambe le coin KDF prima di configurare la strategia")
        for route in (spec.base, spec.quote):
            if not route.symbol:
                continue
            if validate and client is not None:
                rules = client.symbol_rules(route.symbol)
                if rules.base_asset != route.asset or rules.quote_asset != "USDT" or not all(rules.allows(s) for s in HedgeSide) or "LIMIT" not in rules.order_types:
                    raise ValueError(f"route Spot non negoziabile: {route.symbol}")
            key = self._key(spec, route.symbol)
            if key not in c.market_data_by_symbol:
                if self.feeds is None:
                    raise ValueError(f"feed pubblico {spec.cex} richiesto")
                template = c.market_data
                data = MarketDataStore(symbol=route.symbol, secret=template.secret,
                                       clock_ms=template.clock_ms, max_age_ms=template.max_age_ms)
                feed = MexcPublicFeed(client=client, store=data, snapshot_secret=template.secret,
                                      symbol=route.symbol, venue=spec.cex)
                self.feeds.add(key, feed)
                c.market_data_by_symbol = {**c.market_data_by_symbol, key: data}
            if validate and self.feeds is not None:
                # Read-only refresh for the initial preview; render thread never
                # calls this. Existing pollers continue to supply running quotes.
                self.feeds.feeds[key].fetch_once()
        market = MarketSpec(spec.market_id, spec.quote.ticker, spec.base.ticker,
                            self._key(spec, spec.base.symbol),
                            self._key(spec, spec.quote.symbol) if spec.quote.symbol else None,
                            2 if spec.quote.symbol else 1)
        if spec.market_id in c.markets and c.markets[spec.market_id] != market:
            peers = [item for item in self._specs.values()
                     if item.market_id == spec.market_id and item.strategy_id != spec.strategy_id]
            if any(item.cex != spec.cex for item in peers):
                raise ValueError("un mercato/lato non può usare due CEX diversi contemporaneamente")
        c.markets = {**c.markets, spec.market_id: market}

    def status(self) -> dict[str, Any]:
        with self.lock:
            rows = self.store.rows()
            for row in rows:
                spec = StrategySpec.from_payload(row["spec"])
                remaining, daily = self.store.remaining(spec)
                row["remaining_sold"] = str(remaining) if remaining is not None else "automatico"
                row["daily_remaining_sold"] = str(daily) if daily is not None else "nessun limite"
            return {"schema_version": 1, "strategies": rows, "local_first": True,
                    "worker_running": bool(self.thread and self.thread.is_alive())}

    def refunded_swap_uuids(self) -> tuple[str, ...]:
        return self.store.refunded_swap_uuids()

    def assert_legacy_available(self, market_id, side):
        pair = self.controller.market_spec(market_id).pair_for(side)
        if any((s.sold.ticker, s.bought.ticker) == pair for s in self._specs.values()):
            raise ValueError("mercato gestito dalla pagina Strategie: usare i suoi comandi")

    def order_display(self, order):
        """Read-only enrichment of the ACTUAL order, using its durable binding."""
        with self.lock:
            sid = self.store.strategy_for_order(order.order_uuid)
            if not sid:
                return {}  # Never guess a level from its pair or latest preview.
            spec = StrategySpec.from_payload(self.store.get(sid)['spec'])
            result = {'strategy_id': sid, 'cex': spec.cex,
                      'configured_premium': str(spec.premium),
                      'valuation_asset': spec.base.ticker, 'price_usdt': None,
                      'price_usdt_needed': spec.quote.asset != 'USDT' or spec.side is DexSide.BUY_ARRR}
            try:
                price = D(str(order.kdf_price))
                if not price.is_finite() or price <= 0:
                    return result
                human = price if spec.side is DexSide.SELL_ARRR else 1 / price
                quote_usdt = D(1)
                if spec.quote.symbol:
                    snap = self.controller.market_data_by_symbol[self._key(spec, spec.quote.symbol)].current()
                    book = snap.order_book()
                    if not book.bids or not book.asks or book.bids[0].price >= book.asks[0].price:
                        return result
                    quote_usdt = (book.bids[0].price + book.asks[0].price) / 2
                    result['valuation_observed_at_ms'] = snap.observed_at_ms
                result['price_usdt'] = str(human * quote_usdt)
            except (ValueError, KeyError, ArithmeticError):
                pass  # Missing/stale conversion is not a zero price.
            return result

    def _for_quote(self, item):
        sid = getattr(item, "strategy_id", "") or self.store.strategy_for_order(getattr(item, "order_uuid", ""))
        if sid:
            return self._specs.get(sid) or StrategySpec.from_payload(self.store.get(sid)["spec"])
        candidates = [s for s in self._specs.values() if s.market_id == item.market_id and s.side == item.dex_side]
        writing = [s for s in candidates if self.store.get(s.strategy_id)["state"] == "WRITING"]
        return writing[0] if len(writing) == 1 else candidates[0] if len(candidates) == 1 else None

    def orders_for(self, spec):
        return tuple(o for o in self.controller.active_orders_for_market_side(spec.market_id, spec.side)
                     if self.store.strategy_for_order(o.order_uuid) == spec.strategy_id)

    def allow_siblings(self, plan, orders):
        spec = self._for_quote(plan)
        if spec is None:
            return False
        for order in orders:
            other = self._for_quote(order)
            if (other is None or other.strategy_id == spec.strategy_id
                    or (other.scale_group or other.strategy_id) != (spec.scale_group or spec.strategy_id)
                    or other.premium == spec.premium or order.kdf_price == plan.kdf_price):
                return False
        return True

    def _legs(self, spec, sold, bought):
        return tuple({"symbol": route.symbol, "market_data_key": self._key(spec, route.symbol),
                      "asset": route.asset, "side": side, "quantity": str(amount),
                      "kdf_ticker": route.ticker, "cex": spec.cex}
                     for route, side, amount in ((spec.sold, "BUY", sold), (spec.bought, "SELL", bought)) if route.symbol)

    def coverage_requirement(self, item, spec_override=None, *, check_depth=True):
        spec = spec_override or self._for_quote(item)
        if spec is None:
            return None
        needed = {}
        volume = self._coverage_volume(item)
        for leg in self._legs(spec, volume, volume * item.kdf_price):
            snap = self.controller.market_data_by_symbol[leg["market_data_key"]].current()
            side = HedgeSide(leg["side"])
            book = snap.order_book()
            levels = book.asks if side is HedgeSide.BUY else book.bids
            quantity = D(leg["quantity"])
            capacity = quantity_within_slippage(levels, side=side, max_slippage=spec.impact) * spec.depth_fraction
            if check_depth and capacity < quantity:
                raise HedgeDepthError(f"{spec.sold.ticker} → {spec.bought.ticker}: profondità hedge ridotta; "
                                      f"{side.value} {leg['asset']} richiede {quantity}, "
                                      f"massimo {capacity}. Ritirare/ridimensionare questo ordine.")
            if side is HedgeSide.BUY:
                # Reserve for upward CEX precision rounding as well as fees.
                quantity = (quantity / snap.quantity_step).to_integral_value(rounding=ROUND_CEILING) * snap.quantity_step
                asset, amount = "USDT", quantity * levels[0].price * (1 + spec.impact)
            else:
                asset, amount = leg["asset"], quantity
            asset = self._coverage_key(spec, asset)
            needed[asset] = needed.get(asset, D(0)) + amount * (1 + self._fee(spec))
        return needed

    def _coverage_volume(self, item) -> Decimal:
        """Potential fill, without confusing pool allocation with order size."""
        order_uuid = getattr(item, "order_uuid", "")
        if order_uuid and self.controller.ownership.swaps_for_order(order_uuid):
            # After a partial swap KDF's available amount is the true residual.
            return D(item.kdf_volume)
        advertised = getattr(item, "advertised_volume", None)
        return D(advertised if advertised is not None else item.kdf_volume)

    def _preview(
        self, spec, *, extra=(), exclude=None, extra_specs=(), exclude_many=(),
        diagnostics_only=False, allow_read_only=False,
    ):
        c = self.controller
        coverage = c.coverage.status({}) if c.coverage else {}
        if (not coverage.get("lease_fresh") and allow_read_only
                and self.preview_balances is not None):
            coverage = self.preview_balances(spec.cex)
            # Private reads may take several seconds over Tor. Refresh depth
            # afterwards without repeating cached symbol metadata.
            if self.feeds is not None:
                for route in (spec.base, spec.quote):
                    if route.symbol:
                        self.feeds.feeds[self._key(spec, route.symbol)].fetch_once()
        if not coverage.get("lease_fresh"):
            raise ValueError(f"saldo Spot {spec.cex} aggiornato richiesto per dimensionare l'ordine")
        required_symbols = {self._key(spec, r.symbol) for r in (spec.base, spec.quote) if r.symbol}
        if not required_symbols <= set(coverage.get("hedge_symbols", [])):
            raise ValueError(f"verificare/aggiornare il worker: {spec.cex} non conferma tutte le route richieste")
        free = {a: D(v) for a, v in coverage["free_balances"].items()}
        excluded = set(exclude_many) | {exclude}
        needed = {}
        reserved = {}
        active = [o for o in c.ownership.active() if o.order_uuid not in excluded]
        for order in active:
            requirement = self.coverage_requirement(order, check_depth=False)
            if requirement is None and exclude_many:
                raise ValueError("riduzione automatica non disponibile con ordini legacy attivi")
            if requirement is None:
                volume = self._coverage_volume(order)
                c._add_coverage_requirement(needed, dex_side=order.dex_side,
                    base_quantity=volume if order.dex_side is DexSide.SELL_ARRR else volume * order.kdf_price,
                    market_id=order.market_id)
                continue
            for asset, amount in requirement.items():
                needed[asset] = needed.get(asset, D(0)) + amount
        def lookup(item):
            return next((s for s in extra_specs if s.strategy_id == getattr(item, "strategy_id", "")), None) or self._for_quote(item)
        for item in (*active, *extra):
            other = lookup(item)
            if other is not None:
                volume = self._coverage_volume(item)
                for leg in self._legs(other, volume, volume * item.kdf_price):
                    key = leg["market_data_key"], leg["side"]
                    reserved[key] = reserved.get(key, D(0)) + D(leg["quantity"])
            else:
                # Legacy base-side hedge also consumes shared liquidity.
                market = c.market_spec(item.market_id)
                key = market.base_cex_symbol, "BUY" if item.dex_side is DexSide.SELL_ARRR else "SELL"
                volume = self._coverage_volume(item)
                reserved[key] = reserved.get(key, D(0)) + (volume if item.dex_side is DexSide.SELL_ARRR else volume * item.kdf_price)
        for plan in extra:
            required = self.coverage_requirement(plan, lookup(plan))
            if required is None:
                raise ValueError("route della strategia opposta non disponibile")
            for asset, amount in required.items():
                needed[asset] = needed.get(asset, D(0)) + amount
        free = {asset: max(D(0), amount - needed.get(asset, D(0))) for asset, amount in free.items()}
        free = {asset: free.get(self._coverage_key(spec, asset), D(0))
                for asset in {"USDT", spec.base.asset, spec.quote.asset}}
        maximum = D(_numeric_decimal(c.kdf.max_maker_volume(spec.sold.ticker), "volume"))
        committed = sum((self._coverage_volume(o) for o in (*active, *extra)
                         if (o.kdf_base, o.kdf_rel) == (spec.sold.ticker, spec.bought.ticker)), D(0))
        remaining, daily = self.store.remaining(spec)
        snapshots = {r.symbol: c.market_data_by_symbol[self._key(spec, r.symbol)].current()
                     for r in (spec.base, spec.quote) if r.symbol}
        venue_reserved = {
            (route.symbol, side): reserved.get((self._key(spec, route.symbol), side), D(0))
            for route in (spec.base, spec.quote) if route.symbol
            for side in ("BUY", "SELL")
        }
        if (coverage.get("source") == "read_only_preview"
                and time.monotonic() >= coverage["expires_monotonic"]):
            raise ValueError(f"{spec.cex}: saldo preview scaduto durante il calcolo; riprovare")
        sizing_spec = (replace(spec, fixed_sold=min(spec.fixed_sold, remaining))
                       if spec.quantity_mode == "fixed" and not spec.replenish and remaining is not None and remaining > 0
                       else spec)
        preview = preview_strategy(sizing_spec, snapshots, kdf_free=max(D(0), maximum - committed), cex_free=free,
                                remaining_budget=remaining, daily_remaining=daily, fee=self._fee(spec),
                                buffer=c.risk_buffer, daily_volume_fraction=c.max_daily_volume_fraction,
                                reserved_hedges=venue_reserved, diagnostics_only=diagnostics_only)
        if diagnostics_only:
            return preview
        return replace(preview, plan=replace(preview.plan, strategy_id=spec.strategy_id))

    def capacity(self, raw_spec):
        spec = StrategySpec.from_payload(raw_spec)
        with self.lock, self.controller._order_lock:
            self._register(spec, validate=True)
            exclude = tuple(o.order_uuid for o in self.orders_for(spec)) if spec.strategy_id in self._specs else ()
            return self._preview(spec, diagnostics_only=True, exclude_many=exclude, allow_read_only=True)

    def scale_preview(self, payload):
        """Server-owned replicas: clients cannot override safety settings/routes."""
        with self.lock, self.controller._order_lock:
            source = self._specs[str(payload["source_id"])]
            row = self.store.get(source.strategy_id)
            if not row["enabled"] or row["state"] not in {"RUNNING", "STABILIZING", "WAITING"}:
                raise ValueError("Scala richiede una strategia attiva senza anomalie")
            request_id = str(payload["request_id"])
            if not request_id.isascii() or not request_id.isalnum() or not 8 <= len(request_id) <= 40:
                raise ValueError("identificativo richiesta Scala non valido")
            qty, premium = D(str(payload["quantity"])), D(str(payload["premium"])) / 100
            if not qty.is_finite() or qty < 0:
                raise ValueError("Quantità Scala: inserire un importo positivo oppure 0 per la stessa quantità pubblicata")
            if qty == 0:
                current = self.orders_for(source)
                if len(current) != 1:
                    raise ValueError("Stessa quantità non disponibile: l'ordine originale non è pubblicato. Attendere la ripresa oppure inserire una quantità esplicita.")
                qty = current[0].kdf_volume
            mode = payload.get("opposite", "no")
            if mode not in {"no", "si", "personalizza"}:
                raise ValueError("mercato speculare non valido")
            costs = self._fee(source) * (2 if source.quote.symbol else 1) + self.controller.risk_buffer
            sign = 1 if source.side is DexSide.SELL_ARRR else -1
            fixed_price = (source.fixed_price * (1 + premium + sign * costs) /
                           (1 + source.premium + sign * costs)) if source.price_mode == "fixed" else None
            first = replace(source, strategy_id="scala-" + request_id, scale_group=source.scale_group or source.strategy_id,
                            premium=premium, quantity_mode="fixed", fixed_sold=qty, max_sold=qty,
                            total_sold_budget=qty, fixed_price=fixed_price, auto_fraction=D(1))
            for spec in (source,):
                block = self.reconciliation.block_quote(spec.market_id, spec.side)
                if block:
                    raise ValueError(block)
            if not self.controller.kdf.orders_enabled:
                raise ValueError("pubblicazione live disabilitata")
            coverage = self.controller.coverage.status({}) if self.controller.coverage else {}
            if not coverage.get("live_hedging_enabled") or coverage.get("strategy_version") != 1:
                raise ValueError(f"worker {source.cex} live non pronto")
            self._register(first, validate=True)
            # Automatic siblings can give up capacity, but only after a preview
            # and explicit confirmation; each reduction is acknowledged by KDF.
            autos = [s for s in self._specs.values() if s.market_id == source.market_id
                     and s.quantity_mode == "auto" and self.store.get(s.strategy_id)["enabled"]]
            excluded = tuple(o.order_uuid for s in autos for o in self.orders_for(s))
            specs = [first]
            previews = [self._preview(first, exclude_many=excluded)]
            if mode != "no":
                book = self.controller.market_data_by_symbol[self._key(source, source.base.symbol)].current().order_book()
                reverse_sell = source.side is DexSide.BUY_ARRR
                bp = book.asks[0].price if reverse_sell else book.bids[0].price
                qp = D(1)
                if source.quote.symbol:
                    qbook = self.controller.market_data_by_symbol[self._key(source, source.quote.symbol)].current().order_book()
                    qp = qbook.bids[0].price if reverse_sell else qbook.asks[0].price
                rp = -premium if mode == "si" else D(str(payload["opposite_premium"])) / 100
                price = bp / qp * (1 + rp + (costs if reverse_sell else -costs))
                reverse = opposite_spec(first, previews[0], price)
                parents = [s for s in self._specs.values() if s.market_id == reverse.market_id and s.side == reverse.side]
                reverse = replace(reverse, premium=rp, scale_group=(parents[0].scale_group or parents[0].strategy_id) if parents else reverse.strategy_id)
                if mode == "personalizza":
                    rq = D(str(payload["opposite_quantity"]))
                    reverse = replace(reverse, fixed_sold=rq, max_sold=rq, total_sold_budget=rq)
                specs.append(reverse)
                self._register(reverse, validate=True)
                previews.append(self._preview(reverse, extra=(previews[0].plan,), extra_specs=specs, exclude_many=excluded))
            for spec, preview in zip(specs, previews):
                self.minimum_volume(preview.plan, spec)
                block = self.reconciliation.block_quote(spec.market_id, spec.side)
                if block:
                    raise ValueError(block)
                for other in self._specs.values():
                    if (other.sold.ticker, other.bought.ticker) == (spec.sold.ticker, spec.bought.ticker):
                        if other.premium == spec.premium:
                            raise ValueError("premium già presente per questa direzione")
                        if (other.scale_group or other.strategy_id) != spec.scale_group:
                            raise ValueError("direzione appartenente a un altro gruppo")
                if any(o.kdf_price == preview.plan.kdf_price for o in self.controller.active_orders_for_market_side(spec.market_id, spec.side)):
                    raise ValueError("il prezzo finale coincide con un livello esistente")
            reductions = []
            extras = [p.plan for p in previews]
            for auto in autos:
                p = self._preview(auto, extra=tuple(extras), extra_specs=specs, exclude_many=excluded)
                self.minimum_volume(p.plan, auto)
                extras.append(p.plan)
                current = self.orders_for(auto)
                if current and p.plan.kdf_volume < current[0].kdf_volume:
                    reductions.append({"strategy_id": auto.strategy_id, "order_uuid": current[0].order_uuid,
                                       "from": str(current[0].kdf_volume), "to": str(p.plan.kdf_volume)})
            return {"specs": [s.payload() for s in specs], "previews": [p.payload() for p in previews],
                    "resolved_quantity": str(qty),
                    "reductions": reductions, "scale": True,
                    "notice": "Quantità aggiuntive. Riduzioni auto confermate diventano nuovi massimi per ordine. Pubblicazione sequenziale, non atomica."}

    def scale_publish(self, payload):
        with self.lock, self.controller._order_lock:
            ids = ["scala-" + str(payload["request_id"])]
            if payload.get("opposite", "no") != "no":
                ids.append(ids[0] + "-opposto")
            if any(sid in self._specs for sid in ids):
                raise ValueError("richiesta Scala già registrata: controllare i livelli senza reinviare")
            result = self.scale_preview(payload)
            if D(str(payload['quantity'])) == 0 and payload.get('confirmed_quantity') != result['resolved_quantity']:
                raise ValueError("Quantità dell'originale cambiata o non confermata: ripetere anteprima e conferma, senza pubblicare automaticamente.")
            if result["reductions"] != payload.get("confirmed_reductions", []):
                raise ValueError("quantità automatica cambiata: ripetere anteprima e conferma")
            specs = tuple(StrategySpec.from_payload(s) for s in result["specs"])
            # Persist configuration before external writes; partial success is
            # visible and never retried by repeating the HTTP request.
            self.store.create_group(specs, scaled=True)
            self._specs.update({s.strategy_id: s for s in specs})
            try:
                for reduction in result["reductions"]:
                    sid = reduction["strategy_id"]
                    auto = self._specs[sid]
                    capped = replace(auto, max_sold=D(reduction["to"]) / auto.auto_fraction)
                    self.store.cap_auto(sid, capped.max_sold)
                    self._specs[sid] = capped
                    self._cycle(capped, self.store.get(sid))
                    orders = self.orders_for(capped)
                    if len(orders) != 1 or orders[0].kdf_volume > D(reduction['to']):
                        raise ValueError("riduzione KDF non confermata; livelli nuovi restano in pausa")
                for spec in specs:
                    # Explicit live confirmation authorizes a first publication;
                    # subsequent increases retain normal stabilization rules.
                    preview = self._preview(spec)
                    self.minimum_volume(preview.plan, spec)
                    self.store.update(spec.strategy_id, preview=json.dumps(preview.payload()), state="WRITING")
                    order = self.controller.publish_quote(preview.plan)
                    self.store.bind_order(spec.strategy_id, order.order_uuid)
                    self.controller.ownership.bind_strategy(order.order_uuid, spec.strategy_id)
                    self.store.update(spec.strategy_id, enabled=1, state="RUNNING", last_write=self.clock())
            except Exception as exc:
                for sid in (*ids, *(r["strategy_id"] for r in result["reductions"])):
                    if self.store.get(sid)["state"] == "WRITING":
                        self.store.update(sid, enabled=0, state="REVIEW_REQUIRED", detail=str(exc))
                raise ValueError("Scala registrata con esito parziale: controllare i livelli. " + str(exc)) from exc
            return self.status()

    def preview_group(self, raw_specs):
        specs = tuple(StrategySpec.from_payload(raw) for raw in raw_specs)
        if not 1 <= len(specs) <= 2:
            raise ValueError("massimo due strategie per conferma")
        with self.lock, self.controller._order_lock:
            for spec in specs:
                self._register(spec, validate=True)
            previews = []
            for spec in specs:
                previews.append(self._preview(spec, extra=tuple(p.plan for p in previews), extra_specs=specs, allow_read_only=True))
                self.minimum_volume(previews[-1].plan, spec)
            return {"previews": [p.payload() for p in previews], "specs": [s.payload() for s in specs],
                    "publication": "Configurazioni salvate in pausa; avvio separato, pubblicazione KDF sequenziale"}

    def create_group(self, raw_specs):
        with self.lock:
            result = self.preview_group(raw_specs)
            specs = tuple(StrategySpec.from_payload(raw) for raw in result["specs"])
            legacy = self.repricing.payload().get("quotes", {})
            targets = legacy.values() if isinstance(legacy, dict) else legacy
            pairs = {self.controller.market_spec(str(t["market_id"])).pair_for(DexSide(t["dex_side"])) for t in targets}
            pairs.update((o.kdf_base, o.kdf_rel) for o in self.controller.ownership.active())
            for spec in specs:
                if (spec.sold.ticker, spec.bought.ticker) in pairs:
                    raise ValueError("mercato già gestito: fermare/rimuovere prima il vecchio target")
            self.store.create_group(specs)
            self._specs.update({s.strategy_id: s for s in specs})
            return self.status()

    def opposite(self, raw_spec):
        spec = StrategySpec.from_payload(raw_spec)
        self._register(spec, validate=True)
        first = self._preview(spec)
        snapshots = {r.symbol: self.controller.market_data_by_symbol[self._key(spec, r.symbol)].current()
                     for r in (spec.base, spec.quote) if r.symbol}
        sell_base = spec.side is DexSide.BUY_ARRR
        base_book = snapshots[spec.base.symbol].order_book()
        bp = base_book.asks[0].price if sell_base else base_book.bids[0].price
        qp = D(1)
        if spec.quote.symbol:
            book = snapshots[spec.quote.symbol].order_book()
            qp = book.bids[0].price if sell_base else book.asks[0].price
        costs = self._fee(spec) * (2 if spec.quote.symbol else 1) + self.controller.risk_buffer
        price = bp / qp * (1 - spec.premium + (costs if sell_base else -costs))
        opposite = opposite_spec(spec, first, price)
        return {"spec": opposite.payload(), "notice": "Quantità equivalente in asset base all'anteprima corrente; i limiti di sicurezza possono ridurla se automatica."}

    def replace(self, raw_spec):
        spec = StrategySpec.from_payload(raw_spec)
        with self.lock, self.controller._order_lock:
            row = self.store.get(spec.strategy_id)
            previous = self._specs[spec.strategy_id]
            if spec.scale_group != previous.scale_group or any(
                s.strategy_id != spec.strategy_id and (s.sold.ticker, s.bought.ticker) == (spec.sold.ticker, spec.bought.ticker)
                and s.premium == spec.premium for s in self._specs.values()
            ):
                raise ValueError("gruppo non modificabile o premium già presente")
            if row["enabled"] or self.orders_for(previous):
                raise ValueError("mettere in pausa e ritirare l'ordine prima di modificare")
            block = self.reconciliation.block_quote(previous.market_id, previous.side)
            if block:
                raise ValueError(block)
            self.preview_group([spec.payload()])
            self.store.replace_spec(spec)
            self._specs[spec.strategy_id] = spec
            return self.status()

    def pause_for_order(self, order_uuid):
        sid = self.store.strategy_for_order(order_uuid)
        if sid is None:
            return False
        self.set_enabled(sid, False)
        return True

    def set_enabled(self, strategy_id: str, enabled: bool):
        with self.lock:
            row = self.store.get(strategy_id)
            if row["state"] == "DELETED":
                raise ValueError("strategia eliminata: creare una nuova configurazione")
            if enabled and row["state"] in {"WRITING", "REVIEW_REQUIRED"}:
                if row['detail'].startswith(LEGACY_UPDATE_TIMEOUT):
                    raise ValueError(f"riconciliazione automatica KDF non conclusa: {row['detail']}")
                raise ValueError("scrittura incerta: riconciliazione manuale richiesta")
            sticky_review = row["state"] in {"WRITING", "REVIEW_REQUIRED"}
            if not enabled and self.store.update_intent(strategy_id):
                self.store.set_update_intent_state(strategy_id, 'HELD', 'Pausa manuale')
                sticky_review = True
            detail = row['detail'] if sticky_review else ''
            if not enabled and sticky_review:
                if row['detail'].startswith(LEGACY_UPDATE_TIMEOUT):
                    detail = 'Pausa manuale: recupero del timeout KDF sospeso'
                elif (row['detail'].startswith('KDF RPC cancel_order:')
                      and 'HTTP 404' in row['detail']):
                    detail = 'Pausa manuale: recupero dello swap KDF sospeso'
            self.store.update(strategy_id, enabled=int(enabled), state="REVIEW_REQUIRED" if sticky_review else "WAITING" if enabled else "PAUSED",
                              detail=detail, confirmations=0, evidence="null")
            if not enabled:
                if sticky_review:
                    self.controller.publications.hold(strategy_id)
                try:
                    self._cancel(self._specs[strategy_id], reason='Strategia messa in pausa', source='strategy_pause')
                except Exception as exc:
                    self.store.update(strategy_id, state="REVIEW_REQUIRED", detail=str(exc))
                    raise
            return self.status()

    def set_all_enabled(self, enabled: bool):
        """Pause every active strategy or resume every clean manual pause.

        Pausing is best-effort across the complete set: one uncertain KDF
        cancellation must not prevent the remaining strategies from being
        made safe.  Resuming is deliberately limited to PAUSED strategies;
        exhausted or review-required rows are never restarted in bulk.
        """
        with self.lock:
            rows = self.store.rows()
            if enabled:
                targets = [row["id"] for row in rows
                           if not row["enabled"] and row["state"] == "PAUSED"]
            else:
                targets = [row["id"] for row in rows if row["enabled"]]
            changed, errors = [], []
            for strategy_id in targets:
                try:
                    self.set_enabled(strategy_id, enabled)
                    changed.append(strategy_id)
                except Exception as exc:
                    errors.append(f"{strategy_id}: {exc}")
            if errors:
                action = "riattivazione" if enabled else "pausa"
                raise ValueError(f"{action} globale incompleta: " + "; ".join(errors))
            result = self.status()
            result["changed_strategy_ids"] = changed
            result["bulk_action"] = "STARTED" if enabled else "PAUSED"
            return result

    def delete(self, strategy_id: str):
        return self.delete_group((strategy_id,))

    def delete_group(self, strategy_ids, *, pause_first=False):
        ids = tuple(strategy_ids)
        if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
            raise ValueError("selezionare una o due strategie distinte")
        with self.lock, self.controller._order_lock:
            targets = []
            for sid in ids:
                row = self.store.get(sid)
                if row["state"] == "DELETED":
                    continue
                if row["state"] in {"WRITING", "REVIEW_REQUIRED"}:
                    raise ValueError("risolvere le anomalie prima di eliminare")
                if not pause_first and (row["enabled"] or row["state"] != "PAUSED"):
                    raise ValueError("premere H per mettere in pausa prima di eliminare")
                targets.append(self._specs[sid])
            # Cancellation is external and cannot be rolled back. If it fails,
            # retain every configuration; previously paused sides stay paused.
            for spec in targets:
                if pause_first:
                    self.set_enabled(spec.strategy_id, False)
            for spec in targets:
                if self.orders_for(spec):
                    raise ValueError("ordine KDF ancora attivo: completare la pausa prima di eliminare")
                block = self.reconciliation.block_quote(spec.market_id, spec.side)
                if block:
                    raise ValueError(block)
            self.store.archive_group(ids)
            for sid in ids:
                self._specs.pop(sid, None)
            return self.status()

    def _cancel(self, spec, *, reason, source):
        for order in self.controller.active_orders_for_market_side(spec.market_id, spec.side):
            if self.store.strategy_for_order(order.order_uuid) == spec.strategy_id:
                self.controller.cancel_owned_order(order.order_uuid, reason=reason, source=source, strategy_id=spec.strategy_id)

    def observe_swap(self, order, swap, status):
        sid = self.store.strategy_for_order(order.order_uuid)
        if sid is None:
            spec = self._for_quote(order)
            if spec is not None and self.store.get(spec.strategy_id)["state"] == "WRITING":
                sid = spec.strategy_id
                self.store.bind_order(sid, order.order_uuid)
                self.controller.ownership.bind_strategy(order.order_uuid, sid)
        if sid is not None:
            terms = _swap_terms(status)
            self.store.record_swap(strategy_id=sid, swap_uuid=swap.swap_uuid,
                                   sold=D(terms["maker_amount"]), outcome=swap.state.value)

    def hedge_route(self, order, terms):
        sid = self.store.strategy_for_order(order.order_uuid)
        if sid is None:
            return {}
        # Archived strategies retain immutable historical routes for replay.
        spec = self._specs.get(sid) or StrategySpec.from_payload(self.store.get(sid)["spec"])
        return {"strategy_id": sid, "cex": spec.cex,
                "hedge_symbol": spec.base.symbol, "hedge_base_asset": spec.base.asset,
                "hedge_quote_asset": "USDT",
                "hedge_legs": list(self._legs(spec, D(terms["maker_amount"]), D(terms["taker_amount"])))}

    def run_once(self):
        with self.lock, self.controller._order_lock:
            for row in self.store.rows():
                spec = self._specs[row["id"]]
                if self._recover_update(spec, row):
                    continue
                if self._recover_publication(spec, row):
                    continue
                if self._recover_legacy_update(spec, row):
                    continue
                if self._recover_completed_swap_review(spec, row):
                    continue
                if not row["enabled"]:
                    continue
                if row['state'] == 'RECOVERING':
                    # A terminal swap is itself one of the reasons block_quote()
                    # returns a gate.  Run the durable settlement proof before
                    # honoring that gate, otherwise a refunded swap can deadlock:
                    # RECOVERING skips _cycle(), while _cycle() was the only place
                    # that acknowledged the refund and released the inventory pool.
                    try:
                        if self.settlement:
                            self.settlement(spec, self.store)
                    except Exception as exc:
                        LOG.warning(
                            "recovering strategy settlement pending for %s: %s",
                            spec.strategy_id,
                            type(exc).__name__,
                        )
                    if self.reconciliation.block_quote(spec.market_id, spec.side):
                        # Let durable reconciliation observe the newly attributed
                        # UUID (or the now-acknowledged terminal swap) before
                        # repricing; never cancel it because of a stale snapshot.
                        continue
                try:
                    if row["state"] == "WRITING":
                        raise RuntimeError("scrittura precedente incerta")
                    self._cycle(spec, row)
                except Exception as exc:
                    if self.store.update_intent(spec.strategy_id):
                        # update_maker_order may have applied even if its reply
                        # was lost. Never submit the same volume_delta again.
                        self.store.update(spec.strategy_id, state="RECOVERING",
                            detail=f"Aggiornamento KDF incerto: {exc}. Verifica automatica in corso")
                        continue
                    # If any KDF write may have reached the network, do not
                    # republish on retry. Human reconciliation is mandatory.
                    uncertain = self.store.get(spec.strategy_id)["state"] == "WRITING" and not isinstance(exc, KdfPreflightError)
                    if uncertain:
                        self.store.update(spec.strategy_id, enabled=0, state="REVIEW_REQUIRED", detail=str(exc))
                    else:
                        try:
                            self._cancel(spec, reason=str(exc) or type(exc).__name__, source='strategy_safety')
                        except Exception as cancel_error:
                            self.store.update(spec.strategy_id, enabled=0, state="REVIEW_REQUIRED", detail=str(cancel_error))
                            continue
                        self.store.update(spec.strategy_id, state="WAITING", detail=str(exc), confirmations=0, evidence="null", preview="{}")

    def _recover_update(self, spec, row):
        intent = self.store.update_intent(spec.strategy_id)
        if intent is None:
            return False
        if intent['state'] == 'HELD' and intent['detail'] != POST_SWAP_UPDATE_HELD:
            return True  # A manual pause or ambiguous terms must not restart.
        now = time.monotonic()
        key = ('update', spec.strategy_id)
        if now - self._recovery_checked.get(key, float('-inf')) < 10:
            return True
        self._recovery_checked[key] = now
        uid = intent['order_uuid']
        owned = self.controller.ownership.get(uid)
        try:
            if owned is None or self.store.strategy_for_order(uid) != spec.strategy_id:
                raise ValueError('UUID non più attribuito alla strategia')
            orders = self.controller.kdf._maker_order_snapshot(timeout=5.0)
            observed = orders.get(uid)
            if observed is None:
                related = self.controller.ownership.swaps_for_order(uid)
                if related:
                    if any(swap.state is OwnedSwapState.FAILED for swap in related):
                        self.store.set_update_intent_state(spec.strategy_id, 'HELD',
                            'swap non riuscito o rimborsato')
                        self.store.update(spec.strategy_id, enabled=0, state='REVIEW_REQUIRED',
                            detail='Swap non riuscito: verificare rimborso ed eventuale hedge prima di riprendere')
                        return True
                    if self.settlement:
                        self.settlement(spec, self.store)
                    terminal = self.reconciliation.verified_settled_order(uid)
                    block = self.reconciliation.block_quote(spec.market_id, spec.side)
                    if terminal is not None and block is None and not self.controller.publications.pending(spec.strategy_id):
                        # No retry of the uncertain update or its volume_delta.
                        # The old UUID is gone and KDF history matches every
                        # successful swap whose hedge was durably settled.
                        self.controller.ownership.mark(uid, terminal,
                            error='Swap, hedge e ordine riconciliati nello storico KDF',
                            source='post_swap_update_readback')
                        self.store.set_update_intent_state(spec.strategy_id, 'DONE')
                        self.store.update(spec.strategy_id, enabled=1, state='WAITING',
                            detail='Swap riconciliato; nuova quotazione dopo feed e copertura',
                            confirmations=0, evidence='null', preview='{}')
                        return True
                    self.store.update(spec.strategy_id,
                        state='RECOVERING' if row['enabled'] else 'REVIEW_REQUIRED',
                        detail='Swap sull’ordine precedente: attesa esito KDF, hedge e storico ordine')
                    return True
                block = self.reconciliation.block_quote(spec.market_id, spec.side)
                if owned.status is OwnedOrderStatus.CANCELLED and block is None:
                    self.store.update(spec.strategy_id, state='WAITING',
                        detail='Ordine precedente cancellato; nuova quotazione dopo i controlli correnti',
                        confirmations=0, evidence='null', preview='{}')
                    self.store.set_update_intent_state(spec.strategy_id, 'DONE')
                elif owned.status in {OwnedOrderStatus.ERROR, OwnedOrderStatus.COMPLETED,
                                      OwnedOrderStatus.INSUFFICIENT_BALANCE}:
                    raise ValueError(POST_SWAP_UPDATE_HELD)
                else:
                    self.store.update(spec.strategy_id, state='RECOVERING',
                        detail='Ordine non visibile: attesa conferma della cancellazione o dello swap')
                return True
            block = self.reconciliation.block_quote(spec.market_id, spec.side)
            if (observed.get('uuid') != uid or observed.get('base') != owned.kdf_base
                    or observed.get('rel') != owned.kdf_rel):
                raise ValueError('termini UUID/pair KDF incoerenti')
            if observed.get('matches') or observed.get('started_swaps'):
                related = self.controller.ownership.swaps_for_order(uid)
                if related:
                    if any(swap.state is OwnedSwapState.FAILED for swap in related):
                        self.store.set_update_intent_state(spec.strategy_id, 'HELD',
                            'swap non riuscito o rimborsato')
                        self.store.update(spec.strategy_id, enabled=0, state='REVIEW_REQUIRED',
                            detail='Swap non riuscito: verificare rimborso ed eventuale hedge prima di riprendere')
                        return True
                    if self.settlement:
                        self.settlement(spec, self.store)
                    settled = all(swap.state is OwnedSwapState.SUCCEEDED and swap.acknowledged
                                  for swap in related)
                    started = observed.get('started_swaps')
                    matches = observed.get('matches')
                    if (settled and isinstance(started, list)
                            and all(isinstance(swap_uuid, str) for swap_uuid in started)
                            and len(started) == len(set(started))
                            and set(started) == {swap.swap_uuid for swap in related}
                            and isinstance(matches, Mapping) and matches
                            and set(matches) == set(started)):
                        active = self.controller.kdf.active_swaps(include_status=False)
                        if (not isinstance(active, Mapping) or not isinstance(active.get('uuids'), list)):
                            raise ValueError('stato swap attivi KDF incompleto')
                        if not any(swap.swap_uuid in active['uuids'] for swap in related):
                            price = D(str(observed['price']))
                            maximum = D(str(observed['max_base_vol']))
                            available = D(str(observed['available_amount']))
                            if (not all(value.is_finite() and value > 0
                                        for value in (price, maximum, available))
                                    or available > maximum):
                                raise ValueError('termini dell’ordine residuo KDF non validi')
                            if owned.status in {OwnedOrderStatus.COMPLETED, OwnedOrderStatus.ERROR}:
                                self.controller.ownership.restore_seen(
                                    uid, kdf_price=price, kdf_volume=available)
                            elif owned.status is OwnedOrderStatus.OPEN:
                                self.controller.ownership.update_quote(
                                    uid, kdf_price=price, kdf_volume=available)
                            else:
                                raise ValueError('ordine residuo KDF in stato locale non riattivabile')
                            if self.reconciliation.block_quote(spec.market_id, spec.side) is None:
                                self.store.set_update_intent_state(spec.strategy_id, 'DONE')
                                self.store.update(spec.strategy_id, enabled=1, state='RUNNING',
                                    detail='Ordine residuo verificato dopo swap; nuovo prezzo al prossimo controllo',
                                    last_write=self.clock(), confirmations=0, evidence='null')
                                return True
                self.store.update(spec.strategy_id, state='RECOVERING',
                    detail='Swap o match sullo stesso UUID: attesa riconciliazione prima del repricing')
                return True
            if owned.status is OwnedOrderStatus.CANCELLED:
                self.store.update(spec.strategy_id, state='RECOVERING',
                    detail='Cancellazione registrata; attesa scomparsa dell’UUID da KDF')
                return True
            if owned.status is not OwnedOrderStatus.OPEN:
                raise ValueError(f'ordine KDF in stato locale {owned.status}')
            price = D(str(observed['price']))
            maximum = D(str(observed['max_base_vol']))
            available = D(str(observed['available_amount']))
            minimum = D(str(observed['min_base_vol'])) if intent['min_volume'] is not None else None
            if (not all(value.is_finite() for value in (price, maximum, available))
                    or price <= 0 or maximum <= 0 or available <= 0 or available > maximum):
                raise ValueError('prezzo o volumi KDF non validi')
            desired = (price == D(intent['new_price']) and maximum == D(intent['new_volume'])
                       and (minimum is None or minimum == D(intent['min_volume'])))
            old = (price == D(intent['old_price']) and maximum == D(intent['old_volume'])
                   and available <= maximum)
            if desired:
                if owned.status is not OwnedOrderStatus.OPEN or block is not None:
                    self.store.update(spec.strategy_id, state='RECOVERING',
                        detail=f'Nuovi termini verificati; attesa riconciliazione: {block or owned.status}')
                    return True
                self.controller.ownership.update_quote(uid, kdf_price=price, kdf_volume=available)
                self.store.update(spec.strategy_id, state='RUNNING',
                    detail='Aggiornamento KDF recuperato e verificato',
                    last_write=self.clock(), confirmations=0, evidence='null')
                self.store.set_update_intent_state(spec.strategy_id, 'DONE')
                return True
            if not old:
                raise ValueError('ordine KDF con termini diversi sia dai precedenti sia da quelli richiesti')
            if block is not None:
                self.store.update(spec.strategy_id, state='RECOVERING',
                    detail=f'Ordine invariato; attesa riconciliazione: {block}')
                return True
            if intent['cancel_requested_at'] and self.clock() - intent['cancel_requested_at'] < 30:
                self.store.update(spec.strategy_id, state='RECOVERING',
                    detail='Ordine ancora ai vecchi termini; attesa prima di riprovare la cancellazione')
                return True
            # Invalidate the old UUID before any new publication. A late update
            # cannot create an order once cancellation has succeeded.
            self.store.note_update_cancel(spec.strategy_id)
            self.controller.cancel_owned_order(uid,
                reason='Aggiornamento KDF incerto: annullamento di sicurezza',
                source='strategy_update_recovery', strategy_id=spec.strategy_id)
            self.store.update(spec.strategy_id, state='WAITING',
                detail='Vecchio ordine annullato; nuova quotazione dopo i controlli correnti',
                confirmations=0, evidence='null', preview='{}')
            self.store.set_update_intent_state(spec.strategy_id, 'DONE')
        except Exception as exc:
            # Temporary reads/cancellation failures are retryable. Unknown
            # order identity or terms must instead remain fail-closed.
            if isinstance(exc, (ValueError, KeyError, ArithmeticError, KdfReconciliationError)):
                self.store.update(spec.strategy_id, enabled=0, state='REVIEW_REQUIRED',
                    detail=f'Aggiornamento KDF ambiguo: {exc}. Verifica manuale richiesta')
                self.store.set_update_intent_state(spec.strategy_id, 'HELD', str(exc))
            else:
                self.store.update(spec.strategy_id, state='RECOVERING',
                    detail=f'Verifica KDF temporaneamente indisponibile: {exc}; nuovo tentativo automatico')
        return True

    def _recover_publication(self, spec, row):
        journal = self.controller.publications
        intents = journal.pending(spec.strategy_id)
        if not intents:
            return False
        intent = intents[0]
        if intent['order_uuid'] and self.store.strategy_for_order(intent['order_uuid']) == spec.strategy_id:
            if row['state'] not in {'WRITING', 'REVIEW_REQUIRED'}:
                journal.state(intent['id'], 'DONE')
                return False
        if row['state'] not in {'WRITING', 'REVIEW_REQUIRED'}:
            return False
        now = time.monotonic()
        if now - self._recovery_checked.get(spec.strategy_id, float('-inf')) < 10:
            return True
        self._recovery_checked[spec.strategy_id] = now
        try:
            snapshot = self.controller.kdf._maker_order_snapshot(timeout=2.0)
            uid, plan = journal.candidate(intent, snapshot)
            self.controller.ownership.register(uid, plan)
            self.store.bind_order(spec.strategy_id, uid)
            self.controller.ownership.bind_strategy(uid, spec.strategy_id)
            self.controller.ownership.record_order_event(uid, 'PUBLICATION_RECOVERED',
                source='durable_intent', reason='Pubblicazione verificata dopo risposta incerta', strategy_id=spec.strategy_id)
            self.store.update(spec.strategy_id, enabled=1, state='RECOVERING',
                detail='UUID recuperato; attesa riconciliazione e controlli correnti', confirmations=0, evidence='null', preview='{}')
            journal.state(intent['id'], 'DONE', order_uuid=uid)
        except Exception as exc:
            journal.state(intent['id'], intent['state'], detail=str(exc))
            self.store.update(spec.strategy_id, enabled=0, state='REVIEW_REQUIRED',
                detail=f'Pubblicazione incerta: {exc}. Nessun reinvio; verifica in sola lettura ogni 10 s.')
        return True

    def _recover_legacy_update(self, spec, row):
        """Release a pre-intent update timeout only after KDF proves cancellation.

        Older versions disabled the strategy without recording the requested
        terms. We must never guess whether that update applied or retry its
        volume_delta. A cancelled UUID with no swap can instead be replaced by
        a fresh quote through the normal feed, coverage and confirmation gates.
        """
        if (row['enabled'] or row['state'] != 'REVIEW_REQUIRED'
                or not row['detail'].startswith(LEGACY_UPDATE_TIMEOUT)):
            return False
        now = time.monotonic()
        key = ('legacy_update', spec.strategy_id)
        if now - self._recovery_checked.get(key, float('-inf')) < 10:
            return True
        self._recovery_checked[key] = now
        def wait_for(reason):
            detail = f'{LEGACY_UPDATE_TIMEOUT}; recupero automatico: {reason}'
            if row['detail'] != detail:
                self.store.update(spec.strategy_id, detail=detail)
            return True
        try:
            bound = self.store.orders_for_strategy(spec.strategy_id)
            if not bound:
                return wait_for('nessun UUID attribuito alla strategia')
            if self.controller.publications.pending(spec.strategy_id):
                return wait_for('pubblicazione precedente ancora in verifica')
            ownership = self.controller.ownership
            latest = ownership.get(bound[0])
            if latest is None or latest.status is not OwnedOrderStatus.CANCELLED:
                return wait_for('ultimo ordine non confermato come cancellato')
            if ownership.swaps_for_order(latest.order_uuid):
                return wait_for('swap associato all’ultimo ordine; nessuna ripubblicazione')
            for uid in bound:
                order = ownership.get(uid)
                if order is None or order.status is OwnedOrderStatus.OPEN:
                    return wait_for('almeno un ordine della strategia è ancora aperto o non attribuito')
            block = self.reconciliation.block_quote(spec.market_id, spec.side)
            if block is not None:
                return wait_for(f'riconciliazione generale non pronta: {block}')
            orders = self.controller.kdf._maker_order_snapshot(timeout=5.0)
            if any(uid in orders for uid in bound):
                return wait_for('un UUID della strategia è ancora pubblicato su KDF')
            status = self.controller.kdf.order_status(latest.order_uuid)
            if not isinstance(status, Mapping):
                return wait_for('risposta order_status KDF incompleta')
            historical = status.get('order', status)
            if not isinstance(historical, Mapping):
                return wait_for('storico KDF incompleto')
            reason = historical.get('cancellation_reason', status.get('cancellation_reason'))
            if isinstance(reason, Mapping):
                reason = reason.get('type') or reason.get('reason')
            if (str(reason or '').replace('_', '').replace(' ', '').lower() != 'cancelled'
                    or historical.get('matches') or historical.get('started_swaps')
                    or 'matches' not in historical or 'started_swaps' not in historical):
                return wait_for('storico KDF non conferma cancellazione senza swap')
            ownership.record_order_event(latest.order_uuid, 'UPDATE_TIMEOUT_RECOVERED',
                source='kdf_readback', reason='Ultimo UUID cancellato senza swap; nessun aggiornamento ritentato',
                strategy_id=spec.strategy_id)
            self.store.update(spec.strategy_id, enabled=1, state='WAITING',
                detail='Timeout storico riconciliato; attesa feed e copertura prima della nuova pubblicazione',
                confirmations=0, evidence='null', preview='{}')
        except Exception as exc:
            # Network/read failures preserve the original review gate. The
            # next cycle retries the read; no KDF mutation is sent here.
            self.store.update(spec.strategy_id, detail=
                f'{LEGACY_UPDATE_TIMEOUT}; verifica automatica in attesa ({type(exc).__name__})')
        return True

    def _recover_completed_swap_review(self, spec, row):
        """Clear a failed post-match cancellation after durable settlement.

        KDF can remove a matched maker order before our safety cancellation
        reaches it. Neither HTTP 404 nor HTTP 500 is proof of settlement:
        retain REVIEW_REQUIRED until KDF history identifies the exact swap,
        the hedge journal acknowledges either success or a verified maker
        refund, and no bound order or swap remains active in KDF.
        """
        if (row['enabled'] or row['state'] != 'REVIEW_REQUIRED'
                or not row['detail'].startswith('KDF RPC cancel_order:')):
            return False
        key = ('completed_swap_review', spec.strategy_id)
        now = time.monotonic()
        if now - self._recovery_checked.get(key, float('-inf')) < 10:
            return True
        self._recovery_checked[key] = now
        try:
            bound = self.store.orders_for_strategy(spec.strategy_id)
            ownership = self.controller.ownership
            # The failed cancellation belongs to the newest bound UUID. Older
            # swaps on this strategy are historical and may have other outcomes.
            swaps = list(ownership.swaps_for_order(bound[0])) if bound else []
            if (not swaps or any(swap.state is OwnedSwapState.ACTIVE for swap in swaps)
                    or any(ownership.get(uid) is None or ownership.get(uid).status is OwnedOrderStatus.OPEN
                           for uid in bound[1:])):
                return True
            if self.settlement:
                self.settlement(spec, self.store)
            if any(not ownership.get_swap(swap.swap_uuid).acknowledged for swap in swaps):
                return True
            if self.controller.publications.pending(spec.strategy_id) or self.store.update_intent(spec.strategy_id):
                return True
            active = self.controller.kdf.active_swaps(include_status=False)
            if not isinstance(active, Mapping) or not isinstance(active.get('uuids'), list):
                return True
            if any(swap.swap_uuid in active['uuids'] for swap in swaps):
                return True
            orders = self.controller.kdf._maker_order_snapshot(timeout=5.0)
            if any(uid in orders for uid in bound[1:]):
                return True
            latest = ownership.get(bound[0])
            live = orders.get(bound[0])
            if latest is None:
                return True
            failed = any(swap.state is OwnedSwapState.FAILED for swap in swaps)
            if failed:
                # A refunded swap cannot leave a residual maker order.  Prove
                # that KDF history binds this exact fulfilled order to exactly
                # the terminal swaps before enabling a fresh publication.
                if live is not None or latest.status is not OwnedOrderStatus.COMPLETED:
                    return True
                historical_response = self.controller.kdf.order_status(bound[0])
                if not isinstance(historical_response, Mapping):
                    return True
                historical = historical_response.get('order', historical_response)
                if not isinstance(historical, Mapping):
                    return True
                reason = historical.get(
                    'cancellation_reason',
                    historical_response.get('cancellation_reason'),
                )
                if isinstance(reason, Mapping):
                    reason = reason.get('type') or reason.get('reason')
                normalized = str(reason or '').replace('_', '').replace(' ', '').lower()
                started = historical.get('started_swaps')
                matches = historical.get('matches')
                expected = {swap.swap_uuid for swap in swaps}
                if (
                    historical.get('uuid') != bound[0]
                    or historical.get('base') != latest.kdf_base
                    or historical.get('rel') != latest.kdf_rel
                    or normalized != 'fulfilled'
                    or not isinstance(started, list)
                    or set(started) != expected
                    or not isinstance(matches, Mapping)
                    or set(matches) != expected
                ):
                    return True
            if live is not None:
                if (latest.status is not OwnedOrderStatus.OPEN
                        or live.get('uuid') != bound[0]
                        or live.get('base') != latest.kdf_base
                        or live.get('rel') != latest.kdf_rel):
                    return True
            elif latest.status is OwnedOrderStatus.OPEN:
                return True
            if self.reconciliation.block_quote(spec.market_id, spec.side):
                return True
            for swap in swaps:
                ownership.record_order_event(swap.order_uuid,
                    'REFUNDED_SWAP_REVIEW_RECOVERED' if swap.state is OwnedSwapState.FAILED
                    else 'COMPLETED_SWAP_REVIEW_RECOVERED',
                    source='kdf_readback', reason=(
                        'Rimborso maker e hedge verificati; inventario MEXC conservato e copertura ricalcolata'
                        if swap.state is OwnedSwapState.FAILED else
                        'Swap e hedge verificati; UUID residuo attribuito' if live is not None
                        else 'Swap concluso, hedge verificato e nessun UUID aperto'),
                    strategy_id=spec.strategy_id)
            self.store.update(spec.strategy_id, enabled=1, state='WAITING',
                detail=('Rimborso e hedge riconciliati; inventario MEXC conservato, '
                        'attesa nuova copertura prima della pubblicazione' if failed else
                        'Swap e hedge riconciliati; attesa feed e copertura prima della nuova pubblicazione'),
                confirmations=0, evidence='null', preview='{}')
        except Exception as exc:
            # Read-only recovery: keep the review gate until every proof is
            # available. In particular, never retry the failed cancellation.
            LOG.warning("completed-swap review readback pending for %s: %s",
                        spec.strategy_id, type(exc).__name__)
        return True

    def _cycle(self, spec, row):
        c = self.controller
        targets = self.repricing.payload().get("quotes", {}).values()
        if any(c.market_spec(str(t["market_id"])).pair_for(DexSide(t["dex_side"])) == (spec.sold.ticker, spec.bought.ticker) for t in targets):
            raise ValueError("rimuovere il target repricing precedente prima di avviare la strategia")
        # A successful KDF swap is acknowledged automatically only after the
        # local, durable hedge journal confirms every leg. Failed swaps never
        # replenish automatically, even if an early hedge already completed.
        if self.settlement:
            self.settlement(spec, self.store)
        block = self.reconciliation.block_quote(spec.market_id, spec.side)
        if block:
            if "requires order reconciliation:" in block:
                # A freshly accepted setprice can be absent from one my_orders
                # snapshot. Keep its UUID reserved while the reconciler checks
                # history; cancelling it here caused a publish/cancel storm.
                self.store.update(spec.strategy_id, state="RECOVERING", detail=block)
                return
            raise ValueError(block)
        if not {spec.sold.ticker, spec.bought.ticker} <= set(c.enabled_tickers()):
            raise ValueError("coin non attive")
        if c.coverage is None or not c.coverage.status({}).get("live_hedging_enabled") or c.coverage.status({}).get("strategy_version") != 1:
            raise ValueError(f"worker {spec.cex} live non pronto")
        all_orders = c.active_orders_for_market_side(spec.market_id, spec.side)
        orders = self.orders_for(spec)
        if len(orders) > 1 or any(self._for_quote(o) is None for o in all_orders):
            raise ValueError("ordine di un altro gestore presente")
        order = orders[0] if orders else None
        remaining, daily = self.store.remaining(spec)
        if remaining is not None and remaining <= 0:
            self._cancel(spec, reason='Budget totale esaurito', source='strategy_budget')
            self.store.update(spec.strategy_id, enabled=0, state="EXHAUSTED", detail="budget totale esaurito")
            return
        if order is None:
            coverage_status = c.coverage.status({})
            if not coverage_status.get("publication_ready", True):
                self.store.update(spec.strategy_id, state="WAITING",
                    detail=f"ripubblicazione sospesa: attesa rinnovo completo {spec.cex}",
                    confirmations=0, evidence="null", preview="{}")
                return
            generation = int(coverage_status.get("recovery_generation", 0))
            since = coverage_status.get("recovery_since_ms")
            if generation and since is not None:
                record = self._publication_books.get(spec.strategy_id)
                if record is None or record["generation"] != generation:
                    record = {"generation": generation, "sequences": {}}
                    self._publication_books[spec.strategy_id] = record
                sequences = record["sequences"]
                symbols = {self._key(spec, route.symbol)
                           for route in (spec.base, spec.quote) if route.symbol}
                for symbol in symbols:
                    snap = c.market_data_by_symbol[symbol].current()
                    if snap.observed_at_ms > since:
                        seen = sequences.setdefault(symbol, [])
                        if snap.sequence not in seen:
                            seen.append(snap.sequence)
                            del seen[:-2]
                if any(len(sequences.get(symbol, [])) < 2 for symbol in symbols):
                    self.store.update(spec.strategy_id, state="WAITING",
                        detail=f"rinnovo {spec.cex} riuscito; attesa due nuovi book distinti per mercato",
                        confirmations=0, evidence="null", preview="{}")
                    return
            cooldown = self.store.safety_cooldown(spec.strategy_id)
            if cooldown is not None and self.clock() < cooldown["resume_after"]:
                seconds = int(cooldown["resume_after"] - self.clock()) + 1
                self.store.update(spec.strategy_id, state="WAITING",
                                  detail=f"ritiro di sicurezza ({cooldown['source']}): "
                                         f"ripresa dopo {seconds} s e nuovi controlli {spec.cex}",
                                  confirmations=0, evidence="null", preview="{}")
                return
        preview = self._preview(spec, exclude=order.order_uuid if order else None)
        current_volume = self._coverage_volume(order) if order is not None else D(0)
        increasing = order is None or preview.plan.kdf_volume > current_volume
        previous_quantity = row["preview"].get("plan", {}).get("kdf_volume")
        previous_quantity = D(previous_quantity) if previous_quantity is not None else None
        if not row['confirmations'] or row['evidence'] == 'null':
            previous_quantity = None
        if (increasing and spec.quantity_mode == 'auto' and previous_quantity is not None
                and previous_quantity > 0 and (order is None or previous_quantity > current_volume)
                and preview.plan.kdf_volume > previous_quantity):
            # Confirm a conservative floor across distinct books, not an exactly
            # repeated moving maximum. Recompute the complete plan at this cap;
            # never edit just volume and leave hedge legs/prices inconsistent.
            ceiling = previous_quantity / spec.auto_fraction
            capped = replace(spec, max_sold=min(ceiling, spec.max_sold)
                             if spec.max_sold is not None else ceiling)
            try:
                preview = self._preview(capped, exclude=order.order_uuid if order else None)
            except ValueError:
                # The old floor may now be below the CEX minimum. The uncapped
                # preview already passed all checks; restart its confirmations.
                previous_quantity = None
        evidence = json.dumps(preview.evidence)
        stable_candidate = previous_quantity is not None and previous_quantity > 0 and (
            preview.plan.kdf_volume <= previous_quantity or abs(preview.plan.kdf_volume / previous_quantity - 1) < spec.quantity_threshold
        ) and (order is None or previous_quantity > current_volume)
        confirmations = ((row["confirmations"] if stable_candidate else 0) + int(row["evidence"] != evidence)) if increasing else 0
        self.store.update(spec.strategy_id, evidence=evidence, confirmations=confirmations, preview=json.dumps(preview.payload()))
        plan = preview.plan
        if any(o.order_uuid != (order.order_uuid if order else None) and o.kdf_price == plan.kdf_price
               for o in all_orders):
            raise ValueError("prezzo finale già presente su un altro livello")
        self.minimum_volume(
            plan,
            for_new_auto=order is None,
            depth_reentry=order is None and float(row["last_write"]) > 0,
        )
        if not c.kdf.orders_enabled:
            self.store.update(spec.strategy_id, state="PREVIEW_ONLY", detail="scritture KDF disabilitate")
            return
        safety_reduction = order is not None and plan.kdf_volume < current_volume
        if safety_reduction:
            # Fixed mode raises instead of shrinking. Automatic reduction is
            # immediate and bypasses normal price/quantity hysteresis.
            changed = True
        else:
            changed = order is None or abs(plan.kdf_price / order.kdf_price - 1) >= spec.price_threshold or (plan.kdf_volume != current_volume and abs(plan.kdf_volume / current_volume - 1) >= spec.quantity_threshold)
        if not changed:
            self.store.update(spec.strategy_id, state="RUNNING", detail="prezzo/quantità entro soglia")
            return
        if not safety_reduction and ((increasing and confirmations < spec.confirmations) or self.clock() - row["last_write"] < float(spec.update_seconds)):
            self.store.update(spec.strategy_id, state="STABILIZING", detail="attesa snapshot distinti/intervallo minimo")
            return
        # Final controller checks aggregate coverage and KDF max_maker_vol.
        if self.stop.is_set():
            return
        if order:
            def intent(owned, minimum, old_price, old_max):
                self.store.begin_update(spec.strategy_id, owned.order_uuid,
                    old_price=old_price, old_volume=old_max,
                    new_price=plan.kdf_price, new_volume=plan.kdf_volume, min_volume=minimum)
                self.store.update(spec.strategy_id, state="WRITING", detail="richiesta KDF in corso")
            c.update_owned_quote(order.order_uuid, plan, before_write=intent)
        else:
            def intent():
                self.store.update(spec.strategy_id, state="WRITING", detail="richiesta KDF in corso")
            published = c.publish_quote(plan, before_write=intent)
            self.store.bind_order(spec.strategy_id, published.order_uuid)
            self.controller.ownership.bind_strategy(published.order_uuid, spec.strategy_id)
        self.store.update(spec.strategy_id, state="RUNNING", detail="", last_write=self.clock(), confirmations=0)
        if order:
            self.store.set_update_intent_state(spec.strategy_id, 'DONE')

    def start(self):
        def run():
            while not self.stop.wait(1):
                self.run_once()
        self.thread = threading.Thread(target=run, name="local-strategies", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
