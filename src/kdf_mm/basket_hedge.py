"""Durable two-leg Spot hedge execution, with no blind resend after a timeout.

Both legs are pre-funded. They are not an atomic CEX transaction: partial or
uncertain execution stops new quoting and requires reconciliation.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from .journal import HedgeState
from .models import HedgeSide
from .mexc_test_connector import MexcTestConnector
from .pricing import walk_book
from .strategy import MAX_HEDGE_DUST_USDT

D = Decimal


class BasketHedgeExecutor:
    def __init__(self, *, journal, mexc, fee: Decimal, impact: Decimal, depth: int,
                 precision_limit_usdt: Decimal = MAX_HEDGE_DUST_USDT) -> None:
        self.journal, self.mexc = journal, mexc
        self.fee, self.impact, self.depth = fee, min(impact, D(".01")), depth
        if not precision_limit_usdt.is_finite() or not D(0) <= precision_limit_usdt <= D(".05"):
            raise ValueError("invalid precision residual limit")
        self.precision_limit_usdt = precision_limit_usdt
        with journal._lock, journal.connection:
            journal.connection.execute("""CREATE TABLE IF NOT EXISTS basket_legs (
                swap_uuid TEXT NOT NULL, leg INTEGER NOT NULL, plan TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, PRIMARY KEY(swap_uuid,leg))""")

    def rows(self, swap: str) -> list[dict[str, Any]]:
        with self.journal._lock:
            rows = self.journal.connection.execute("SELECT * FROM basket_legs WHERE swap_uuid=? ORDER BY leg", (swap,)).fetchall()
        return [{**dict(row), "plan": json.loads(row["plan"]), "result": json.loads(row["result"] or "null")} for row in rows]

    def _save(self, swap: str, index: int, state: str, result=None) -> None:
        with self.journal._lock, self.journal.connection:
            self.journal.connection.execute("UPDATE basket_legs SET state=?,result=? WHERE swap_uuid=? AND leg=?",
                                            (state, json.dumps(result), swap, index))

    def _prepare(self, received) -> None:
        raw = received.event["hedge_legs"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
            raise ValueError("numero di gambe hedge non valido")
        self.mexc.synchronize_time(max_round_trip_ms=2000)
        api_symbols = self.mexc.self_symbols()
        balances = MexcTestConnector._account_balances(self.mexc.account())
        required: dict[str, Decimal] = {}
        plans = []
        seen = set()
        for index, leg in enumerate(raw):
            side = HedgeSide(leg["side"])
            symbol, asset = str(leg["symbol"]), str(leg["asset"])
            if symbol in seen or symbol != asset + "USDT":
                raise ValueError("route hedge duplicata o non valida")
            seen.add(symbol)
            exact = D(str(leg["quantity"]))
            if not exact.is_finite() or exact <= 0:
                raise ValueError("quantità hedge non valida")
            rules = self.mexc.symbol_rules(symbol)
            MexcTestConnector._validate_rules(rules, side=side, expected_symbol=symbol,
                                             expected_base_asset=asset, expected_quote_asset="USDT")
            MexcTestConnector._validate_api_symbols(api_symbols, symbol)
            book = self.mexc.order_book(symbol, limit=self.depth)
            levels = book.asks if side is HedgeSide.BUY else book.bids
            if not levels:
                raise ValueError("book hedge vuoto")
            quantity = (exact / rules.quantity_step).to_integral_value(
                rounding=ROUND_CEILING if side is HedgeSide.BUY else ROUND_FLOOR) * rules.quantity_step
            # Explicit bounded precision residual, never concealed as profit.
            dust = quantity - exact
            if quantity <= 0 or abs(dust) * levels[0].price > self.precision_limit_usdt:
                raise ValueError(f"residuo di precisione oltre {self.precision_limit_usdt} USDT: verifica manuale")
            boundary = levels[0].price * (1 + self.impact if side is HedgeSide.BUY else 1 - self.impact)
            price = (boundary / rules.price_step).to_integral_value(
                rounding=ROUND_FLOOR if side is HedgeSide.BUY else ROUND_CEILING) * rules.price_step
            walk = walk_book(levels, quantity)
            if not walk.complete or walk.limit_price is None or (
                walk.limit_price > price if side is HedgeSide.BUY else walk.limit_price < price
            ):
                raise ValueError("profondità MEXC insufficiente entro 1%")
            notional = price * quantity
            if notional < rules.min_quote_amount or (rules.max_quote_amount and notional > rules.max_quote_amount):
                raise ValueError("controvalore fuori dai limiti MEXC")
            fund = "USDT" if side is HedgeSide.BUY else asset
            amount = (notional if side is HedgeSide.BUY else quantity) * (1 + self.fee)
            required[fund] = required.get(fund, D(0)) + amount
            plans.append({"symbol": symbol, "asset": asset, "side": side.value, "quantity": str(quantity),
                          "price": str(price), "exact_quantity": str(exact), "dust": str(dust),
                          "client_order_id": "kdfb" + hashlib.sha256(f"{received.swap_uuid}:{index}".encode()).hexdigest()[:28]})
        if any(balances.get(asset, D(0)) < amount for asset, amount in required.items()):
            raise ValueError("fondi Spot insufficienti per tutte le gambe")
        for plan in plans:
            self.mexc.test_limit_order(**self._args(plan))
        with self.journal._lock, self.journal.connection:
            for index, plan in enumerate(plans):
                self.journal.connection.execute("INSERT INTO basket_legs VALUES(?,?,?,'READY',NULL)",
                                                (received.swap_uuid, index, json.dumps(plan, sort_keys=True)))

    @staticmethod
    def _args(plan):
        return {"symbol": plan["symbol"], "side": HedgeSide(plan["side"]),
                "quantity": D(plan["quantity"]), "price": D(plan["price"]),
                "client_order_id": plan["client_order_id"]}

    def process(self, received) -> list[dict[str, Any]]:
        swap = received.swap_uuid
        hedge = self.journal.get(swap)
        try:
            if not self.rows(swap):
                self._prepare(received)
            if hedge.state is HedgeState.RESERVED:
                self.journal.transition(swap, HedgeState.HEDGE_READY)
            if self.journal.get(swap).state is HedgeState.HEDGE_READY:
                self.journal.transition(swap, HedgeState.SUBMITTING)
            for row in self.rows(swap):
                if row["state"] == "FILLED":
                    continue
                if row["state"] == "PARTIAL":
                    raise ValueError("gamba parzialmente eseguita: nessun reinvio automatico")
                plan, index = row["plan"], row["leg"]
                if row["state"] == "READY":
                    # A restart or slow preceding leg must not turn an old
                    # aggressive limit into an unbounded market-impact order.
                    book = self.mexc.order_book(plan["symbol"], limit=self.depth)
                    side = HedgeSide(plan["side"])
                    levels = book.asks if side is HedgeSide.BUY else book.bids
                    if not levels:
                        raise ValueError("book vuoto prima dell'invio")
                    current_boundary = levels[0].price * (1 + self.impact if side is HedgeSide.BUY else 1 - self.impact)
                    rules = self.mexc.symbol_rules(plan["symbol"])
                    limit = min(D(plan["price"]), current_boundary) if side is HedgeSide.BUY else max(D(plan["price"]), current_boundary)
                    limit = (limit / rules.price_step).to_integral_value(rounding=ROUND_FLOOR if side is HedgeSide.BUY else ROUND_CEILING) * rules.price_step
                    walk = walk_book(levels, D(plan["quantity"]))
                    if not walk.complete or walk.limit_price is None or (walk.limit_price > limit if side is HedgeSide.BUY else walk.limit_price < limit):
                        raise ValueError("liquidità cambiata prima dell'invio; limite originale non allargato")
                    plan = {**plan, "price": str(limit)}
                    with self.journal._lock, self.journal.connection:
                        self.journal.connection.execute("UPDATE basket_legs SET plan=? WHERE swap_uuid=? AND leg=?", (json.dumps(plan, sort_keys=True), swap, index))
                    # Durable intent BEFORE network I/O. Recovery queries this
                    # exact ID, and never interprets 'not found' as safe resend.
                    self._save(swap, index, "SUBMITTING")
                    self.mexc.place_limit_order(**self._args(plan))
                query = {"symbol": plan["symbol"], "client_order_id": plan["client_order_id"]}
                result = self.mexc.query_order(**query)
                if str(result.get("status")) in {"NEW", "PARTIALLY_FILLED"}:
                    self.mexc.cancel_order(**query)
                    result = self.mexc.query_order(**query)
                if result.get("symbol") != plan["symbol"] or result.get("side") != plan["side"] or result.get("clientOrderId") != plan["client_order_id"]:
                    raise ValueError("identità risposta MEXC non valida")
                executed = D(str(result["executedQty"]))
                quote = D(str(result["cummulativeQuoteQty"]))
                if not executed.is_finite() or not quote.is_finite() or executed < 0 or quote < 0 or executed > D(plan["quantity"]):
                    raise ValueError("quantità eseguita MEXC non valida")
                if executed > 0 and (quote > executed * D(plan["price"]) if plan["side"] == "BUY" else quote < executed * D(plan["price"])):
                    raise ValueError("prezzo eseguito MEXC oltre il limite registrato")
                status = str(result.get("status"))
                if status == "FILLED" and executed == D(plan["quantity"]):
                    self._save(swap, index, "FILLED", dict(result))
                elif status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
                    self._save(swap, index, "PARTIAL", dict(result))
                    raise ValueError("copertura incompleta; residuo registrato, richiesta verifica")
                else:
                    raise RuntimeError("stato ordine MEXC ancora incerto")
            target = self.journal.get(swap).target_quantity
            self.journal.transition(swap, HedgeState.FILLED, filled_quantity=target)
        except Exception as exc:
            current = self.journal.get(swap).state
            uncertain = any(row["state"] == "SUBMITTING" for row in self.rows(swap))
            if current in {HedgeState.SUBMITTING, HedgeState.UNKNOWN, HedgeState.SUBMITTED}:
                self.journal.transition(swap, HedgeState.UNKNOWN if uncertain else HedgeState.FAILED, error=str(exc))
            elif current in {HedgeState.RESERVED, HedgeState.HEDGE_READY}:
                self.journal.transition(swap, HedgeState.REVIEW_REQUIRED, error=str(exc))
            else:
                raise
        return [{"swap_uuid": swap, "action": "BASKET_HEDGE", "state": self.journal.get(swap).state.value,
                 "legs": self.rows(swap)}]
