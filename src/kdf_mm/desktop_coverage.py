from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .coverage import build_coverage_envelope
from .network_diagnostics import emit as diagnostic
from uuid import uuid4
from .mexc import MexcError
from .exchanges import load_config
from .venues import coverage_asset_key, market_data_key, normalize_cex


class CoveragePublisherError(RuntimeError):
    pass


class MexcCoverageApi(Protocol):
    def synchronize_time(self, *, max_round_trip_ms: int = 2_000) -> Any: ...

    def account(self) -> Mapping[str, Any]: ...

    def self_symbols(self) -> Mapping[str, Any]: ...


class VpsCoverageApi(Protocol):
    def publish_coverage_lease(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def hold_coverage_publications(self, recovery_marker: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CoveragePublishResult:
    state: str
    free_balances: Mapping[str, Decimal]
    expires_at_ms: int | None

    def payload(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["free_balances"] = {
            asset: str(amount) for asset, amount in self.free_balances.items()
        }
        return raw


class DesktopCoveragePublisher:
    """Publishes only selected MEXC Spot balances in a short signed lease."""

    def __init__(
        self,
        *,
        mexc: MexcCoverageApi | None = None,
        clients: Mapping[str, MexcCoverageApi] | None = None,
        vps: VpsCoverageApi,
        event_secret: str,
        consumer_id: str,
        assets: tuple[str, ...],
        ttl_seconds: float = 10.0,
        clock_ms: Any | None = None,
        live_hedging_enabled: bool = False,
        include_all_spot_assets: bool = False,
    ) -> None:
        selected = tuple(dict.fromkeys(asset.strip().upper() for asset in assets))
        if not selected or any(not asset for asset in selected):
            raise ValueError("at least one MEXC coverage asset is required")
        ttl_ms = int(ttl_seconds * 1_000)
        if ttl_ms <= 0 or ttl_ms > 30_000:
            raise ValueError("coverage lease TTL must be in (0, 30] seconds")
        configured = dict(clients or {})
        if mexc is not None:
            configured.setdefault("MEXC", mexc)
        if not configured:
            raise ValueError("at least one Spot exchange client is required")
        self.clients = {normalize_cex(name): client for name, client in configured.items()}
        self.mexc = self.clients.get("MEXC") or next(iter(self.clients.values()))
        self.vps = vps
        self.event_secret = event_secret
        self.consumer_id = consumer_id
        self.assets = selected
        self.ttl_ms = ttl_ms
        self.clock_ms = clock_ms
        self.live_hedging_enabled = bool(live_hedging_enabled)
        self.include_all_spot_assets = include_all_spot_assets
        self.timings_ms: dict[str, float] = {}
        self._time_sync: dict[str, Any] = {}
        self._time_checked: dict[str, float] = {name: float('-inf') for name in self.clients}
        self._symbols_failures: dict[str, int] = {name: 0 for name in self.clients}
        self._symbols_retry_at: dict[str, float] = {name: float('-inf') for name in self.clients}
        self._recovery_marker = time.time_ns() // 1_000_000
        self._publication_hold_pending = False
        self._last_issued_at_ms = 0

    def hold_publications(self) -> None:
        """Fail closed for republishing; do not change the current balance lease."""
        # The next successful lease must complete every renewal stage, even
        # when the timeout happened before this publisher was called.
        self._time_checked = {name: float('-inf') for name in self.clients}
        if not self._publication_hold_pending:
            self._recovery_marker = max(
                self._recovery_marker + 1, time.time_ns() // 1_000_000
            )
            self._publication_hold_pending = True
        self.vps.hold_coverage_publications(self._recovery_marker)

    def _stage(self, name, callback):
        started = time.monotonic()
        identity = uuid4().hex
        outcome, failure = 'received', None
        diagnostic('coverage_stage_start', request_id=identity, phase=name)
        try:
            return callback()
        except Exception as exc:
            outcome = 'error'
            failure = getattr(exc, 'failure_kind', None) or 'stage_error'
            diagnostic('coverage_publication_hold', request_id=identity, phase=name, outcome='error',
                       reason='renewal_failed')
            raise CoveragePublisherError(f"rinnovo copertura, fase {name}: {exc}") from exc
        finally:
            self.timings_ms[name] = round((time.monotonic() - started) * 1000, 2)
            diagnostic('coverage_stage_end', request_id=identity, phase=name, outcome=outcome,
                       failure_kind=failure, elapsed_ms=self.timings_ms[name])

    def publish_once(self) -> CoveragePublishResult:
        self.timings_ms = {}
        # Time synchronization need not delay every fresh balance request.
        # Refresh at most once per minute, and invalidate on any failed cycle.
        try:
            return self._publish_once()
        except Exception:
            self._time_checked = {name: float('-inf') for name in self.clients}
            try:
                self.hold_publications()
            except Exception:
                # Preserve the original renewal failure. Runtime retries the
                # restrictive notification on the next cycle.
                pass
            raise

    def _read(self, client, callback):
        # Only read endpoints. Never alter timeouts/retry policy of hedge writes.
        if getattr(client, "supports_read_deadlines", False) is True:
            venue = next(name for name, value in self.clients.items() if value is client)
            policy = load_config(venue)
            # Two bounded GETs fit inside one coverage lease renewal. Calling
            # the endpoint again creates a new timestamp and signature.
            read_timeout = min(policy.private_read_timeout, self.ttl_ms / 2500)
            for attempt in range(2):
                try:
                    return callback(
                        timeout=read_timeout,
                        total_timeout=read_timeout,
                    )
                except MexcError as exc:
                    retryable = (
                        isinstance(exc.payload, Mapping)
                        and str(exc.payload.get("code")) in policy.timestamp_error_codes
                    ) or (exc.status is None and "(timeout)" in str(exc))
                    if attempt or not retryable:
                        raise
        return callback()

    def _authorized_symbols(self, venue: str, client) -> list[str]:
        # selfSymbols has no symbol parameter. MEXC has occasionally returned
        # -1121 anyway; fail closed and back off rather than hammering the API
        # or reusing an unverified permission set.
        remaining = self._symbols_retry_at[venue] - time.monotonic()
        if remaining > 0:
            raise CoveragePublisherError(
                f"{venue} elenco simboli: riprova tra {remaining:.1f}s; "
                "autorizzazioni non verificate"
            )
        try:
            payload = self._read(client, client.self_symbols)
        except MexcError as exc:
            if (isinstance(exc.payload, Mapping)
                    and str(exc.payload.get('code')) in load_config(venue).symbols_error_codes):
                self._symbols_failures[venue] += 1
                delay = min(30.0, 3.0 * 2 ** min(self._symbols_failures[venue] - 1, 4))
                self._symbols_retry_at[venue] = time.monotonic() + delay
                raise CoveragePublisherError(
                    f"{venue} elenco simboli non disponibile; "
                    f"autorizzazioni non verificate, riprova tra {delay:.0f}s"
                ) from exc
            raise
        self._symbols_failures[venue] = 0
        self._symbols_retry_at[venue] = float('-inf')
        raw = payload.get('data')
        if not isinstance(raw, list) or any(not isinstance(s, str) for s in raw):
            raise CoveragePublisherError(f"simboli Spot {venue} non disponibili")
        return raw

    def _publish_once(self) -> CoveragePublishResult:
        for venue, client in self.clients.items():
            if time.monotonic() - self._time_checked[venue] >= 60:
                self._time_sync[venue] = self._stage(
                    f'{venue.lower()}_time',
                    lambda venue=venue, client=client: client.synchronize_time(
                        max_round_trip_ms=load_config(venue).time_sync_budget_ms),
                )
                self._time_checked[venue] = time.monotonic()
        hedge_symbols: list[str] = []
        if self.include_all_spot_assets:
            for venue, client in self.clients.items():
                raw_symbols = self._stage(
                    f'{venue.lower()}_symbols',
                    lambda venue=venue, client=client: self._authorized_symbols(venue, client),
                )
                hedge_symbols.extend(
                    market_data_key(venue, symbol)
                    for symbol in raw_symbols if symbol.isascii() and symbol.isalnum()
                )
        # Read balances LAST: capability/time requests must not age the lease.
        account_started = time.monotonic()
        # Date the lease at the request, not after all subsequent network calls.
        issued_at_ms = int(self.clock_ms()) if self.clock_ms is not None else time.time_ns() // 1_000_000
        issued_at_ms = max(issued_at_ms, self._last_issued_at_ms + 1)
        selected: dict[str, Decimal] = {}
        for venue, client in self.clients.items():
            balances = self._balances(
                self._stage(f'{venue.lower()}_account',
                            lambda client=client: self._read(client, client.account)),
                venue=venue,
            )
            selected.update({coverage_asset_key(venue, asset): balances.get(asset, Decimal("0"))
                             for asset in self.assets})
            if self.include_all_spot_assets:
                selected.update({coverage_asset_key(venue, asset): amount
                                 for asset, amount in balances.items() if amount > 0})
        if (time.monotonic() - account_started) * 1000 >= self.ttl_ms:
            raise CoveragePublisherError("rinnovo copertura troppo lento: saldi già scaduti; nessun permesso inviato")
        envelope = build_coverage_envelope(
            consumer_id=self.consumer_id,
            free_balances=selected,
            secret=self.event_secret,
            issued_at_ms=issued_at_ms,
            ttl_ms=self.ttl_ms,
            live_hedging_enabled=self.live_hedging_enabled,
            strategy_version=1 if self.include_all_spot_assets else 0,
            hedge_symbols=tuple(hedge_symbols),
            recovery_marker=self._recovery_marker,
        )
        self._last_issued_at_ms = issued_at_ms
        response = self._stage('local_lease_delivery', lambda: self.vps.publish_coverage_lease(envelope))
        if response.get("publication_ready") is False:
            raise CoveragePublisherError("rinnovo consegnato ma ripubblicazione ancora sospesa")
        self._publication_hold_pending = False
        return CoveragePublishResult(
            state=str(response.get("state", "UNKNOWN")),
            free_balances=selected,
            expires_at_ms=(
                int(response["expires_at_ms"])
                if response.get("expires_at_ms") is not None
                else None
            ),
        )

    @staticmethod
    def _balances(payload: Mapping[str, Any], *, venue: str = "MEXC", require_trading: bool = True) -> dict[str, Decimal]:
        if payload.get("accountType") != "SPOT" or (require_trading and payload.get("canTrade") is not True):
            raise CoveragePublisherError(f"{venue} account is not enabled for Spot trading")
        rows = payload.get("balances")
        if not isinstance(rows, list):
            raise CoveragePublisherError(f"{venue} account balances are missing")
        result: dict[str, Decimal] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise CoveragePublisherError(f"{venue} returned an invalid balance row")
            asset = str(row.get("asset", "")).strip().upper()
            if not asset or asset in result:
                raise CoveragePublisherError(f"{venue} returned invalid balance assets")
            try:
                free = Decimal(str(row.get("free")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise CoveragePublisherError(
                    f"{venue} returned an invalid {asset} balance"
                ) from exc
            if not free.is_finite() or free < 0:
                raise CoveragePublisherError(
                    f"{venue} returned an invalid {asset} balance"
                )
            result[asset] = free
        return result
