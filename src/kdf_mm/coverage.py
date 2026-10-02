from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


COVERAGE_LEASE_SCHEMA_VERSION = 1
FORCE_COVERAGE_CONFIRMATION = "FORZA COPERTURA"
_CONSUMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CoverageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoverageLease:
    consumer_id: str
    issued_at_ms: int
    expires_at_ms: int
    free_balances: Mapping[str, Decimal]
    live_hedging_enabled: bool
    signature: str
    strategy_version: int = 0
    hedge_symbols: tuple[str, ...] = ()
    recovery_marker: int = 0


def coverage_signature(payload: Mapping[str, Any], *, secret: str) -> str:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("KDF_MM_EVENT_SECRET must contain at least 32 bytes")
    message = b"kdf-mm/coverage/v1\0" + json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_coverage_envelope(
    *,
    consumer_id: str,
    free_balances: Mapping[str, Decimal],
    secret: str,
    issued_at_ms: int | None = None,
    ttl_ms: int = 10_000,
    live_hedging_enabled: bool = False,
    strategy_version: int = 0,
    hedge_symbols: tuple[str, ...] = (),
    recovery_marker: int = 0,
) -> dict[str, Any]:
    if not _CONSUMER_ID.fullmatch(consumer_id):
        raise ValueError("desktop consumer_id is invalid")
    if ttl_ms <= 0 or ttl_ms > 30_000:
        raise ValueError("coverage lease TTL must be in [1, 30000] ms")
    issued = int(time.time_ns() // 1_000_000 if issued_at_ms is None else issued_at_ms)
    if issued <= 0:
        raise ValueError("coverage lease timestamp must be positive")
    balances = _balance_payload(free_balances)
    if type(recovery_marker) is not int or not 0 <= recovery_marker < 2**63:
        raise ValueError("coverage recovery marker is invalid")
    lease = {
        "schema_version": COVERAGE_LEASE_SCHEMA_VERSION,
        "consumer_id": consumer_id,
        "issued_at_ms": issued,
        "expires_at_ms": issued + ttl_ms,
        "free_balances": balances,
        "live_hedging_enabled": bool(live_hedging_enabled),
        "strategy_version": strategy_version,
        "hedge_symbols": list(hedge_symbols),
        "recovery_marker": recovery_marker,
    }
    return {"lease": lease, "signature": coverage_signature(lease, secret=secret)}


class CoverageGuard:
    """Fail-closed, short-lived proof of available CEX Spot balances."""

    def __init__(
        self,
        *,
        secret: str,
        required: bool,
        audit_db: str | Path = "var/coverage-audit.sqlite3",
        max_lease_ttl_ms: int = 30_000,
        max_clock_skew_ms: int = 5_000,
        max_override_seconds: int = 300,
        clock_ms: Any | None = None,
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("KDF_MM_EVENT_SECRET must contain at least 32 bytes")
        if max_lease_ttl_ms <= 0 or max_lease_ttl_ms > 60_000:
            raise ValueError("maximum coverage lease TTL is invalid")
        if max_clock_skew_ms < 0 or max_clock_skew_ms > 60_000:
            raise ValueError("coverage clock skew is invalid")
        if max_override_seconds <= 0 or max_override_seconds > 300:
            raise ValueError("maximum coverage override must be in [1, 300] seconds")
        self._secret = secret
        self.required = bool(required)
        self.max_lease_ttl_ms = max_lease_ttl_ms
        self.max_clock_skew_ms = max_clock_skew_ms
        self.max_override_seconds = max_override_seconds
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.RLock()
        self._lease: CoverageLease | None = None
        self._publication_hold_marker: int | None = None
        self._recovery_generation = 0
        self._recovery_since_ms: int | None = None
        self._override_until_ms: int | None = None
        self._override_reason: str | None = None
        database_path = None if str(audit_db) == ":memory:" else Path(audit_db)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit = sqlite3.connect(str(audit_db), check_same_thread=False)
        if database_path is not None:
            os.chmod(database_path, 0o600)
        self._audit.execute(
            """
            CREATE TABLE IF NOT EXISTS coverage_override_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                enabled INTEGER NOT NULL,
                requested_at_ms INTEGER NOT NULL,
                expires_at_ms INTEGER,
                reason TEXT NOT NULL
            )
            """
        )
        self._audit.commit()

    def close(self) -> None:
        with self._lock:
            self._audit.close()

    def accept(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        raw = envelope.get("lease")
        signature = envelope.get("signature")
        if not isinstance(raw, Mapping) or not isinstance(signature, str):
            raise CoverageError("invalid coverage lease envelope")
        expected = coverage_signature(raw, secret=self._secret)
        if not hmac.compare_digest(signature, expected):
            raise CoverageError("coverage lease signature is invalid")
        lease = self._parse_lease(raw, signature)
        with self._lock:
            previous = self._lease
            if previous is not None and lease.issued_at_ms < previous.issued_at_ms:
                raise CoverageError("stale coverage lease replay")
            if previous is not None and lease.recovery_marker < previous.recovery_marker:
                raise CoverageError("stale coverage recovery marker")
            if (
                previous is not None
                and lease.issued_at_ms == previous.issued_at_ms
                and lease.signature != previous.signature
            ):
                raise CoverageError("conflicting coverage lease timestamp")
            now = int(self.clock_ms())
            recovered_hold = (self._publication_hold_marker is not None
                              and lease.recovery_marker >= self._publication_hold_marker)
            recovered_expiry = previous is not None and previous.expires_at_ms < now
            recovered_marker = (previous is not None
                                and lease.recovery_marker > previous.recovery_marker)
            if recovered_hold or recovered_expiry or recovered_marker:
                self._recovery_generation += 1
                self._recovery_since_ms = now
            if recovered_hold:
                self._publication_hold_marker = None
            self._lease = lease
        return self.status({})

    def hold_publications(self, *, recovery_marker: int) -> dict[str, Any]:
        """Restrict new quotes without extending a balance lease or cancelling orders."""
        if type(recovery_marker) is not int or not 0 < recovery_marker < 2**63:
            raise CoverageError("publication recovery marker is invalid")
        with self._lock:
            previous = self._lease
            if previous is not None and recovery_marker <= previous.recovery_marker:
                raise CoverageError("stale publication recovery marker")
            if (self._publication_hold_marker is None
                    or recovery_marker > self._publication_hold_marker):
                self._publication_hold_marker = recovery_marker
        return self.status({})

    def set_override(
        self,
        *,
        enabled: bool,
        confirmation: str,
        duration_seconds: int = 300,
    ) -> dict[str, Any]:
        now = int(self.clock_ms())
        if enabled:
            if confirmation != FORCE_COVERAGE_CONFIRMATION:
                raise CoverageError(
                    f'type exactly "{FORCE_COVERAGE_CONFIRMATION}" to force coverage'
                )
            if duration_seconds <= 0 or duration_seconds > self.max_override_seconds:
                raise CoverageError(
                    f"coverage override must be in [1, {self.max_override_seconds}] seconds"
                )
            expires = now + duration_seconds * 1_000
            reason = "manual_force"
        else:
            if confirmation != "TERMINA FORZATURA":
                raise CoverageError('type exactly "TERMINA FORZATURA" to stop the override')
            expires = None
            reason = "manual_stop"
        with self._lock, self._audit:
            self._override_until_ms = expires
            self._override_reason = reason if enabled else None
            self._audit.execute(
                """
                INSERT INTO coverage_override_audit
                    (enabled, requested_at_ms, expires_at_ms, reason)
                VALUES (?, ?, ?, ?)
                """,
                (int(enabled), now, expires, reason),
            )
        return self.status({})

    def block_reason(self, required_balances: Mapping[str, Decimal]) -> str | None:
        status = self.status(required_balances)
        return str(status["reason"]) if status.get("blocked") else None

    def status(self, required_balances: Mapping[str, Decimal]) -> dict[str, Any]:
        required = _decimal_balances(required_balances)
        now = int(self.clock_ms())
        with self._lock:
            lease = self._lease
            override_until = self._override_until_ms
            hold_marker = self._publication_hold_marker
            recovery_generation = self._recovery_generation
            recovery_since_ms = self._recovery_since_ms
            override_active = override_until is not None and now < override_until
            if override_until is not None and not override_active:
                self._override_until_ms = None
                self._override_reason = None
                override_until = None

        fresh = lease is not None and now <= lease.expires_at_ms
        free = dict(lease.free_balances) if fresh and lease is not None else {}
        gaps = {
            asset: amount - free.get(asset, Decimal("0"))
            for asset, amount in required.items()
            if free.get(asset, Decimal("0")) < amount
        }
        reason: str | None = None
        if self.required and not override_active:
            if not fresh:
                reason = "copertura CEX assente o scaduta"
            elif lease is not None and not lease.live_hedging_enabled:
                reason = "copertura automatica CEX reale non attiva"
            elif gaps:
                details = ", ".join(
                    f"{asset} mancano {amount}" for asset, amount in sorted(gaps.items())
                )
                reason = f"copertura CEX insufficiente: {details}"
        blocked = reason is not None
        state = (
            "OVERRIDE"
            if override_active
            else "BLOCKED"
            if blocked
            else "OK"
            if self.required
            else "MONITOR_ONLY"
        )
        return {
            "state": state,
            "required": self.required,
            "blocked": blocked,
            "reason": reason,
            "lease_fresh": fresh,
            "publication_ready": fresh and hold_marker is None,
            "recovery_generation": recovery_generation,
            "recovery_since_ms": recovery_since_ms,
            "consumer_id": lease.consumer_id if lease is not None else None,
            "issued_at_ms": lease.issued_at_ms if lease is not None else None,
            "expires_at_ms": lease.expires_at_ms if lease is not None else None,
            "lease_age_ms": max(0, now - lease.issued_at_ms) if lease is not None else None,
            "live_hedging_enabled": bool(
                lease.live_hedging_enabled if lease is not None else False
            ),
            "strategy_version": lease.strategy_version if fresh and lease is not None else 0,
            "hedge_symbols": list(lease.hedge_symbols) if fresh and lease is not None else [],
            "free_balances": _balance_payload(free),
            "required_balances": _balance_payload(required),
            "gaps": _balance_payload(gaps),
            "override_active": override_active,
            "override_until_ms": override_until,
            "override_remaining_seconds": (
                max(0, (override_until - now + 999) // 1_000)
                if override_until is not None
                else None
            ),
            "override_max_seconds": self.max_override_seconds,
        }

    def _parse_lease(self, raw: Mapping[str, Any], signature: str) -> CoverageLease:
        try:
            schema = int(raw["schema_version"])
            consumer_id = str(raw["consumer_id"])
            issued = int(raw["issued_at_ms"])
            expires = int(raw["expires_at_ms"])
            balances_raw = raw["free_balances"]
            live_hedging_enabled = raw["live_hedging_enabled"]
        except (KeyError, TypeError, ValueError) as exc:
            raise CoverageError("coverage lease fields are invalid") from exc
        if schema != COVERAGE_LEASE_SCHEMA_VERSION:
            raise CoverageError(f"unsupported coverage lease schema {schema}")
        if not _CONSUMER_ID.fullmatch(consumer_id):
            raise CoverageError("coverage lease consumer_id is invalid")
        if not isinstance(balances_raw, Mapping):
            raise CoverageError("coverage lease balances are invalid")
        if not isinstance(live_hedging_enabled, bool):
            raise CoverageError("coverage lease hedging state is invalid")
        strategy_version = raw.get("strategy_version", 0)
        if type(strategy_version) is not int or strategy_version not in (0, 1):
            raise CoverageError("coverage strategy version is invalid")
        hedge_symbols = raw.get("hedge_symbols", [])
        if not isinstance(hedge_symbols, list) or len(hedge_symbols) > 10000 or any(
            not isinstance(s, str) or not s.isascii()
            or any(not c.isalnum() and c != ":" for c in s)
            or s.count(":") > 1 or s.startswith(":") or s.endswith(":")
            for s in hedge_symbols
        ):
            raise CoverageError("coverage hedge symbols are invalid")
        recovery_marker = raw.get("recovery_marker", 0)
        if type(recovery_marker) is not int or not 0 <= recovery_marker < 2**63:
            raise CoverageError("coverage recovery marker is invalid")
        balances = _decimal_balances(balances_raw)
        now = int(self.clock_ms())
        if issued <= 0 or expires <= issued:
            raise CoverageError("coverage lease timestamps are invalid")
        if expires - issued > self.max_lease_ttl_ms:
            raise CoverageError("coverage lease TTL is too long")
        if issued > now + self.max_clock_skew_ms:
            raise CoverageError("coverage lease timestamp is in the future")
        if expires < now:
            raise CoverageError("coverage lease is already expired")
        return CoverageLease(
            consumer_id,
            issued,
            expires,
            balances,
            live_hedging_enabled,
            signature,
            strategy_version,
            tuple(hedge_symbols),
            recovery_marker,
        )


def _decimal_balances(values: Mapping[str, Any]) -> dict[str, Decimal]:
    if len(values) > 100:
        raise CoverageError("too many assets in coverage lease")
    result: dict[str, Decimal] = {}
    for raw_asset, raw_amount in values.items():
        asset = str(raw_asset).strip().upper()
        if not asset or len(asset) > 32 or asset in result:
            raise CoverageError("coverage balance asset is invalid")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CoverageError(f"coverage balance for {asset} is invalid") from exc
        if not amount.is_finite() or amount < 0:
            raise CoverageError(f"coverage balance for {asset} is invalid")
        result[asset] = amount
    return result


def _balance_payload(values: Mapping[str, Decimal]) -> dict[str, str]:
    parsed = _decimal_balances(values)
    return {asset: str(amount) for asset, amount in sorted(parsed.items())}
