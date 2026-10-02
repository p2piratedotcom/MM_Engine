from __future__ import annotations

import json

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


ATTENTION_STATES = {"REVIEW_REQUIRED", "UNKNOWN", "FAILED"}
IN_PROGRESS_STATES = {"TESTING", "SUBMITTING", "SUBMITTED"}
PENDING_VALIDATION_STATES = {"RESERVED", "HEDGE_READY"}


class DesktopJournalStatus:
    """Builds a read-only, display-safe view of the Desktop hedge journal."""

    def __init__(self, path: str | Path, *, recent_limit: int = 8) -> None:
        if recent_limit <= 0 or recent_limit > 100:
            raise ValueError("recent_limit must be in [1, 100]")
        self.path = Path(path)
        self.recent_limit = recent_limit

    def payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "available": False,
                "reason": "journal Desktop non ancora creato",
                "delivery": _empty_delivery(),
                "hedges": _empty_hedges(),
                "alarms": [],
                "recent": [],
            }
        try:
            connection = sqlite3.connect(
                f"file:{quote(str(self.path.resolve()), safe='/')}?mode=ro",
                uri=True,
                timeout=2,
            )
            connection.row_factory = sqlite3.Row
            try:
                return self._read(connection)
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return {
                "available": False,
                "reason": f"journal Desktop non leggibile: {exc}",
                "delivery": _empty_delivery(),
                "hedges": _empty_hedges(),
                "alarms": [],
                "recent": [],
            }

    def _read(self, connection: sqlite3.Connection) -> dict[str, Any]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"hedges", "hedge_attempts", "received_hedge_events"}
        if not required.issubset(tables):
            return {
                "available": False,
                "reason": "il file non e un journal Desktop compatibile",
                "delivery": _empty_delivery(),
                "hedges": _empty_hedges(),
                "alarms": [],
                "recent": [],
            }

        has_outcomes = "received_swap_outcomes" in tables
        delivery_source = (
            """
            SELECT event_id, acknowledged FROM received_hedge_events
            UNION ALL
            SELECT event_id, acknowledged FROM received_swap_outcomes
            """
            if has_outcomes
            else "SELECT event_id, acknowledged FROM received_hedge_events"
        )
        delivery_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN acknowledged = 1 THEN 1 ELSE 0 END), 0)
                       AS acknowledged,
                   COALESCE(SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END), 0)
                       AS pending_acknowledgement,
                   COALESCE(MAX(event_id), 0) AS cursor
            FROM ({delivery_source})
            """
        ).fetchone()
        state_rows = connection.execute(
            "SELECT state, COUNT(*) AS count FROM hedges GROUP BY state"
        ).fetchall()
        by_state = {str(row["state"]): int(row["count"]) for row in state_rows}
        outcome_join = (
            "LEFT JOIN received_swap_outcomes AS o USING (swap_uuid)"
            if has_outcomes
            else ""
        )
        outcome_columns = (
            "o.kdf_success, o.terminal_event"
            if has_outcomes
            else "NULL AS kdf_success, NULL AS terminal_event"
        )
        recent_rows = connection.execute(
            f"""
            SELECT h.swap_uuid, h.hedge_side, h.target_quantity,
                   h.filled_quantity, h.state, h.last_error, h.updated_at,
                   r.event_id, r.market_id, r.hedge_symbol, r.acknowledged,
                   r.client_order_id, r.payload_json, a.status AS attempt_status,
                   a.limit_price, {outcome_columns}
            FROM hedges AS h
            LEFT JOIN received_hedge_events AS r USING (swap_uuid)
            {outcome_join}
            LEFT JOIN hedge_attempts AS a
              ON a.id = (
                  SELECT newest.id FROM hedge_attempts AS newest
                  WHERE newest.swap_uuid = h.swap_uuid
                  ORDER BY newest.sequence DESC LIMIT 1
              )
            ORDER BY h.updated_at DESC, r.event_id DESC
            LIMIT ?
            """,
            (self.recent_limit,),
        ).fetchall()

        delivery = {
            "total": int(delivery_row["total"]),
            "acknowledged": int(delivery_row["acknowledged"]),
            "pending_acknowledgement": int(
                delivery_row["pending_acknowledgement"]
            ),
            "cursor": int(delivery_row["cursor"]),
        }
        hedges = {
            "total": sum(by_state.values()),
            "attention": _count_states(by_state, ATTENTION_STATES),
            "in_progress": _count_states(by_state, IN_PROGRESS_STATES),
            "pending_validation": _count_states(
                by_state, PENDING_VALIDATION_STATES
            ),
            "validated": by_state.get("TEST_VALIDATED", 0),
            "by_state": by_state,
        }
        alarms = _alarms(delivery, by_state, recent_rows)
        recent = [
            {
                "swap_uuid": str(row["swap_uuid"]),
                "event_id": int(row["event_id"]) if row["event_id"] else None,
                "market_id": str(row["market_id"] or "-"),
                "hedge_symbol": str(row["hedge_symbol"] or "-"),
                "hedge_side": str(row["hedge_side"]),
                "target_quantity": str(row["target_quantity"]),
                "filled_quantity": str(row["filled_quantity"]),
                "state": str(row["state"]),
                "last_error": str(row["last_error"]) if row["last_error"] else None,
                "acknowledged": bool(row["acknowledged"]),
                "client_order_id": str(row["client_order_id"] or "-"),
                "attempt_status": str(row["attempt_status"] or "-"),
                "limit_price": str(row["limit_price"]) if row["limit_price"] else None,
                "kdf_outcome": (
                    "PENDING"
                    if row["kdf_success"] is None
                    else "SUCCEEDED"
                    if bool(row["kdf_success"])
                    else "FAILED"
                ),
                "terminal_event": str(row["terminal_event"] or "-"),
                "cex": _event_cex(row["payload_json"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in recent_rows
        ]
        if "basket_legs" in tables:
            for item in recent:
                legs = connection.execute("SELECT leg,plan,state,result FROM basket_legs WHERE swap_uuid=? ORDER BY leg", (item["swap_uuid"],)).fetchall()
                item["basket_legs"] = []
                for leg in legs:
                    plan = json.loads(leg["plan"])
                    result = json.loads(leg["result"] or "null") or {}
                    item["basket_legs"].append({"leg": leg["leg"], "state": leg["state"],
                        "cex": str(plan.get("cex", item["cex"])).upper(),
                        "symbol": plan["symbol"], "side": plan["side"], "quantity": plan["quantity"],
                        "executed_quantity": result.get("executedQty"), "quote_quantity": result.get("cummulativeQuoteQty"),
                        "dust": plan.get("dust", "0"), "client_order_id": plan["client_order_id"]})
        venue_rows = connection.execute("""
            SELECT h.state, r.payload_json
              FROM hedges AS h
              LEFT JOIN received_hedge_events AS r USING (swap_uuid)
        """).fetchall()
        venues: dict[str, dict[str, Any]] = {}
        for row in venue_rows:
            venue = _event_cex(row["payload_json"])
            states = venues.setdefault(venue, {"by_state": {}})["by_state"]
            state = str(row["state"])
            states[state] = states.get(state, 0) + 1
        for venue, payload in venues.items():
            states = payload["by_state"]
            payload.update({
                "total": sum(states.values()),
                "attention": _count_states(states, ATTENTION_STATES),
                "in_progress": _count_states(states, IN_PROGRESS_STATES),
                "pending_validation": _count_states(states, PENDING_VALIDATION_STATES),
                "validated": states.get("TEST_VALIDATED", 0),
                "recent": [item for item in recent if item["cex"] == venue],
            })
        return {
            "available": True,
            "reason": None,
            "delivery": delivery,
            "hedges": hedges,
            "alarms": alarms,
            "recent": recent,
            "venues": venues,
        }


def _event_cex(raw: Any) -> str:
    try:
        payload = json.loads(str(raw or "{}"))
        return str(payload.get("cex") or "MEXC").upper()
    except (TypeError, ValueError, AttributeError):
        return "MEXC"


def _count_states(by_state: dict[str, int], states: set[str]) -> int:
    return sum(by_state.get(state, 0) for state in states)


def _alarms(
    delivery: dict[str, int],
    by_state: dict[str, int],
    recent_rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    alarms: list[dict[str, Any]] = []
    pending_ack = delivery["pending_acknowledgement"]
    if pending_ack:
        alarms.append(
            {
                "severity": "WARNING",
                "code": "ACK_PENDING",
                "count": pending_ack,
                "message": f"{pending_ack} eventi attendono conferma alla VPS",
            }
        )
    in_progress = _count_states(by_state, IN_PROGRESS_STATES)
    if in_progress:
        alarms.append(
            {
                "severity": "WARNING",
                "code": "HEDGE_IN_PROGRESS",
                "count": in_progress,
                "message": f"{in_progress} coperture sono ancora in corso",
            }
        )
    attention = _count_states(by_state, ATTENTION_STATES)
    if attention:
        alarms.append(
            {
                "severity": "CRITICAL",
                "code": "HEDGE_ATTENTION",
                "count": attention,
                "message": f"{attention} coperture richiedono attenzione manuale",
            }
        )
    for row in recent_rows:
        if str(row["state"]) not in ATTENTION_STATES:
            continue
        error = str(row["last_error"] or "nessun dettaglio disponibile")
        alarms.append(
            {
                "severity": "CRITICAL",
                "code": str(row["state"]),
                "count": 1,
                "swap_uuid": str(row["swap_uuid"]),
                "message": error[:300],
            }
        )
    return alarms


def _empty_delivery() -> dict[str, int]:
    return {
        "total": 0,
        "acknowledged": 0,
        "pending_acknowledgement": 0,
        "cursor": 0,
    }


def _empty_hedges() -> dict[str, Any]:
    return {
        "total": 0,
        "attention": 0,
        "in_progress": 0,
        "pending_validation": 0,
        "validated": 0,
        "by_state": {},
    }
