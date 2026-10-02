from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .auto_hedge import AutomaticHedgeEngine
from .desktop_agent import DesktopAgent, DesktopAgentError
from .desktop_coverage import DesktopCoveragePublisher
from .journal import JournalConflict


@dataclass(frozen=True, slots=True)
class DesktopRuntimeResult:
    sync: dict[str, Any] | None
    coverage: dict[str, Any] | None
    hedging: dict[str, Any] | None
    errors: dict[str, str]
    timings_ms: dict[str, float] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class DesktopRuntime:
    """Runs delivery, fail-closed balance leases and optional live hedging."""

    def __init__(
        self,
        *,
        desktop: DesktopAgent,
        coverage: DesktopCoveragePublisher | None = None,
        hedging: AutomaticHedgeEngine | None = None,
    ) -> None:
        self.desktop = desktop
        self.coverage = coverage
        self.hedging = hedging
        self._last_sample = float('-inf')

    def run_once(self) -> DesktopRuntimeResult:
        started = time.monotonic()
        timings = {}
        errors: dict[str, str] = {}
        coverage_payload: dict[str, Any] | None = None
        sync_payload: dict[str, Any] | None = None
        hedge_payload: dict[str, Any] | None = None

        try:
            sync_payload = self.desktop.sync_once().payload()
        except (DesktopAgentError, JournalConflict) as exc:
            errors["event_sync"] = _safe_error(exc)
        timings['event_sync'] = round((time.monotonic() - started) * 1000, 2)
        hedge_started = time.monotonic()

        # A valid event already in the journal represents an economic commitment.
        # Its hedge must not wait for a temporary VPS/ACK outage.
        if self.hedging is not None:
            try:
                hedge_payload = self.hedging.run_once().payload()
            except Exception as exc:
                errors["hedging"] = _safe_error(exc)

        timings['hedging'] = round((time.monotonic() - hedge_started) * 1000, 2)
        coverage_started = time.monotonic()
        hedge_attention = bool(
            hedge_payload is not None and (
                hedge_payload.get("attention", 0) or hedge_payload.get("in_progress", 0)
            )
        )
        if self.coverage is not None and not errors and not hedge_attention:
            try:
                coverage_payload = self.coverage.publish_once().payload()
            except Exception as exc:
                errors["coverage"] = _safe_error(exc)
            timings.update(getattr(self.coverage, 'timings_ms', {}))
        elif self.coverage is not None and hedge_attention:
            errors["coverage"] = (
                "permesso VPS non rinnovato: una copertura richiede attenzione"
            )

        if self.coverage is not None and errors and "coverage" not in errors:
            errors["coverage"] = "rinnovo completo non riuscito; ripubblicazione sospesa"
        if self.coverage is not None and errors:
            try:
                self.coverage.hold_publications()
            except Exception as exc:
                errors["publication_hold"] = _safe_error(exc)

        timings['coverage'] = round((time.monotonic() - coverage_started) * 1000, 2)
        timings['total'] = round((time.monotonic() - started) * 1000, 2)
        record = getattr(getattr(self.desktop, 'journal', None), 'record_runtime_sample', None)
        if record and (errors or started - self._last_sample >= 60
                       or timings['total'] >= getattr(self.coverage, 'ttl_ms', 10000) / 2):
            try:
                record(timings_ms=timings, errors=errors)
                self._last_sample = started
            except Exception as exc:
                errors['telemetry'] = _safe_error(exc)
        return DesktopRuntimeResult(
            sync=sync_payload,
            coverage=coverage_payload,
            hedging=hedge_payload,
            errors=errors,
            timings_ms=timings,
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
        last_payload: str | None = None
        last_telemetry = float('-inf')
        while not stop.is_set():
            started = time.monotonic()
            result = self.run_once()
            payload = result.payload()
            printable = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            has_activity = bool(
                result.errors
                or started - last_telemetry >= 60
                or result.timings_ms.get('total', 0) >= getattr(self.coverage, 'ttl_ms', 10000) / 2
                or (result.sync and (result.sync.get("received") or result.sync.get("acknowledged")))
                or (result.hedging and result.hedging.get("actions"))
            )
            if has_activity and printable != last_payload:
                stream = sys.stderr if result.errors else sys.stdout
                print(printable, file=stream, flush=True)
                last_payload = printable
                last_telemetry = started
            # Period is start-to-start, not work duration plus a further delay.
            # Never busy-spin when a cycle takes longer than the target period.
            stop.wait(max(0.1, poll_interval - (time.monotonic() - started)))


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:300]
