from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlencode

from .http import JsonTransport, TransportError, UrllibJsonTransport
from .journal import (
    HedgeJournal,
    JournalConflict,
    ReceivedHedgeEvent,
    ReceivedSwapOutcome,
)
from .models import DexSide, HedgeSide
from .venues import normalize_cex
from .outbox import (
    EVENT_SCHEMA_VERSION,
    HEDGE_EVENT_TYPE,
    HEDGE_TRIGGER_EVENT,
    SWAP_OUTCOME_EVENT_TYPE,
    SWAP_OUTCOME_TRIGGER_EVENT,
    verify_event_envelope,
)


class DesktopAgentError(RuntimeError):
    pass


class DesktopEventValidationError(DesktopAgentError):
    pass


_CONSUMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ValidatedHedgeEvent:
    event_id: int
    swap_uuid: str
    order_uuid: str
    schema_version: int
    event_type: str
    market_id: str
    dex_side: DexSide
    hedge_side: HedgeSide
    hedge_symbol: str
    arrr_quantity: Decimal
    base_ticker: str
    trigger_event: str
    trigger_timestamp_ms: int
    event: Mapping[str, Any]
    signature: str

    @property
    def base_quantity(self) -> Decimal:
        return self.arrr_quantity


@dataclass(frozen=True, slots=True)
class ValidatedSwapOutcomeEvent:
    event_id: int
    swap_uuid: str
    schema_version: int
    event_type: str
    trigger_event: str
    completed_at_ms: int
    kdf_success: bool
    terminal_event: str
    event: Mapping[str, Any]
    signature: str


@dataclass(frozen=True, slots=True)
class DesktopSyncResult:
    received: int
    duplicates: int
    acknowledged: int
    cursor: int
    pending_acknowledgements: int

    def payload(self) -> dict[str, int]:
        return asdict(self)


class VpsEventClient:
    """Small authenticated client for the durable VPS Agent event API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: JsonTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not base_url:
            raise ValueError("VPS Agent URL is required")
        if not token:
            raise ValueError("VPS Agent token is required")
        if timeout <= 0:
            raise ValueError("desktop HTTP timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport or UrllibJsonTransport()
        self.timeout = timeout

    def events(
        self, *, after_event_id: int, limit: int = 100
    ) -> tuple[Mapping[str, Any], ...]:
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if limit <= 0 or limit > 1000:
            raise ValueError("event limit must be in [1, 1000]")
        query = urlencode({"after_event_id": after_event_id, "limit": limit})
        payload = self._request("GET", f"/v1/events?{query}")
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("events"), list
        ):
            raise DesktopAgentError("VPS Agent returned an invalid event page")
        rows = payload["events"]
        if len(rows) > limit or any(not isinstance(row, Mapping) for row in rows):
            raise DesktopAgentError("VPS Agent returned an invalid event list")
        return tuple(rows)

    def acknowledge(
        self, *, event_id: int, consumer_id: str
    ) -> Mapping[str, Any]:
        if event_id <= 0:
            raise ValueError("event_id must be positive")
        body = json.dumps(
            {"event_id": event_id, "consumer_id": consumer_id},
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._request(
            "POST",
            "/v1/events/acknowledge",
            body=body,
            content_type="application/json",
        )
        if not isinstance(payload, Mapping):
            raise DesktopAgentError("VPS Agent returned an invalid acknowledgement")
        return payload

    def publish_coverage_lease(
        self, envelope: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        body = json.dumps(
            dict(envelope), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        payload = self._request(
            "POST",
            "/v1/coverage/lease",
            body=body,
            content_type="application/json",
        )
        if not isinstance(payload, Mapping):
            raise DesktopAgentError("VPS Agent returned an invalid coverage status")
        return payload

    def hold_coverage_publications(self, recovery_marker: int) -> Mapping[str, Any]:
        body = json.dumps({"recovery_marker": recovery_marker}).encode("utf-8")
        payload = self._request(
            "POST", "/v1/coverage/publication-hold", body=body,
            content_type="application/json",
        )
        if not isinstance(payload, Mapping):
            raise DesktopAgentError("VPS Agent returned an invalid publication hold")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            return self.transport.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers=headers,
                body=body,
                timeout=self.timeout,
            )
        except TransportError as exc:
            raise DesktopAgentError(str(exc)) from exc


class DesktopAgent:
    """Receives and durably records signed hedge events before execution."""

    def __init__(
        self,
        *,
        journal: HedgeJournal,
        events: VpsEventClient,
        event_secret: str,
        consumer_id: str,
        page_limit: int = 100,
    ) -> None:
        if len(event_secret.encode("utf-8")) < 32:
            raise ValueError("KDF_MM_EVENT_SECRET must contain at least 32 bytes")
        if not _CONSUMER_ID.fullmatch(consumer_id):
            raise ValueError("desktop consumer_id is invalid")
        if page_limit <= 0 or page_limit > 1000:
            raise ValueError("desktop page_limit must be in [1, 1000]")
        self.journal = journal
        self.events = events
        self.event_secret = event_secret
        self.consumer_id = consumer_id
        self.page_limit = page_limit

    def receive_envelope(
        self, envelope: Mapping[str, Any]
    ) -> ReceivedHedgeEvent | ReceivedSwapOutcome:
        validated = validate_event_envelope(envelope, secret=self.event_secret)
        try:
            if isinstance(validated, ValidatedSwapOutcomeEvent):
                return self.journal.record_received_outcome(
                    event_id=validated.event_id,
                    swap_uuid=validated.swap_uuid,
                    schema_version=validated.schema_version,
                    event_type=validated.event_type,
                    trigger_event=validated.trigger_event,
                    completed_at_ms=validated.completed_at_ms,
                    kdf_success=validated.kdf_success,
                    terminal_event=validated.terminal_event,
                    event=validated.event,
                    signature=validated.signature,
                )
            return self.journal.record_received_event(
                event_id=validated.event_id,
                swap_uuid=validated.swap_uuid,
                order_uuid=validated.order_uuid,
                schema_version=validated.schema_version,
                event_type=validated.event_type,
                market_id=validated.market_id,
                dex_side=validated.dex_side,
                hedge_side=validated.hedge_side,
                hedge_symbol=validated.hedge_symbol,
                target_quantity=validated.arrr_quantity,
                trigger_event=validated.trigger_event,
                trigger_timestamp_ms=validated.trigger_timestamp_ms,
                event=validated.event,
                signature=validated.signature,
            )
        except JournalConflict as exc:
            raise DesktopAgentError(f"desktop journal conflict: {exc}") from exc

    def sync_once(self) -> DesktopSyncResult:
        acknowledged = 0
        for pending in self.journal.pending_event_acknowledgements():
            self._acknowledge(pending)
            acknowledged += 1

        initial_cursor = self.journal.received_event_cursor()
        envelopes = self.events.events(
            after_event_id=initial_cursor, limit=self.page_limit
        )
        unique: list[Mapping[str, Any]] = []
        seen: dict[int, tuple[Mapping[str, Any], str]] = {}
        last_new_event_id = initial_cursor
        duplicates = 0

        # Validate the complete page before changing local or remote state.
        for envelope in envelopes:
            validated = validate_event_envelope(envelope, secret=self.event_secret)
            previous = seen.get(validated.event_id)
            identity = (validated.event, validated.signature)
            if previous is not None:
                if previous != identity:
                    raise DesktopEventValidationError(
                        "one event ID was delivered with conflicting contents"
                    )
                duplicates += 1
                continue
            seen[validated.event_id] = identity
            existing = self.journal.received_delivery_event(validated.event_id)
            if existing is None:
                if validated.event_id <= last_new_event_id:
                    raise DesktopEventValidationError(
                        "new VPS events are not in increasing order"
                    )
                last_new_event_id = validated.event_id
            else:
                duplicates += 1
            unique.append(envelope)

        received = 0
        for envelope in unique:
            validated = validate_event_envelope(envelope, secret=self.event_secret)
            existing = self.journal.received_delivery_event(validated.event_id)
            record = self.receive_envelope(envelope)
            if existing is None:
                received += 1
            if not record.acknowledged:
                self._acknowledge(record)
                acknowledged += 1

        status = self.journal.received_event_status()
        return DesktopSyncResult(
            received=received,
            duplicates=duplicates,
            acknowledged=acknowledged,
            cursor=status["cursor"],
            pending_acknowledgements=status["pending_acknowledgement"],
        )

    def run_forever(
        self,
        *,
        poll_interval: float,
        stop_event: threading.Event | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("desktop poll interval must be positive")
        stop = stop_event or threading.Event()
        last_error: str | None = None
        while not stop.is_set():
            try:
                result = self.sync_once()
                if last_error is not None:
                    print("Desktop Agent: collegamento ripristinato")
                    last_error = None
                if result.received or result.acknowledged:
                    print(json.dumps(result.payload(), sort_keys=True))
            except (DesktopAgentError, JournalConflict) as exc:
                message = str(exc)
                if message != last_error:
                    print(f"ATTENZIONE Desktop Agent: {message}", file=sys.stderr)
                    last_error = message
            stop.wait(poll_interval)

    def _acknowledge(
        self, received: ReceivedHedgeEvent | ReceivedSwapOutcome
    ) -> None:
        local = validate_event_envelope(
            {"event": received.event, "signature": received.signature},
            secret=self.event_secret,
        )
        if (
            local.event_id != received.event_id
            or local.swap_uuid != received.swap_uuid
            or dict(local.event) != dict(received.event)
            or local.signature != received.signature
        ):
            raise DesktopEventValidationError(
                "journal metadata does not match its signed VPS event"
            )
        envelope = self.events.acknowledge(
            event_id=received.event_id,
            consumer_id=self.consumer_id,
        )
        validated = validate_event_envelope(
            envelope, secret=self.event_secret
        )
        if (
            validated.event_id != received.event_id
            or dict(validated.event) != dict(received.event)
            or validated.signature != received.signature
        ):
            raise DesktopEventValidationError(
                "VPS acknowledgement does not match the journalled event"
            )
        delivery = envelope.get("delivery")
        if not isinstance(delivery, Mapping):
            raise DesktopEventValidationError(
                "VPS acknowledgement omitted delivery details"
            )
        if delivery.get("acknowledged") is not True:
            raise DesktopEventValidationError("VPS did not acknowledge the event")
        if delivery.get("acknowledged_by") != self.consumer_id:
            raise DesktopEventValidationError(
                "VPS event was acknowledged by another consumer"
            )
        remote_timestamp = delivery.get("acknowledged_at_ms")
        acknowledged_at_ms = _positive_integer(
            remote_timestamp,
            "delivery.acknowledged_at_ms",
        )
        self.journal.mark_event_acknowledged(
            received.event_id,
            acknowledged_at_ms=acknowledged_at_ms,
        )


def validate_event_envelope(
    envelope: Mapping[str, Any], *, secret: str
) -> ValidatedHedgeEvent | ValidatedSwapOutcomeEvent:
    event = envelope.get("event")
    if not isinstance(event, Mapping):
        raise DesktopEventValidationError("VPS event envelope is invalid")
    event_type = event.get("event_type")
    if event_type == HEDGE_EVENT_TYPE:
        return validate_hedge_event_envelope(envelope, secret=secret)
    if event_type == SWAP_OUTCOME_EVENT_TYPE:
        return validate_swap_outcome_envelope(envelope, secret=secret)
    raise DesktopEventValidationError(f"unsupported VPS event {event_type}")


def validate_hedge_event_envelope(
    envelope: Mapping[str, Any], *, secret: str
) -> ValidatedHedgeEvent:
    if not verify_event_envelope(envelope, secret=secret):
        raise DesktopEventValidationError("VPS event signature is invalid")
    event = envelope.get("event")
    signature = envelope.get("signature")
    assert isinstance(event, Mapping) and isinstance(signature, str)

    event_id = _positive_integer(event.get("event_id"), "event_id")
    schema_version = _positive_integer(
        event.get("schema_version"), "schema_version"
    )
    if schema_version != EVENT_SCHEMA_VERSION:
        raise DesktopEventValidationError(
            f"unsupported VPS event schema {schema_version}"
        )
    event_type = _required_string(event, "event_type")
    if event_type != HEDGE_EVENT_TYPE:
        raise DesktopEventValidationError(f"unsupported VPS event {event_type}")
    trigger_event = _required_string(event, "trigger_event")
    if trigger_event != HEDGE_TRIGGER_EVENT:
        raise DesktopEventValidationError(
            f"unsafe hedge trigger {trigger_event}"
        )

    swap_uuid = _required_string(event, "swap_uuid")
    order_uuid = _required_string(event, "order_uuid")
    market_id = _required_string(event, "market_id")
    hedge_symbol = _required_string(event, "hedge_symbol")
    try:
        cex = normalize_cex(event.get("cex", "MEXC"))
    except ValueError as exc:
        raise DesktopEventValidationError(str(exc)) from exc
    quote_ticker = _required_string(event, "quote_ticker")
    _validate_quote_valuation(event)
    base_ticker = str(event.get("base_ticker", "ARRR")).strip().upper()
    if not base_ticker:
        raise DesktopEventValidationError("invalid event field base_ticker")
    if market_id != f"{base_ticker}-{quote_ticker}":
        raise DesktopEventValidationError(
            "event market does not match its quote ticker"
        )

    try:
        dex_side = DexSide(_required_string(event, "dex_side"))
        hedge_side = HedgeSide(_required_string(event, "hedge_side"))
    except ValueError as exc:
        raise DesktopEventValidationError("event contains an invalid side") from exc
    expected_hedge = (
        HedgeSide.BUY if dex_side is DexSide.SELL_ARRR else HedgeSide.SELL
    )
    if hedge_side is not expected_hedge:
        raise DesktopEventValidationError(
            "event hedge side is inconsistent with its DEX side"
        )

    raw_base_quantity = event.get("base_quantity", event.get("arrr_quantity"))
    arrr_quantity = _positive_decimal(raw_base_quantity, "base_quantity")
    if "arrr_quantity" in event and _positive_decimal(
        event.get("arrr_quantity"), "arrr_quantity"
    ) != arrr_quantity:
        raise DesktopEventValidationError("event base quantities are inconsistent")
    maker_coin = _required_string(event, "kdf_maker_coin")
    taker_coin = _required_string(event, "kdf_taker_coin")
    maker_amount = _positive_decimal(event.get("kdf_maker_amount"), "kdf_maker_amount")
    taker_amount = _positive_decimal(event.get("kdf_taker_amount"), "kdf_taker_amount")
    if dex_side is DexSide.SELL_ARRR:
        terms_match = (
            maker_coin == base_ticker
            and taker_coin == quote_ticker
            and maker_amount == arrr_quantity
        )
    else:
        terms_match = (
            maker_coin == quote_ticker
            and taker_coin == base_ticker
            and taker_amount == arrr_quantity
        )
    if not terms_match:
        raise DesktopEventValidationError(
            "event base quantity does not match its KDF swap terms"
        )
    if "hedge_legs" in event:
        legs = event["hedge_legs"]
        if not isinstance(legs, list) or not 1 <= len(legs) <= 2:
            raise DesktopEventValidationError("invalid hedge basket")
        sides = set()
        for leg in legs:
            if not isinstance(leg, Mapping) or leg.get("side") not in {"BUY", "SELL"}:
                raise DesktopEventValidationError("invalid hedge leg")
            side = leg["side"]
            expected_amount = maker_amount if side == "BUY" else taker_amount
            expected_coin = maker_coin if side == "BUY" else taker_coin
            if side in sides or leg.get("kdf_ticker") != expected_coin or _positive_decimal(leg.get("quantity"), "leg quantity") != expected_amount:
                raise DesktopEventValidationError("hedge legs do not match actual swap terms")
            asset = _required_string(leg, "asset")
            try:
                leg_cex = normalize_cex(leg.get("cex", cex))
            except ValueError as exc:
                raise DesktopEventValidationError(str(exc)) from exc
            if leg_cex != cex:
                raise DesktopEventValidationError("hedge leg CEX does not match event CEX")
            if asset == "USDT" or leg.get("symbol") != asset + "USDT":
                raise DesktopEventValidationError("invalid hedge leg route")
            sides.add(side)

    trigger_timestamp_ms = _positive_integer(
        event.get("trigger_timestamp_ms"), "trigger_timestamp_ms"
    )
    _positive_integer(event.get("observed_at_ms"), "observed_at_ms")
    return ValidatedHedgeEvent(
        event_id=event_id,
        swap_uuid=swap_uuid,
        order_uuid=order_uuid,
        schema_version=schema_version,
        event_type=event_type,
        market_id=market_id,
        dex_side=dex_side,
        hedge_side=hedge_side,
        hedge_symbol=hedge_symbol,
        arrr_quantity=arrr_quantity,
        base_ticker=base_ticker,
        trigger_event=trigger_event,
        trigger_timestamp_ms=trigger_timestamp_ms,
        event=dict(event),
        signature=signature,
    )


def validate_swap_outcome_envelope(
    envelope: Mapping[str, Any], *, secret: str
) -> ValidatedSwapOutcomeEvent:
    if not verify_event_envelope(envelope, secret=secret):
        raise DesktopEventValidationError("VPS event signature is invalid")
    event = envelope.get("event")
    signature = envelope.get("signature")
    assert isinstance(event, Mapping) and isinstance(signature, str)

    event_id = _positive_integer(event.get("event_id"), "event_id")
    schema_version = _positive_integer(
        event.get("schema_version"), "schema_version"
    )
    if schema_version != EVENT_SCHEMA_VERSION:
        raise DesktopEventValidationError(
            f"unsupported VPS event schema {schema_version}"
        )
    if _required_string(event, "event_type") != SWAP_OUTCOME_EVENT_TYPE:
        raise DesktopEventValidationError("invalid KDF outcome event type")
    trigger_event = _required_string(event, "trigger_event")
    if trigger_event != SWAP_OUTCOME_TRIGGER_EVENT:
        raise DesktopEventValidationError(
            f"unsafe outcome trigger {trigger_event}"
        )

    swap_uuid = _required_string(event, "swap_uuid")
    _required_string(event, "order_uuid")
    market_id = _required_string(event, "market_id")
    hedge_symbol = _required_string(event, "hedge_symbol")
    quote_ticker = _required_string(event, "quote_ticker")
    _validate_quote_valuation(event)
    base_ticker = str(event.get("base_ticker", "ARRR")).strip().upper()
    if not base_ticker or market_id != f"{base_ticker}-{quote_ticker}":
        raise DesktopEventValidationError(
            "outcome market does not match its hedge terms"
        )

    try:
        dex_side = DexSide(_required_string(event, "dex_side"))
        hedge_side = HedgeSide(_required_string(event, "hedge_side"))
    except ValueError as exc:
        raise DesktopEventValidationError("event contains an invalid side") from exc
    expected_hedge = (
        HedgeSide.BUY if dex_side is DexSide.SELL_ARRR else HedgeSide.SELL
    )
    if hedge_side is not expected_hedge:
        raise DesktopEventValidationError(
            "outcome hedge side is inconsistent with its DEX side"
        )

    raw_base_quantity = event.get("base_quantity", event.get("arrr_quantity"))
    arrr_quantity = _positive_decimal(raw_base_quantity, "base_quantity")
    if "arrr_quantity" in event and _positive_decimal(
        event.get("arrr_quantity"), "arrr_quantity"
    ) != arrr_quantity:
        raise DesktopEventValidationError("outcome base quantities are inconsistent")
    maker_coin = _required_string(event, "kdf_maker_coin")
    taker_coin = _required_string(event, "kdf_taker_coin")
    maker_amount = _positive_decimal(
        event.get("kdf_maker_amount"), "kdf_maker_amount"
    )
    taker_amount = _positive_decimal(
        event.get("kdf_taker_amount"), "kdf_taker_amount"
    )
    terms_match = (
        maker_coin == base_ticker
        and taker_coin == quote_ticker
        and maker_amount == arrr_quantity
        if dex_side is DexSide.SELL_ARRR
        else maker_coin == quote_ticker
        and taker_coin == base_ticker
        and taker_amount == arrr_quantity
    )
    if not terms_match:
        raise DesktopEventValidationError(
            "outcome base quantity does not match its KDF swap terms"
        )

    kdf_success = event.get("kdf_success")
    if not isinstance(kdf_success, bool):
        raise DesktopEventValidationError("invalid event field kdf_success")
    terminal_event = _required_string(event, "terminal_event")
    completed_at_ms = _positive_integer(
        event.get("completed_at_ms"), "completed_at_ms"
    )
    trigger_timestamp_ms = _positive_integer(
        event.get("trigger_timestamp_ms"), "trigger_timestamp_ms"
    )
    if trigger_timestamp_ms != completed_at_ms:
        raise DesktopEventValidationError(
            "outcome completion timestamp is inconsistent"
        )
    _positive_integer(event.get("observed_at_ms"), "observed_at_ms")
    return ValidatedSwapOutcomeEvent(
        event_id=event_id,
        swap_uuid=swap_uuid,
        schema_version=schema_version,
        event_type=SWAP_OUTCOME_EVENT_TYPE,
        trigger_event=trigger_event,
        completed_at_ms=completed_at_ms,
        kdf_success=kdf_success,
        terminal_event=terminal_event,
        event=dict(event),
        signature=signature,
    )


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 128:
        raise DesktopEventValidationError(f"invalid event field {name}")
    return value


def _validate_quote_valuation(event: Mapping[str, Any]) -> None:
    fields = (
        "quote_usdt_rate",
        "quote_usdt_symbol",
        "quote_usdt_side",
        "quote_usdt_observed_at_ms",
    )
    present = [field in event for field in fields]
    if not any(present):
        return
    if not all(present):
        raise DesktopEventValidationError(
            "event contains an incomplete quote/USDT valuation"
        )
    _positive_decimal(event["quote_usdt_rate"], "quote_usdt_rate")
    _required_string(event, "quote_usdt_symbol")
    if event["quote_usdt_side"] not in {"DIRECT", "BID", "ASK"}:
        raise DesktopEventValidationError("invalid event field quote_usdt_side")
    _positive_integer(
        event["quote_usdt_observed_at_ms"], "quote_usdt_observed_at_ms"
    )


def _positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DesktopEventValidationError(f"invalid event field {name}")
    if value <= 0:
        raise DesktopEventValidationError(f"invalid event field {name}")
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DesktopEventValidationError(f"invalid event field {name}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise DesktopEventValidationError(f"invalid event field {name}")
    return parsed
