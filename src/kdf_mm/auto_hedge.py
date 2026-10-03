from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .hedging import HedgeExecutor
from .journal import HedgeJournal, HedgeState, JournalConflict
from .mexc_test_connector import MexcTestConnector, MexcTestConnectorError
from .basket_hedge import BasketHedgeExecutor


ACTIVE_HEDGE_STATES = {
    HedgeState.RESERVED,
    HedgeState.HEDGE_READY,
    HedgeState.TESTING,
    HedgeState.TEST_VALIDATED,
    HedgeState.SUBMITTING,
    HedgeState.SUBMITTED,
    HedgeState.UNKNOWN,
    HedgeState.PARTIAL,
}


@dataclass(frozen=True, slots=True)
class AutomaticHedgeResult:
    examined: int
    filled: int
    attention: int
    in_progress: int
    actions: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class AutomaticHedgeEngine:
    """Executes exact-quantity, aggressive LIMIT hedges from durable events."""

    def __init__(
        self,
        *,
        journal: HedgeJournal,
        mexc: Any = None,
        clients: dict[str, Any] | None = None,
        venue_fees: dict[str, Decimal] | None = None,
        max_slippage: Decimal,
        fee_buffer: Decimal,
        depth_limit: int,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0 or max_attempts > 10:
            raise ValueError("automatic hedge attempts must be in [1, 10]")
        self.journal = journal
        configured = {str(k).upper(): v for k, v in (clients or {}).items()}
        if mexc is not None:
            configured.setdefault("MEXC", mexc)
        if not configured:
            raise ValueError("at least one Spot exchange client is required")
        self.clients = configured
        self.mexc = self.clients.get("MEXC") or next(iter(self.clients.values()))
        fees = {str(k).upper(): Decimal(str(v)) for k, v in (venue_fees or {}).items()}
        self.max_attempts = max_attempts
        self.baskets = {
            venue: BasketHedgeExecutor(journal=journal, mexc=client,
                                       fee=fees.get(venue, fee_buffer),
                                       impact=max_slippage, depth=depth_limit)
            for venue, client in self.clients.items()
        }
        self.validators = {
            venue: MexcTestConnector(
                journal=journal, mexc=client, max_slippage=max_slippage,
                fee_buffer=fees.get(venue, fee_buffer), depth_limit=depth_limit,
                require_acknowledged=False,
            )
            for venue, client in self.clients.items()
        }

    def run_once(self) -> AutomaticHedgeResult:
        actions: list[dict[str, Any]] = []
        unresolved = self.journal.hedges(
            states={HedgeState.REVIEW_REQUIRED, HedgeState.FAILED}
        )
        if unresolved:
            return AutomaticHedgeResult(
                examined=0,
                filled=0,
                attention=len(unresolved),
                in_progress=len(
                    self.journal.hedges(states=ACTIVE_HEDGE_STATES)
                ),
                actions=tuple(
                    {
                        "swap_uuid": hedge.swap_uuid,
                        "state": hedge.state.value,
                        "action": "BLOCKED_BY_UNRESOLVED_EXPOSURE",
                        "error": hedge.last_error,
                    }
                    for hedge in unresolved
                ),
            )
        hedges = tuple(
            sorted(
                self.journal.hedges(states=ACTIVE_HEDGE_STATES),
                key=lambda item: (
                    0
                    if item.state
                    in {
                        HedgeState.UNKNOWN,
                        HedgeState.SUBMITTING,
                        HedgeState.SUBMITTED,
                    }
                    else 1,
                    item.swap_uuid,
                ),
            )
        )
        for hedge in hedges:
            try:
                actions.extend(self._process(hedge.swap_uuid))
            except (MexcTestConnectorError, JournalConflict, KeyError, ValueError) as exc:
                actions.append(
                    {
                        "swap_uuid": hedge.swap_uuid,
                        "state": self.journal.get(hedge.swap_uuid).state.value,
                        "action": "ATTENTION",
                        "error": str(exc),
                    }
                )
            current = self.journal.get(hedge.swap_uuid)
            if current is not None and current.state in {
                HedgeState.REVIEW_REQUIRED,
                HedgeState.UNKNOWN,
                HedgeState.FAILED,
            }:
                break
        current = [self.journal.get(item.swap_uuid) for item in hedges]
        return AutomaticHedgeResult(
            examined=len(hedges),
            filled=sum(item is not None and item.state is HedgeState.FILLED for item in current),
            attention=sum(
                item is not None
                and item.state
                in {HedgeState.REVIEW_REQUIRED, HedgeState.UNKNOWN, HedgeState.FAILED}
                for item in current
            ),
            in_progress=sum(
                item is not None and item.state in ACTIVE_HEDGE_STATES for item in current
            ),
            actions=tuple(actions),
        )

    def _process(self, swap_uuid: str) -> list[dict[str, Any]]:
        received = self.journal.received_event_for_swap(swap_uuid)
        if received is None:
            raise MexcTestConnectorError(
                "signed VPS hedge event is missing before live execution"
            )
        venue = str(received.event.get("cex", "MEXC")).upper()
        if venue not in self.clients:
            raise MexcTestConnectorError(f"credenziali/client {venue} non disponibili")
        client = self.clients[venue]
        validator = self.validators[venue]
        if "hedge_legs" in received.event:
            legs = received.event.get("hedge_legs")
            if not isinstance(legs, list) or any(str(leg.get("cex", venue)).upper() != venue
                                                 for leg in legs if isinstance(leg, dict)):
                raise MexcTestConnectorError("evento hedge contiene CEX discordanti")
            return self.baskets[venue].process(received)
        actions: list[dict[str, Any]] = []
        while True:
            hedge = self.journal.get(swap_uuid)
            assert hedge is not None
            attempts = self.journal.attempts(swap_uuid)
            if hedge.state is HedgeState.TESTING:
                validator.validate(
                    swap_uuid,
                    sequence=attempts[-1].sequence if attempts else 1,
                )
                continue
            if hedge.state is HedgeState.HEDGE_READY:
                attempt = self.journal.latest_attempt(swap_uuid)
                if attempt is None:
                    raise JournalConflict("prepared live hedge has no durable attempt")
                validator.validate(
                    swap_uuid,
                    sequence=attempt.sequence,
                    requested_quantity=attempt.requested_quantity,
                )
                continue
            if hedge.state in {HedgeState.RESERVED, HedgeState.PARTIAL}:
                if len(attempts) >= self.max_attempts:
                    failed = self.journal.transition(
                        swap_uuid,
                        HedgeState.FAILED,
                        error=(
                            "automatic hedge attempt limit reached; residual exposure "
                            "requires manual action"
                        ),
                    )
                    actions.append(
                        {
                            "swap_uuid": swap_uuid,
                            "state": failed.state.value,
                            "action": "ATTEMPT_LIMIT",
                        }
                    )
                    return actions
                remaining = hedge.target_quantity - self.journal.total_executed(swap_uuid)
                sequence = len(attempts) + 1
                validated = validator.validate(
                    swap_uuid,
                    sequence=sequence,
                    requested_quantity=remaining,
                )
                actions.append(
                    {
                        "swap_uuid": swap_uuid,
                        "state": validated.state.value,
                        "action": "TEST_VALIDATED",
                        "sequence": sequence,
                        "quantity": str(remaining),
                        "limit_price": str(validated.plan.limit_price),
                    }
                )
                continue
            if hedge.state in {
                HedgeState.TEST_VALIDATED,
                HedgeState.SUBMITTING,
                HedgeState.SUBMITTED,
                HedgeState.UNKNOWN,
            }:
                attempt = self.journal.latest_attempt(swap_uuid)
                if attempt is None:
                    raise JournalConflict("live hedge state has no durable attempt")
                result = HedgeExecutor(
                    journal=self.journal,
                    mexc=client,
                    symbol=received.hedge_symbol,
                ).execute_or_recover(
                    swap_uuid=swap_uuid,
                    sequence=attempt.sequence,
                )
                actions.append(
                    {
                        "swap_uuid": swap_uuid,
                        "state": result.state.value,
                        "action": "EXECUTE_OR_RECOVER",
                        "sequence": attempt.sequence,
                        "filled_quantity": str(result.filled_quantity),
                    }
                )
                if result.state is HedgeState.PARTIAL:
                    continue
                return actions
            return actions
