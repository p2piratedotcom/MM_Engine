from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


ZERO = Decimal("0")
USDT_ASSETS = {"USDT", "USDT-BEP20"}


class EconomicLedgerStatus:
    """Read-only economic projection of signed swaps, hedge fills and fees."""

    def __init__(self, path: str | Path, *, recent_limit: int = 20) -> None:
        if recent_limit <= 0 or recent_limit > 100:
            raise ValueError("recent_limit must be in [1, 100]")
        self.path = Path(path)
        self.recent_limit = recent_limit

    def payload(
        self,
        *,
        mark_price_usdt: object | None = None,
        current_arrr_quantity: object | None = None,
        base_asset: str = "ARRR",
    ) -> dict[str, Any]:
        result = _empty_payload()
        if not self.path.is_file():
            result["reason"] = "journal economico non ancora creato"
            return result
        try:
            selected_base = base_asset.strip().upper()
            if not selected_base:
                raise ValueError("asset base mancante")
            mark = _optional_positive_decimal(
                mark_price_usdt, f"prezzo {selected_base}"
            )
            current_arrr = _optional_non_negative_decimal(
                current_arrr_quantity, f"inventario {selected_base} corrente"
            )
            connection = sqlite3.connect(
                f"file:{quote(str(self.path.resolve()), safe='/')}?mode=ro",
                uri=True,
                timeout=2,
            )
            connection.row_factory = sqlite3.Row
            try:
                return self._read(
                    connection, mark, current_arrr, base_asset=selected_base
                )
            finally:
                connection.close()
        except (sqlite3.Error, InvalidOperation, TypeError, ValueError) as exc:
            result["reason"] = f"ledger economico non leggibile: {str(exc)[:240]}"
            result["warnings"] = [result["reason"]]
            return result

    def _read(
        self,
        connection: sqlite3.Connection,
        mark_price: Decimal | None,
        current_arrr: Decimal | None,
        *,
        base_asset: str,
    ) -> dict[str, Any]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"hedges", "hedge_attempts", "received_hedge_events"}
        if not required.issubset(tables):
            result = _empty_payload()
            result["reason"] = "il file non contiene un journal economico compatibile"
            return result

        has_outcomes = "received_swap_outcomes" in tables
        has_fees = "economic_fees" in tables
        has_fills = "mexc_trade_fills" in tables
        has_baselines = "inventory_baselines" in tables
        has_adjustments = "inventory_adjustments" in tables
        outcome_join = (
            """
            LEFT JOIN received_swap_outcomes AS o USING (swap_uuid)
            """
            if has_outcomes
            else ""
        )
        outcome_columns = (
            "o.kdf_success, o.completed_at_ms, o.terminal_event"
            if has_outcomes
            else "NULL AS kdf_success, NULL AS completed_at_ms, NULL AS terminal_event"
        )
        rows = connection.execute(
            f"""
            SELECT h.swap_uuid, h.dex_side, h.hedge_side,
                   h.target_quantity, h.filled_quantity, h.state,
                   r.event_id, r.market_id, r.payload_json,
                   r.trigger_timestamp_ms, {outcome_columns}
            FROM hedges AS h
            JOIN received_hedge_events AS r USING (swap_uuid)
            {outcome_join}
            ORDER BY r.trigger_timestamp_ms DESC, r.event_id DESC
            """
        ).fetchall()

        cycles: list[dict[str, Any]] = []
        warnings: list[str] = []
        invalid_cycles = 0
        totals = {
            "cycles": 0,
            "pending": 0,
            "settled": 0,
            "failed": 0,
            "realized": 0,
            "open_exposure": 0,
            "mexc_fills_imported": 0,
            "cycles_with_verified_fills": 0,
            "cycles_missing_verified_fills": 0,
        }
        realized_gross = ZERO
        realized_fees = ZERO
        realized_net = ZERO
        unrealized = ZERO
        unrealized_complete = True
        unvalued_fee_count = 0

        for row in rows:
            try:
                attempts = connection.execute(
                    """
                    SELECT executed_quantity, quote_quantity
                    FROM hedge_attempts WHERE swap_uuid = ? ORDER BY sequence
                    """,
                    (row["swap_uuid"],),
                ).fetchall()
                fees = (
                    connection.execute(
                        """
                        SELECT venue, asset, amount, source
                        FROM economic_fees WHERE swap_uuid = ? ORDER BY fee_id
                        """,
                        (row["swap_uuid"],),
                    ).fetchall()
                    if has_fees
                    else []
                )
                fill_rows = (
                    connection.execute(
                        """
                        SELECT quantity, quote_quantity FROM mexc_trade_fills
                        WHERE swap_uuid = ? ORDER BY sequence, traded_at_ms, fill_key
                        """,
                        (row["swap_uuid"],),
                    ).fetchall()
                    if has_fills
                    else []
                )
                cycle = _cycle(row, attempts, fees, fill_rows, mark_price)
            except (json.JSONDecodeError, InvalidOperation, TypeError, ValueError) as exc:
                invalid_cycles += 1
                warnings.append(
                    f"swap {str(row['swap_uuid'])[:16]} escluso: {str(exc)[:160]}"
                )
                continue

            cycles.append(cycle)
            totals["cycles"] += 1
            totals["mexc_fills_imported"] += int(cycle["mexc_fill_count"])
            if cycle["mexc_fill_status"] == "VERIFIED":
                totals["cycles_with_verified_fills"] += 1
            if cycle["mexc_fill_status"] in {"MISSING", "INCONSISTENT"}:
                totals["cycles_missing_verified_fills"] += 1
            if cycle["kdf_outcome"] == "PENDING":
                totals["pending"] += 1
            else:
                totals["settled"] += 1
            if cycle["kdf_outcome"] == "FAILED":
                totals["failed"] += 1
            if cycle["pnl_kind"] == "REALIZED":
                totals["realized"] += 1
                realized_gross += _decimal(cycle["gross_pnl_usdt"], "gross P/L")
                realized_fees += _decimal(cycle["known_fees_usdt"], "commissioni")
                realized_net += _decimal(cycle["net_pnl_usdt"], "net P/L")
            elif cycle["pnl_kind"] == "UNREALIZED":
                totals["open_exposure"] += 1
                unrealized += _decimal(cycle["net_pnl_usdt"], "unrealized P/L")
                if cycle["mexc_fill_status"] in {"MISSING", "INCONSISTENT"}:
                    unrealized_complete = False
            elif cycle["status"] == "OPEN_EXPOSURE":
                totals["open_exposure"] += 1
                unrealized_complete = False
            unvalued_fee_count += int(cycle["unvalued_fee_count"])
            if cycle["pnl_kind"] == "UNAVAILABLE" and cycle["status"] == "OPEN_EXPOSURE":
                unrealized_complete = False

        if totals["open_exposure"] == 0:
            unrealized_complete = True
        if invalid_cycles:
            unrealized_complete = False
        net_complete = (
            unvalued_fee_count == 0
            and invalid_cycles == 0
            and totals["cycles_missing_verified_fills"] == 0
        )
        result = {
            "available": True,
            "reason": None,
            "base_asset": base_asset,
            "warnings": warnings[:20],
            "summary": {
                **totals,
                "invalid_cycles": invalid_cycles,
                "gross_realized_usdt": _text(realized_gross),
                "known_realized_fees_usdt": _text(realized_fees),
                "net_realized_usdt": _text(realized_net),
                "unrealized_usdt": (
                    _text(unrealized) if unrealized_complete else None
                ),
                "estimated_total_pnl_usdt": (
                    _text(realized_net + unrealized)
                    if unrealized_complete and net_complete
                    else None
                ),
                "net_realized_complete": net_complete,
                "unrealized_complete": unrealized_complete,
                "unvalued_fee_count": unvalued_fee_count,
            },
            "mark_price_usdt": _text(mark_price),
            "recent": cycles[: self.recent_limit],
            "notice": (
                "P/L realizzato solo dopo esito KDF firmato e copertura chiusa; "
                "fill mancanti e commissioni non valorizzabili rendono il totale incompleto"
            ),
            "inventory_pnl": _inventory_pnl(
                connection,
                has_baselines=has_baselines,
                has_adjustments=has_adjustments,
                has_fills=has_fills,
                has_fees=has_fees,
                cycle_rows=rows,
                current_arrr=current_arrr,
                mark_price=mark_price,
                base_asset=base_asset,
            ),
        }
        if not has_outcomes:
            result["warnings"].append(
                "journal precedente: gli esiti finali KDF saranno disponibili dai prossimi swap"
            )
        return result


def disabled_economic_ledger() -> dict[str, Any]:
    result = _empty_payload()
    result["reason"] = "journal Desktop non configurato"
    return result


def failed_economic_ledger(exc: Exception) -> dict[str, Any]:
    result = _empty_payload()
    result["reason"] = f"calcolo economico non riuscito: {str(exc)[:240]}"
    result["warnings"] = [result["reason"]]
    return result


def _cycle(
    row: sqlite3.Row,
    attempts: list[sqlite3.Row],
    fees: list[sqlite3.Row],
    fills: list[sqlite3.Row],
    mark_price: Decimal | None,
) -> dict[str, Any]:
    event = json.loads(str(row["payload_json"]))
    if not isinstance(event, Mapping):
        raise ValueError("evento firmato non strutturato")
    dex_side = str(row["dex_side"])
    if dex_side not in {"SELL_ARRR", "BUY_ARRR"}:
        raise ValueError("lato KDF non valido")
    base_asset = _required_text(
        event.get("hedge_base_asset", event.get("base_ticker", "ARRR")),
        "asset base",
    )
    arrr_quantity = _positive_decimal(
        event.get("base_quantity", event.get("arrr_quantity")),
        f"quantita {base_asset}",
    )
    quote_ticker = _required_text(event.get("quote_ticker"), "quote ticker")
    maker_amount = _positive_decimal(event.get("kdf_maker_amount"), "maker amount")
    taker_amount = _positive_decimal(event.get("kdf_taker_amount"), "taker amount")
    kdf_quote = taker_amount if dex_side == "SELL_ARRR" else maker_amount
    quote_usdt_rate = _historical_quote_usdt_rate(event, quote_ticker)
    executed = sum(
        (_non_negative(item["executed_quantity"], "executed quantity") for item in attempts),
        start=ZERO,
    )
    cex_quote = sum(
        (_non_negative(item["quote_quantity"], "quote quantity") for item in attempts),
        start=ZERO,
    )
    average_price = cex_quote / executed if executed > ZERO else None
    imported_quantity = sum(
        (_non_negative(item["quantity"], "fill quantity") for item in fills),
        start=ZERO,
    )
    imported_quote = sum(
        (_non_negative(item["quote_quantity"], "fill quote quantity") for item in fills),
        start=ZERO,
    )
    if executed == ZERO and not fills:
        mexc_fill_status = "NONE_REQUIRED"
    elif not fills:
        mexc_fill_status = "MISSING"
    elif _close_decimal(imported_quantity, executed) and _close_decimal(
        imported_quote, cex_quote
    ):
        mexc_fill_status = "VERIFIED"
    else:
        mexc_fill_status = "INCONSISTENT"

    direct_fees = ZERO
    arrr_fees = ZERO
    unvalued_fees: list[dict[str, str]] = []
    fee_rows: list[dict[str, str]] = []
    for fee in fees:
        amount = _positive_decimal(fee["amount"], "fee amount")
        asset = str(fee["asset"])
        fee_rows.append(
            {
                "venue": str(fee["venue"]),
                "asset": asset,
                "amount": _text(amount),
                "source": str(fee["source"]),
            }
        )
        if asset in USDT_ASSETS:
            direct_fees += amount
        elif asset == quote_ticker and quote_usdt_rate is not None:
            direct_fees += amount * quote_usdt_rate
        elif asset == base_asset:
            arrr_fees += amount
        else:
            unvalued_fees.append({"asset": asset, "amount": _text(amount)})

    raw_outcome = row["kdf_success"]
    kdf_outcome = (
        "PENDING"
        if raw_outcome is None
        else "SUCCEEDED"
        if bool(raw_outcome)
        else "FAILED"
    )
    residual_arrr: Decimal | None = None
    cash_usdt: Decimal | None = None
    if kdf_outcome != "PENDING":
        kdf_arrr_flow = ZERO
        kdf_quote_flow = ZERO
        if kdf_outcome == "SUCCEEDED":
            kdf_arrr_flow = -arrr_quantity if dex_side == "SELL_ARRR" else arrr_quantity
            if quote_usdt_rate is not None:
                kdf_quote_flow = (
                    kdf_quote * quote_usdt_rate
                    if dex_side == "SELL_ARRR"
                    else -kdf_quote * quote_usdt_rate
                )
        hedge_arrr_flow = executed if str(row["hedge_side"]) == "BUY" else -executed
        hedge_quote_flow = -cex_quote if str(row["hedge_side"]) == "BUY" else cex_quote
        residual_arrr = kdf_arrr_flow + hedge_arrr_flow - arrr_fees
        if quote_usdt_rate is not None:
            cash_usdt = kdf_quote_flow + hedge_quote_flow

    status = "PENDING_KDF"
    pnl_kind = "UNAVAILABLE"
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    if kdf_outcome != "PENDING" and residual_arrr is not None:
        if residual_arrr == ZERO:
            status = "CLOSED" if kdf_outcome == "SUCCEEDED" else "KDF_FAILED"
            if cash_usdt is not None:
                gross_pnl = cash_usdt
                net_pnl = cash_usdt - direct_fees
                pnl_kind = "REALIZED"
        else:
            status = "OPEN_EXPOSURE"
            if cash_usdt is not None and mark_price is not None:
                gross_pnl = cash_usdt + residual_arrr * mark_price
                net_pnl = gross_pnl - direct_fees
                pnl_kind = "UNREALIZED"

    if "hedge_legs" in event:
        # Legacy single-leg accounting must never label a cross hedge as
        # profit using a synthetic valuation of the received coin.
        status, pnl_kind = "BASKET_ACCOUNTING_PENDING", "UNAVAILABLE"
        gross_pnl = net_pnl = cash_usdt = residual_arrr = None
        mexc_fill_status = "MISSING"

    return {
        "swap_uuid": str(row["swap_uuid"]),
        "event_id": int(row["event_id"]),
        "market_id": str(row["market_id"]),
        "dex_side": dex_side,
        "hedge_side": str(row["hedge_side"]),
        "status": status,
        "kdf_outcome": kdf_outcome,
        "terminal_event": str(row["terminal_event"] or "-"),
        "arrr_quantity": _text(arrr_quantity),
        "base_ticker": base_asset,
        "base_quantity": _text(arrr_quantity),
        "quote_ticker": quote_ticker,
        "kdf_quote_quantity": _text(kdf_quote),
        "quote_usdt_rate": _text(quote_usdt_rate),
        "quote_usdt_symbol": event.get("quote_usdt_symbol"),
        "quote_usdt_side": event.get("quote_usdt_side"),
        "quote_usdt_observed_at_ms": event.get("quote_usdt_observed_at_ms"),
        "cex_executed_arrr": _text(executed),
        "cex_quote_quantity": _text(cex_quote),
        "cex_average_price": _text(average_price),
        "mexc_fill_count": len(fills),
        "mexc_fill_status": mexc_fill_status,
        "residual_arrr": _text(residual_arrr),
        "residual_base": _text(residual_arrr),
        "known_fees_usdt": _text(direct_fees),
        "unvalued_fee_count": len(unvalued_fees),
        "unvalued_fees": unvalued_fees,
        "fees": fee_rows,
        "gross_pnl_usdt": _text(gross_pnl),
        "net_pnl_usdt": _text(net_pnl),
        "pnl_kind": pnl_kind,
        "completed_at_ms": (
            int(row["completed_at_ms"])
            if row["completed_at_ms"] is not None
            else None
        ),
    }


def _empty_payload() -> dict[str, Any]:
    return {
        "available": False,
        "reason": "dati economici non ancora disponibili",
        "warnings": [],
        "summary": {
            "cycles": 0,
            "pending": 0,
            "settled": 0,
            "failed": 0,
            "realized": 0,
            "open_exposure": 0,
            "mexc_fills_imported": 0,
            "cycles_with_verified_fills": 0,
            "cycles_missing_verified_fills": 0,
            "invalid_cycles": 0,
            "gross_realized_usdt": None,
            "known_realized_fees_usdt": None,
            "net_realized_usdt": None,
            "unrealized_usdt": None,
            "estimated_total_pnl_usdt": None,
            "net_realized_complete": False,
            "unrealized_complete": False,
            "unvalued_fee_count": 0,
        },
        "mark_price_usdt": None,
        "recent": [],
        "notice": "nessun P/L calcolato",
        "inventory_pnl": {
            "available": False,
            "reason": "costo iniziale inventario non registrato",
            "baseline_key": None,
            "quantity": None,
            "current_quantity": None,
            "total_cost_usdt": None,
            "average_cost_usdt": None,
            "mark_price_usdt": None,
            "market_value_usdt": None,
            "unrealized_usdt": None,
            "baseline_quantity": None,
            "baseline_total_cost_usdt": None,
            "roll_forward_complete": False,
            "swaps_applied": 0,
            "adjustments_applied": 0,
            "operations": [],
        },
    }


@dataclass(slots=True)
class _InventoryCostState:
    quantity: Decimal
    total_cost: Decimal
    operations: list[dict[str, Any]] = field(default_factory=list)

    def acquire(
        self,
        quantity: Decimal,
        cost: Decimal,
        *,
        at_ms: int,
        source: str,
    ) -> None:
        if quantity <= ZERO or cost < ZERO:
            raise ValueError("acquisizione inventario non valida")
        self.quantity += quantity
        self.total_cost += cost
        self.operations.append(
            {
                "at_ms": at_ms,
                "kind": "ACQUIRE",
                "source": source,
                "quantity": _text(quantity),
                "cost_usdt": _text(cost),
            }
        )

    def dispose(self, quantity: Decimal, *, at_ms: int, source: str) -> None:
        if quantity <= ZERO:
            raise ValueError("uscita inventario non valida")
        tolerance = max(
            Decimal("0.00000001"), self.quantity * Decimal("0.0000000001")
        )
        if quantity > self.quantity + tolerance:
            raise ValueError(
                f"uscita asset {quantity} superiore all'inventario {self.quantity}"
            )
        removed = min(quantity, self.quantity)
        removed_cost = (
            self.total_cost if _close_decimal(removed, self.quantity)
            else (self.total_cost / self.quantity) * removed
        )
        self.quantity -= removed
        self.total_cost -= removed_cost
        if self.quantity <= tolerance:
            self.quantity = ZERO
            self.total_cost = ZERO
        self.operations.append(
            {
                "at_ms": at_ms,
                "kind": "DISPOSE",
                "source": source,
                "quantity": _text(removed),
                "cost_usdt": _text(removed_cost),
            }
        )


def _inventory_pnl(
    connection: sqlite3.Connection,
    *,
    has_baselines: bool,
    has_adjustments: bool,
    has_fills: bool,
    has_fees: bool,
    cycle_rows: list[sqlite3.Row],
    current_arrr: Decimal | None,
    mark_price: Decimal | None,
    base_asset: str,
) -> dict[str, Any]:
    result = _empty_payload()["inventory_pnl"]
    result["asset"] = base_asset
    if not has_baselines:
        return result
    try:
        row = connection.execute(
            """
            SELECT * FROM inventory_baselines WHERE asset = ?
            ORDER BY observed_at_ms DESC, baseline_key DESC LIMIT 1
            """,
            (base_asset,),
        ).fetchone()
        if row is None:
            return result
        quantity = _positive_decimal(
            row["quantity"], f"quantita baseline {base_asset}"
        )
        total_cost = _non_negative(
            row["total_cost_usdt"], f"costo baseline {base_asset}"
        )
        baseline_at = int(row["observed_at_ms"])
        result.update(
            {
                "baseline_key": str(row["baseline_key"]),
                "baseline_quantity": _text(quantity),
                "baseline_total_cost_usdt": _text(total_cost),
                "observed_at_ms": baseline_at,
                "source": str(row["source"]),
                "note": str(row["note"]) if row["note"] is not None else None,
            }
        )
        state, swaps_applied, adjustments_applied = _roll_inventory_forward(
            connection,
            baseline_at_ms=baseline_at,
            quantity=quantity,
            total_cost=total_cost,
            cycle_rows=cycle_rows,
            has_adjustments=has_adjustments,
            has_fills=has_fills,
            has_fees=has_fees,
            base_asset=base_asset,
        )
    except (json.JSONDecodeError, InvalidOperation, TypeError, ValueError) as exc:
        result["reason"] = f"roll-forward non disponibile: {str(exc)[:220]}"
        return result

    average_cost = (
        state.total_cost / state.quantity if state.quantity > ZERO else None
    )
    result.update(
        {
            "quantity": _text(state.quantity),
            "total_cost_usdt": _text(state.total_cost),
            "average_cost_usdt": _text(average_cost),
            "roll_forward_complete": True,
            "swaps_applied": swaps_applied,
            "adjustments_applied": adjustments_applied,
            "operations": state.operations[-20:],
        }
    )
    if current_arrr is None:
        result["reason"] = f"saldo {base_asset} corrente non disponibile"
        return result
    result["current_quantity"] = _text(current_arrr)
    tolerance = max(
        Decimal("0.00000001"), state.quantity * Decimal("0.0000000001")
    )
    if abs(current_arrr - state.quantity) > tolerance:
        result["reason"] = (
            f"saldo {base_asset} corrente non coincide con la quantita calcolata dal "
            "roll-forward"
        )
        return result
    if mark_price is None:
        result["reason"] = f"prezzo corrente {base_asset}/USDT non disponibile"
        return result
    market_value = current_arrr * mark_price
    result.update(
        {
            "available": True,
            "reason": None,
            "mark_price_usdt": _text(mark_price),
            "market_value_usdt": _text(market_value),
            "unrealized_usdt": _text(market_value - state.total_cost),
        }
    )
    return result


def _roll_inventory_forward(
    connection: sqlite3.Connection,
    *,
    baseline_at_ms: int,
    quantity: Decimal,
    total_cost: Decimal,
    cycle_rows: list[sqlite3.Row],
    has_adjustments: bool,
    has_fills: bool,
    has_fees: bool,
    base_asset: str,
) -> tuple[_InventoryCostState, int, int]:
    state = _InventoryCostState(quantity=quantity, total_cost=total_cost)
    cycles: list[dict[str, Any]] = []
    for row in cycle_rows:
        raw_event = json.loads(str(row["payload_json"]))
        if not isinstance(raw_event, Mapping):
            raise ValueError("evento swap non strutturato")
        event_base = str(
            raw_event.get(
                "hedge_base_asset", raw_event.get("base_ticker", "ARRR")
            )
        ).upper()
        if event_base != base_asset:
            continue
        trigger_at = int(row["trigger_timestamp_ms"])
        completed_raw = row["completed_at_ms"]
        fill_window = (
            connection.execute(
                """
                SELECT MIN(traded_at_ms), MAX(traded_at_ms)
                FROM mexc_trade_fills WHERE swap_uuid = ?
                """,
                (row["swap_uuid"],),
            ).fetchone()
            if has_fills
            else (None, None)
        )
        fill_min = fill_window[0]
        fill_max = fill_window[1]
        started_at = min(trigger_at, int(fill_min)) if fill_min is not None else trigger_at
        if completed_raw is None:
            relation = "durante" if started_at <= baseline_at_ms else "dopo"
            raise ValueError(
                f"swap {str(row['swap_uuid'])[:16]} pendente {relation} la baseline"
            )
        completed_at = int(completed_raw)
        ended_at = max(completed_at, int(fill_max)) if fill_max is not None else completed_at
        if ended_at < started_at:
            raise ValueError("intervallo temporale swap non valido")
        if ended_at <= baseline_at_ms:
            continue
        if started_at <= baseline_at_ms:
            raise ValueError(
                f"swap {str(row['swap_uuid'])[:16]} attraversa la baseline"
            )
        cycles.append(
            {
                "time": ended_at,
                "start": started_at,
                "row": row,
                "key": str(row["swap_uuid"]),
            }
        )

    adjustments = (
        connection.execute(
            """
            SELECT * FROM inventory_adjustments
            WHERE asset = ? AND occurred_at_ms > ?
            ORDER BY occurred_at_ms, adjustment_key
            """,
            (base_asset, baseline_at_ms),
        ).fetchall()
        if has_adjustments
        else []
    )
    for adjustment in adjustments:
        at_ms = int(adjustment["occurred_at_ms"])
        for cycle in cycles:
            if int(cycle["start"]) <= at_ms <= int(cycle["time"]):
                raise ValueError(
                    "movimento inventario sovrapposto a uno swap; serve una nuova baseline"
                )

    events: list[tuple[int, str, Any]] = [
        (int(cycle["time"]), f"swap:{cycle['key']}", cycle["row"])
        for cycle in cycles
    ]
    events.extend(
        (
            int(row["occurred_at_ms"]),
            f"adjustment:{row['adjustment_key']}",
            row,
        )
        for row in adjustments
    )
    events.sort(key=lambda item: (item[0], item[1]))

    swaps_applied = 0
    adjustments_applied = 0
    for at_ms, key, row in events:
        if key.startswith("swap:"):
            _apply_inventory_cycle(
                connection,
                state,
                row,
                at_ms=at_ms,
                has_fills=has_fills,
                has_fees=has_fees,
                base_asset=base_asset,
            )
            swaps_applied += 1
            continue
        kind = str(row["kind"])
        movement_quantity = _positive_decimal(
            row["quantity"], "quantita movimento inventario"
        )
        source = f"MOVEMENT:{str(row['adjustment_key'])}"
        if kind == "ACQUIRE":
            movement_cost = _non_negative(
                row["total_cost_usdt"], "costo movimento inventario"
            )
            state.acquire(movement_quantity, movement_cost, at_ms=at_ms, source=source)
        elif kind == "DISPOSE":
            if row["total_cost_usdt"] is not None:
                raise ValueError("uscita inventario con costo manuale non valida")
            state.dispose(movement_quantity, at_ms=at_ms, source=source)
        else:
            raise ValueError("tipo movimento inventario non valido")
        adjustments_applied += 1
    return state, swaps_applied, adjustments_applied


def _apply_inventory_cycle(
    connection: sqlite3.Connection,
    state: _InventoryCostState,
    row: sqlite3.Row,
    *,
    at_ms: int,
    has_fills: bool,
    has_fees: bool,
    base_asset: str,
) -> None:
    event = json.loads(str(row["payload_json"]))
    if not isinstance(event, Mapping):
        raise ValueError("evento swap non strutturato")
    if "hedge_legs" in event:
        raise ValueError("contabilità multi-gamba: fill e commissioni da riconciliare prima del roll-forward")
    swap_uuid = str(row["swap_uuid"])
    attempts = connection.execute(
        """
        SELECT executed_quantity, quote_quantity FROM hedge_attempts
        WHERE swap_uuid = ? ORDER BY sequence
        """,
        (swap_uuid,),
    ).fetchall()
    executed = sum(
        (_non_negative(item["executed_quantity"], "quantita copertura") for item in attempts),
        start=ZERO,
    )
    quote_executed = sum(
        (_non_negative(item["quote_quantity"], "controvalore copertura") for item in attempts),
        start=ZERO,
    )
    fills = (
        connection.execute(
            """
            SELECT side, quantity, quote_quantity, commission, commission_asset,
                   traded_at_ms
            FROM mexc_trade_fills WHERE swap_uuid = ?
            ORDER BY sequence, traded_at_ms, fill_key
            """,
            (swap_uuid,),
        ).fetchall()
        if has_fills
        else []
    )
    fill_quantity = sum(
        (_non_negative(item["quantity"], "quantita fill") for item in fills),
        start=ZERO,
    )
    fill_quote = sum(
        (_non_negative(item["quote_quantity"], "controvalore fill") for item in fills),
        start=ZERO,
    )
    if executed == ZERO:
        if fills:
            raise ValueError(f"swap {swap_uuid[:16]} ha fill MEXC inattesi")
    elif not fills or not _close_decimal(fill_quantity, executed) or not _close_decimal(
        fill_quote, quote_executed
    ):
        raise ValueError(f"swap {swap_uuid[:16]} senza fill MEXC verificati")
    expected_hedge_side = str(row["hedge_side"])
    if any(str(fill["side"]) != expected_hedge_side for fill in fills):
        raise ValueError(f"swap {swap_uuid[:16]} con lato fill MEXC incoerente")

    dex_side = str(row["dex_side"])
    if dex_side not in {"SELL_ARRR", "BUY_ARRR"}:
        raise ValueError("lato KDF inventario non valido")
    kdf_success = bool(row["kdf_success"])
    event_base = str(
        event.get("hedge_base_asset", event.get("base_ticker", "ARRR"))
    ).upper()
    if event_base != base_asset:
        raise ValueError("asset base dello swap non coerente con la baseline")
    arrr_quantity = _positive_decimal(
        event.get("base_quantity", event.get("arrr_quantity")),
        f"quantita {base_asset} swap",
    )
    quote_ticker = _required_text(event.get("quote_ticker"), "quote ticker")
    maker_amount = _positive_decimal(event.get("kdf_maker_amount"), "maker amount")
    taker_amount = _positive_decimal(event.get("kdf_taker_amount"), "taker amount")
    kdf_quote = taker_amount if dex_side == "SELL_ARRR" else maker_amount
    quote_usdt_rate = _historical_quote_usdt_rate(event, quote_ticker)
    if kdf_success and quote_usdt_rate is None:
        raise ValueError(
            f"roll-forward dello swap {quote_ticker} richiede il tasso storico quote/USDT"
        )

    kdf_arrr_fee = ZERO
    kdf_usdt_fee = ZERO
    kdf_quote_fee = ZERO
    other_kdf_fees: list[str] = []
    if has_fees:
        fee_rows = connection.execute(
            """
            SELECT asset, amount FROM economic_fees
            WHERE swap_uuid = ? AND venue = 'KDF'
            """,
            (swap_uuid,),
        ).fetchall()
        for fee in fee_rows:
            amount = _positive_decimal(fee["amount"], "commissione KDF")
            asset = str(fee["asset"])
            if asset == base_asset:
                kdf_arrr_fee += amount
            elif asset == quote_ticker:
                kdf_quote_fee += amount
            elif asset in USDT_ASSETS:
                kdf_usdt_fee += amount
            else:
                other_kdf_fees.append(asset)

    if kdf_success and dex_side == "SELL_ARRR":
        state.dispose(
            arrr_quantity + kdf_arrr_fee,
            at_ms=at_ms,
            source=f"KDF:{swap_uuid}",
        )
        _apply_mexc_inventory_fills(
            state,
            fills,
            at_ms=at_ms,
            swap_uuid=swap_uuid,
            base_asset=base_asset,
        )
    elif kdf_success:
        _apply_mexc_inventory_fills(
            state,
            fills,
            at_ms=at_ms,
            swap_uuid=swap_uuid,
            base_asset=base_asset,
        )
        if other_kdf_fees:
            raise ValueError(
                "costo acquisizione KDF incompleto: fee non valorizzata in "
                + ", ".join(sorted(set(other_kdf_fees)))
            )
        net_quantity = arrr_quantity - kdf_arrr_fee
        assert quote_usdt_rate is not None
        state.acquire(
            net_quantity,
            (kdf_quote + kdf_quote_fee) * quote_usdt_rate + kdf_usdt_fee,
            at_ms=at_ms,
            source=f"KDF:{swap_uuid}",
        )
    else:
        _apply_mexc_inventory_fills(
            state,
            fills,
            at_ms=at_ms,
            swap_uuid=swap_uuid,
            base_asset=base_asset,
        )


def _apply_mexc_inventory_fills(
    state: _InventoryCostState,
    fills: list[sqlite3.Row],
    *,
    at_ms: int,
    swap_uuid: str,
    base_asset: str,
) -> None:
    if not fills:
        return
    side = str(fills[0]["side"])
    quantity = ZERO
    cost = ZERO
    arrr_commission = ZERO
    for fill in fills:
        if str(fill["side"]) != side:
            raise ValueError("fill MEXC con lati misti")
        quantity += _positive_decimal(fill["quantity"], "quantita fill MEXC")
        quote = _positive_decimal(fill["quote_quantity"], "controvalore fill MEXC")
        commission = _non_negative(fill["commission"], "commissione fill MEXC")
        raw_asset = fill["commission_asset"]
        asset = str(raw_asset) if raw_asset is not None else None
        if commission > ZERO and not asset:
            raise ValueError("asset commissione MEXC mancante")
        if asset == base_asset:
            arrr_commission += commission
        if side == "BUY":
            cost += quote
            if asset in USDT_ASSETS:
                cost += commission
            elif commission > ZERO and asset != base_asset:
                raise ValueError(
                    f"costo acquisizione MEXC incompleto: fee {asset} non valorizzata"
                )
    source = f"MEXC:{swap_uuid}"
    if side == "BUY":
        state.acquire(quantity - arrr_commission, cost, at_ms=at_ms, source=source)
    elif side == "SELL":
        state.dispose(quantity + arrr_commission, at_ms=at_ms, source=source)
    else:
        raise ValueError("lato fill MEXC non valido")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} mancante")
    return value


def _historical_quote_usdt_rate(
    event: Mapping[str, Any], quote_ticker: str
) -> Decimal | None:
    if quote_ticker in USDT_ASSETS:
        return Decimal("1")
    raw = event.get("quote_usdt_rate")
    if raw in (None, ""):
        return None
    return _positive_decimal(raw, "tasso storico quote/USDT")


def _decimal(value: object, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{name} non finito")
    return parsed


def _non_negative(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed < ZERO:
        raise ValueError(f"{name} negativo")
    return parsed


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed <= ZERO:
        raise ValueError(f"{name} non positivo")
    return parsed


def _optional_positive_decimal(value: object | None, name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    return _positive_decimal(value, name)


def _optional_non_negative_decimal(
    value: object | None, name: str
) -> Decimal | None:
    if value in (None, ""):
        return None
    return _non_negative(value, name)


def _close_decimal(left: Decimal, right: Decimal) -> bool:
    tolerance = max(Decimal("0.000000000001"), abs(right) * Decimal("0.00000001"))
    return abs(left - right) <= tolerance


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    normalized = value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    return format(normalized.normalize(), "f")
