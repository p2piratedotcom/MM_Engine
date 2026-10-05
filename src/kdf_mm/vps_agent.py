from __future__ import annotations

import hmac
import json
import copy
import secrets
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .coin_profile import CoinProfileError, CoinProfileStore
from .coin_registry import CoinRegistry, CoinRegistryError
from .config import Settings
from .coverage import CoverageError, CoverageGuard
from .kdf import KdfError, KdfOrdersDisabled, KdfRpcClient
from .kdf_events import KdfEventStream
from .market_data import MarketDataError, MarketDataStore
from .markets import select_market_specs
from .models import DexSide
from .outbox import HedgeEventOutbox, OutboxConflict
from .ownership import OrderOwnershipStore
from .public_feed import MexcPublicFeed, MexcPublicFeedGroup
from .exchanges import create_client, create_public_reader, supported_venues, load_config
from .exchanges.plugin_catalog import installed_plugins
from .quote_engine import RepricingEngine
from .reconciliation import KdfReconciler, KdfReconciliationError
from .mexc import MexcClient, MexcError
from .gate import GateClient, GateError
from .supervisor import KdfSupervisor, KdfSupervisorError
from .vps_controller import ActiveOrderLimitError, VpsController, quote_plan_payload


MAX_BODY_BYTES = 1_000_000


def _safe_cex_error(exc: MexcError) -> str:
    """Expose a useful failure without reflecting remote payloads or secrets."""
    venue = getattr(exc, "venue", "Gate" if isinstance(exc, GateError) else "MEXC")
    if exc.status is not None:
        return f"{venue} API rejected the request (HTTP {exc.status})"
    message = str(exc)
    if message.startswith("remote API is unreachable"):
        return f"{venue} {message}"
    if exc.payload is None and message.startswith(f"{venue} ") and len(message) <= 200:
        return message
    return f"{venue} API request failed"

# A wallet client must not inherit the operator API's wallet-send, KDF
# lifecycle, coin activation or manual order controls. Keep this list explicit
# as new operator endpoints are added.
WALLET_GET_PATHS = frozenset({
    "/health", "/v1/capabilities", "/v1/status", "/v1/kdf/coins",
    "/v1/markets", "/v1/market", "/v1/strategies/wallet",
    "/v1/strategies", "/v1/orders", "/v1/coverage",
    "/v1/reconciliation", "/v1/events/status", "/v1/repricing",
    "/v1/credentials/status", "/v1/exchanges/balances",
})
WALLET_POST_PATHS = frozenset({
    "/v1/strategies/capacity", "/v1/strategies/scale-preview",
    "/v1/strategies/preview", "/v1/strategies/opposite",
    "/v1/strategies/create", "/v1/strategies/update",
    "/v1/strategies/delete", "/v1/strategies/delete-group",
    "/v1/strategies/start", "/v1/strategies/pause",
    "/v1/strategies/start-all", "/v1/strategies/pause-all",
    "/v1/strategies/scale", "/v1/reconciliation/run",
    "/v1/engine/shutdown", "/v1/credentials/store",
})


# Worker-only routes: the GUI bearer token cannot renew coverage or acknowledge
# hedge events. The local worker receives a separate process-scoped token.
WALLET_WORKER_GET_PATHS = frozenset({"/v1/events"})
WALLET_WORKER_POST_PATHS = frozenset({
    "/v1/events/acknowledge", "/v1/coverage/lease",
    "/v1/coverage/publication-hold",
})

def _owned_payload(order: object) -> dict[str, str | None]:
    raw = asdict(order)
    return {
        key: value.value if hasattr(value, "value") else (None if value is None else str(value))
        for key, value in raw.items()
    }


def _orders_payload(controller, strategies):
    # One coherent read: a withdrawn order must not lose its strategy row.
    # Keep `orders` strictly actionable; strategy placeholders are display-only.
    locks = [strategies.lock] if strategies is not None else []
    locks.append(controller._order_lock)
    acquired = []
    try:
        for lock in locks:
            if not lock.acquire(timeout=0.05):
                cached = getattr(controller, '_dashboard_snapshot', None)
                if cached is None:
                    raise TimeoutError('Order state is being updated; retry shortly')
                return {**copy.deepcopy(cached), 'refresh_pending': True}
            acquired.append(lock)
        result = {"orders": [{**_owned_payload(order),
                              **(strategies.order_display(order) if strategies else {})}
                             for order in controller.ownership.active()],
                  'observed_at_ms': time.time_ns() // 1000000,
                  'refresh_pending': False}
        if strategies is not None:
            result['strategy_states'] = strategies.status()
        controller._dashboard_snapshot = copy.deepcopy(result)
        return result
    finally:
        for lock in reversed(acquired):
            lock.release()



def handler_factory(
    controller: VpsController,
    token: str,
    *,
    supervisor: KdfSupervisor | None = None,
    public_feed: MexcPublicFeed | MexcPublicFeedGroup | None = None,
    repricing: RepricingEngine | None = None,
    reconciliation: KdfReconciler | None = None,
    event_stream: KdfEventStream | None = None,
    outbox: HedgeEventOutbox | None = None,
    coin_profiles: CoinProfileStore | None = None,
    coverage: CoverageGuard | None = None,
    strategies=None,
    wallet_send=None,
    wallet_mode: bool = False,
    cex_profile: str = "default",
    wallet_live_enabled: bool = False,
    wallet_worker_token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    if not token:
        raise ValueError("agent token is required")
    if wallet_worker_token is not None and (not wallet_mode or
            len(wallet_worker_token) < 32 or
            hmac.compare_digest(wallet_worker_token, token)):
        raise ValueError("wallet worker requires a separate private token")

    class AgentHandler(BaseHTTPRequestHandler):
        server_version = "KdfMmAgent/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Keep the default useful log without ever printing headers or bodies.
            super().log_message(format, *args)

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = urlparse(self.path).path
            if wallet_mode and path not in (WALLET_GET_PATHS |
                    (WALLET_WORKER_GET_PATHS if wallet_worker_token else frozenset())):
                self._json(404, {"error": "not found"})
                return
            if path == "/health":
                self._json(200, {"status": "ok"})
                return
            if not self._authenticated():
                return
            if path == "/v1/capabilities" and wallet_mode:
                self._json(200, {"protocol": 1, "service": "MM_Engine",
                                 "kdf_owner": "wallet", "venues": list(supported_venues()),
                                 "plugin_protocol": 1,
                                 "plugins_external": installed_plugins() is not None,
                                 "live_enabled": wallet_live_enabled})
            elif path == "/v1/exchanges/balances" and wallet_mode:
                try:
                    from .exchanges.balances import PreviewBalances
                    from .credentials import LinuxSecretService
                    from .venues import normalize_cex
                    venue = normalize_cex(parse_qs(urlparse(self.path).query).get("venue", ["MEXC"])[0])
                    reader = PreviewBalances(keyring_factory=lambda: LinuxSecretService(profile=cex_profile))
                    balances = reader.read_account(venue)
                    self._json(200, {"venue": venue, "read_only": True,
                        "balances": [{"ticker": key, "available": str(value)}
                                     for key, value in balances.items()]})
                except ValueError as exc:
                    self._json(422, {"error": str(exc)})
            elif path == "/v1/credentials/status" and wallet_mode:
                from .credentials import LinuxSecretService, SecretServiceError
                try:
                    keyring = LinuxSecretService(profile=cex_profile)
                    available = {}
                    for venue in supported_venues():
                        try:
                            keyring.load(venue)
                        except SecretServiceError:
                            available[venue] = False
                        else:
                            available[venue] = True
                    self._json(200, {"venues": available})
                except SecretServiceError as exc:
                    self._json(503, {"error": str(exc)})
            elif path == "/v1/status":
                self._json(200, controller.status())
            elif path == "/v1/wallet/sends":
                self._json(200, wallet_send.history() if wallet_send else {'enabled': False, 'sends': []})
            elif path == "/v1/kdf/status":
                self._json(200, controller.kdf_status())
            elif path == "/v1/kdf/supervisor":
                self._json(
                    200,
                    supervisor.payload()
                    if supervisor is not None
                    else {"state": "UNMANAGED", "managed": False},
                )
            elif path == "/v1/wallet":
                profile_tickers = ()
                if coin_profiles is not None:
                    try:
                        profile_status = coin_profiles.status()
                    except CoinProfileError as exc:
                        self._json(422, {"error": str(exc)})
                        return
                    profile_tickers = tuple(profile_status.get("tickers", ()))
                self._json(
                    200,
                    controller.wallet_status(extra_tickers=profile_tickers),
                )
            elif path == "/v1/kdf/coin-profile":
                if coin_profiles is None:
                    self._json(503, {"error": "coin profiles are disabled"})
                    return
                try:
                    self._json(200, coin_profiles.status())
                except CoinProfileError as exc:
                    self._json(422, {"error": str(exc)})
            elif path == "/v1/kdf/coins":
                query = parse_qs(urlparse(self.path).query)
                search = query.get("query", [""])[0]
                try:
                    limit = _query_integer(query, "limit", default=50, minimum=1)
                    self._json(200, controller.coin_catalog(search, limit=limit))
                except ValueError as exc:
                    self._json(422, {"error": str(exc)})
            elif path == "/v1/inventory":
                self._json(200, controller.inventory_status())
            elif path == "/v1/coverage":
                self._json(200, controller.coverage_status())
            elif path == "/v1/repricing":
                self._json(
                    200,
                    repricing.payload()
                    if repricing is not None
                    else {"state": "DISABLED", "worker_running": False},
                )
            elif path == "/v1/rebalance/context":
                try:
                    from .rebalance import agent_context
                    self._json(200, agent_context(controller, strategies, reconciliation, repricing))
                except Exception as exc:
                    self._json(503, {"error": str(exc)})
            elif path == "/v1/strategies/wallet":
                self._json(200, controller.wallet_status(extra_tickers=controller.enabled_tickers()))
            elif path == "/v1/strategies":
                self._json(200, strategies.status() if strategies else {"strategies": [], "enabled": False})
            elif path == "/v1/reconciliation":
                payload = (
                    reconciliation.payload()
                    if reconciliation is not None
                    else {"ready": False, "worker_running": False}
                )
                payload["event_stream"] = (
                    event_stream.status()
                    if event_stream is not None
                    else {"running": False, "enabled": False}
                )
                self._json(
                    200,
                    payload,
                )
            elif path == "/v1/events/status":
                self._json(
                    200,
                    {"enabled": outbox is not None, **(outbox.status() if outbox else {})},
                )
            elif path == "/v1/events":
                if outbox is None:
                    self._json(503, {"error": "event outbox is disabled"})
                    return
                query = parse_qs(urlparse(self.path).query)
                try:
                    after_event_id = _query_integer(
                        query, "after_event_id", default=0, minimum=0
                    )
                    limit = _query_integer(query, "limit", default=100, minimum=1)
                    events = outbox.events(
                        after_event_id=after_event_id,
                        limit=limit,
                    )
                except ValueError as exc:
                    self._json(422, {"error": str(exc)})
                    return
                self._json(
                    200,
                    {
                        "events": [event.envelope() for event in events],
                        "next_after_event_id": (
                            events[-1].event_id if events else after_event_id
                        ),
                    },
                )
            elif path == "/v1/markets":
                self._json(200, controller.all_market_statuses())
            elif path == "/v1/market":
                query = parse_qs(urlparse(self.path).query)
                market_id = query.get("market_id", [controller.default_market_id])[0]
                try:
                    spec = controller.market_spec(market_id)
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
                    return
                feed_payload = (
                    public_feed.status_for(spec.required_symbols)
                    if isinstance(public_feed, MexcPublicFeedGroup)
                    else asdict(public_feed.status())
                    if isinstance(public_feed, MexcPublicFeed)
                    else {"running": False, "source": "external_snapshot"}
                )
                activity = controller.market_activity()["markets"][market_id]
                if not activity["active"]:
                    self._json(
                        200,
                        {
                            "state": "INACTIVE",
                            "market_id": market_id,
                            "reason": "coin_not_enabled_and_no_live_trade",
                            **activity,
                            "feed": feed_payload,
                        },
                    )
                    return
                try:
                    market = controller.market_status(market_id)
                except MarketDataError as exc:
                    self._json(
                        503,
                        {
                            "state": "UNAVAILABLE",
                            "market_id": market_id,
                            "age_ms": max(
                                (
                                    controller.market_data_by_symbol[symbol].age_ms()
                                    for symbol in spec.required_symbols
                                    if controller.market_data_by_symbol[symbol].age_ms()
                                    is not None
                                ),
                                default=None,
                            ),
                            "error": str(exc),
                            "feed": feed_payload,
                        },
                    )
                    return
                market["state"] = "FRESH"
                market["feed"] = feed_payload
                self._json(200, market)
            elif path == "/v1/orders":
                try:
                    self._json(200, _orders_payload(controller, strategies))
                except TimeoutError as exc:
                    self._json(503, {"error": str(exc)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if wallet_mode and urlparse(self.path).path not in (WALLET_POST_PATHS |
                    (WALLET_WORKER_POST_PATHS if wallet_worker_token else frozenset())):
                self._json(404, {"error": "not found"})
                return
            if not self._authenticated():
                return
            from .rebalance_guard import rebalance_guard
            if urlparse(self.path).path.startswith('/v1/wallet/send/'):
                self._post_authenticated()
                return
            # Safety actions remain available even while a rebalance is unresolved.
            if urlparse(self.path).path in {'/v1/engine/shutdown', '/v1/strategies/pause', '/v1/strategies/pause-all', '/v1/orders/cancel',
                    '/v1/repricing/pause', '/v1/repricing/remove', '/v1/kdf/stop',
                    '/v1/reconciliation/run', '/v1/reconciliation/acknowledge', '/v1/events/acknowledge',
                    '/v1/coverage/publication-hold'}:
                self._post_authenticated()
                return
            if urlparse(self.path).path in {'/v1/kdf/start', '/v1/kdf/activate/coin',
                    '/v1/kdf/coin-profile/activate', '/v1/kdf/activation/coin/status'}:
                # Recovery must be able to enable a coin to read a pending TXID.
                self._post_authenticated()
                return
            try:
                with rebalance_guard(getattr(controller, 'rebalance_lock_path', None)):
                    self._post_authenticated()
            except ValueError as exc:
                self._json(409, {"error": str(exc)})

        def _post_authenticated(self) -> None:
            try:
                payload = self._body()
                path = urlparse(self.path).path
                if path == "/v1/engine/shutdown" and wallet_mode:
                    result = {"stopping": True}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif path == "/v1/credentials/store" and wallet_mode:
                    from .credentials import (GateCredentials, LinuxSecretService,
                                              MexcCredentials)
                    venue = str(payload.get("venue", "")).upper()
                    api_key = payload.get("api_key")
                    api_secret = payload.get("api_secret")
                    if (venue not in supported_venues()
                            or payload.get("confirmation") != "STORE " + venue
                            or not isinstance(api_key, str) or not isinstance(api_secret, str)
                            or not 1 <= len(api_key) <= 512
                            or not 1 <= len(api_secret) <= 512):
                        raise ValueError("credenziali o conferma non valide")
                    keyring = LinuxSecretService(profile=cex_profile)
                    keyring.store(venue, MexcCredentials(api_key, api_secret))
                    result = {"stored": venue}
                elif path.startswith('/v1/wallet/send/'):
                    if wallet_send is None:
                        raise ValueError('Invio wallet non configurato: aggiornare e riavviare il servizio locale.')
                    if path.endswith('/prepare'):
                        result = wallet_send.prepare(payload)
                    elif path.endswith('/status'):
                        result = wallet_send.status(str(payload['id']))
                    elif path.endswith('/cancel'):
                        result = wallet_send.cancel(str(payload['id']))
                    elif path.endswith('/confirm'):
                        if payload.get('confirmation') != 'INVIA ' + str(payload['id']):
                            raise ValueError('Conferma finale mancante: controllare il riepilogo e selezionare Conferma invio.')
                        result = wallet_send.confirm(str(payload['id']))
                    else:
                        raise ValueError('Azione wallet sconosciuta: riaprire la schermata Invia.')
                elif path.startswith("/v1/strategies/"):
                    if strategies is None:
                        raise ValueError("motore strategie non disponibile")
                    if path == "/v1/strategies/capacity":
                        result = strategies.capacity(payload['spec'])
                    elif path == "/v1/strategies/scale-preview":
                        result = strategies.scale_preview(payload)
                    elif path == "/v1/strategies/scale":
                        if payload.get("confirmation") != "PUBBLICA SCALA " + str(payload.get("source_id", "")):
                            raise ValueError("conferma pubblicazione Scala richiesta")
                        result = strategies.scale_publish(payload)
                    elif path == "/v1/strategies/preview":
                        result = strategies.preview_group(payload["specs"])
                    elif path == "/v1/strategies/opposite":
                        result = strategies.opposite(payload["spec"])
                    elif path == "/v1/strategies/create":
                        if payload.get("confirmation") != "SALVA IN PAUSA":
                            raise ValueError("conferma richiesta: SALVA IN PAUSA")
                        result = strategies.create_group(payload["specs"])
                    elif path == "/v1/strategies/update":
                        if payload.get("confirmation") != "AGGIORNA IN PAUSA":
                            raise ValueError("conferma richiesta: AGGIORNA IN PAUSA")
                        result = strategies.replace(payload["spec"])
                    elif path == "/v1/strategies/delete-group":
                        ids = payload.get("strategy_ids")
                        if not isinstance(ids, list) or not 1 <= len(ids) <= 2 or any(not isinstance(sid, str) or not sid for sid in ids):
                            raise ValueError("identificativi strategie non validi")
                        if payload.get("confirmation") != "PAUSA ED ELIMINA " + ",".join(ids):
                            raise ValueError("confermare tutte le strategie da mettere in pausa ed eliminare")
                        result = strategies.delete_group(ids, pause_first=True)
                    elif path == "/v1/strategies/delete":
                        sid = str(payload["strategy_id"])
                        if payload.get("confirmation") != f"ELIMINA {sid}":
                            raise ValueError("confermare l'identificativo esatto della strategia da eliminare")
                        result = strategies.delete(sid)
                    elif path in {"/v1/strategies/start-all", "/v1/strategies/pause-all"}:
                        enabled = path.endswith("/start-all")
                        expected = "AVVIA TUTTE" if enabled else "PAUSA TUTTE"
                        if payload.get("confirmation") != expected:
                            raise ValueError(f"conferma richiesta: {expected}")
                        result = strategies.set_all_enabled(enabled)
                    elif path in {"/v1/strategies/start", "/v1/strategies/pause"}:
                        sid = str(payload["strategy_id"])
                        if path.endswith("/start") and payload.get("confirmation") != f"AVVIA {sid}":
                            raise ValueError("confermare l'identificativo esatto della strategia")
                        result = strategies.set_enabled(sid, path.endswith("/start"))
                    else:
                        raise ValueError("azione strategia sconosciuta")
                elif path == "/v1/market-snapshot":
                    snapshot = controller.ingest_market_snapshot(payload)
                    result: Any = {"accepted": True, "sequence": snapshot.sequence}
                elif path == "/v1/quote/preview":
                    result = quote_plan_payload(self._preview(payload))
                elif path == "/v1/orders/publish":
                    self._pause_repricing("manual_order_publish")
                    plan = self._preview(payload)
                    self._ensure_quote_ready(plan.market_id, plan.dex_side)
                    result = _owned_payload(controller.publish_quote(plan))
                elif path == "/v1/orders/update":
                    self._pause_repricing("manual_order_update")
                    plan = self._preview(payload)
                    self._ensure_quote_ready(plan.market_id, plan.dex_side)
                    result = _owned_payload(
                        controller.update_owned_quote(str(payload["order_uuid"]), plan)
                    )
                elif path == "/v1/orders/cancel":
                    self._pause_repricing("manual_order_cancel")
                    order_uuid = str(payload["order_uuid"])
                    paused = strategies.pause_for_order(order_uuid) if strategies is not None else False
                    result = _owned_payload(controller.ownership.get(order_uuid) if paused else controller.cancel_owned_order(order_uuid))
                elif path == "/v1/repricing/configure":
                    if strategies is not None:
                        strategies.assert_legacy_available(str(payload.get("market_id") or controller.default_market_id), DexSide(str(payload["dex_side"])))
                    engine = self._repricing_engine()
                    result = engine.configure_target(
                        dex_side=DexSide(str(payload["dex_side"])),
                        requested_quantity=Decimal(str(payload["requested_quantity"])),
                        kdf_available_quantity=Decimal(
                            str(payload["kdf_available_quantity"])
                        ),
                        premium=Decimal(str(payload["premium"])),
                        market_id=str(
                            payload.get("market_id", controller.default_market_id)
                        ),
                    )
                elif path == "/v1/repricing/remove":
                    result = self._repricing_engine().remove_target(
                        DexSide(str(payload["dex_side"])),
                        market_id=str(
                            payload.get("market_id", controller.default_market_id)
                        ),
                    )
                elif path == "/v1/repricing/policy":
                    result = self._repricing_engine().configure_policy(
                        min_price_change=Decimal(str(payload["min_price_change"])),
                        min_update_interval_seconds=float(
                            payload["min_update_interval_seconds"]
                        ),
                    )
                elif path == "/v1/repricing/resume":
                    result = self._repricing_engine().resume()
                elif path == "/v1/repricing/pause":
                    result = self._repricing_engine().pause(reason="manual")
                elif path == "/v1/reconciliation/run":
                    result = self._reconciler().reconcile_once()
                elif path == "/v1/reconciliation/acknowledge":
                    result = self._reconciler().acknowledge_quote(
                        str(payload.get("market_id", controller.default_market_id)),
                        DexSide(str(payload["dex_side"])),
                    )
                elif path == "/v1/events/acknowledge":
                    if outbox is None:
                        raise ValueError("event outbox is disabled")
                    result = outbox.acknowledge(
                        int(payload["event_id"]),
                        consumer_id=str(payload["consumer_id"]),
                    ).envelope()
                elif path == "/v1/coverage/lease":
                    if coverage is None:
                        raise CoverageError("MEXC coverage guard is disabled")
                    result = coverage.accept(payload)
                elif path == "/v1/coverage/publication-hold":
                    if coverage is None:
                        raise CoverageError("MEXC coverage guard is disabled")
                    result = coverage.hold_publications(
                        recovery_marker=payload.get("recovery_marker")
                    )
                elif path == "/v1/coverage/override":
                    if coverage is None:
                        raise CoverageError("MEXC coverage guard is disabled")
                    if not isinstance(payload.get("enabled"), bool):
                        raise CoverageError("coverage override enabled must be boolean")
                    result = coverage.set_override(
                        enabled=payload["enabled"],
                        confirmation=str(payload.get("confirmation", "")),
                        duration_seconds=int(
                            payload.get("duration_seconds", coverage.max_override_seconds)
                        ),
                    )
                elif path == "/v1/kdf/activate/arrr":
                    result = controller.activate_arrr()
                elif path == "/v1/kdf/activation/arrr/status":
                    result = controller.arrr_activation_status(int(payload["task_id"]))
                elif path == "/v1/kdf/activate/quote":
                    result = controller.activate_quote_asset()
                elif path == "/v1/kdf/activate/ltc":
                    result = controller.activate_coin("LTC")
                elif path == "/v1/kdf/activate/coin":
                    result = controller.activate_coins([str(payload["ticker"])])
                elif path == "/v1/kdf/deactivate/coin":
                    self._pause_repricing("coin_deactivation")
                    result = controller.deactivate_coin(str(payload["ticker"]))
                elif path == "/v1/kdf/coin-profile/save":
                    if coin_profiles is None:
                        raise CoinProfileError("i profili coin sono disabilitati")
                    profile_tickers, skipped_tickers = (
                        controller.activation_profile_tickers()
                    )
                    result = coin_profiles.save(profile_tickers).payload(
                        path=coin_profiles.path
                    )
                    result["skipped_tickers"] = list(skipped_tickers)
                elif path == "/v1/kdf/coin-profile/activate":
                    if coin_profiles is None:
                        raise CoinProfileError("i profili coin sono disabilitati")
                    profile = coin_profiles.load()
                    result = {
                        "profile": profile.payload(path=coin_profiles.path),
                        **controller.activate_coins(list(profile.tickers)),
                    }
                elif path == "/v1/kdf/activation/quote/status":
                    result = controller.quote_activation_status(int(payload["task_id"]))
                elif path == "/v1/kdf/activation/coin/status":
                    result = controller.activation_status(
                        str(payload["ticker"]), int(payload["task_id"])
                    )
                elif path == "/v1/kdf/start":
                    result = self._supervisor_action("start")
                elif path == "/v1/kdf/stop":
                    result = self._supervisor_action("stop")
                elif path == "/v1/kdf/restart":
                    result = self._supervisor_action("restart")
                else:
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, result)
            except KdfOrdersDisabled as exc:
                self._json(403, {"error": str(exc)})
            except ActiveOrderLimitError as exc:
                self._json(409, {"error": str(exc)})
            except OutboxConflict as exc:
                self._json(409, {"error": str(exc)})
            except CoverageError as exc:
                self._json(409, {"error": str(exc)})
            except KdfReconciliationError as exc:
                self._json(503, {"error": str(exc)})
            except KeyError as exc:
                self._json(404, {"error": f"not found: {exc}"})
            except (
                CoinProfileError,
                CoinRegistryError,
                MarketDataError,
                ValueError,
                ArithmeticError,
            ) as exc:
                self._json(422, {"error": str(exc)})
            except KdfError as exc:
                self._json(502, {"error": str(exc)})
            except KdfSupervisorError as exc:
                self._json(409, {"error": str(exc)})
            except MexcError as exc:
                self._json(502, {"error": _safe_cex_error(exc)})
            except Exception as exc:
                self._json(500, {"error": f"internal agent error: {type(exc).__name__}"})

        def _preview(self, payload: Mapping[str, Any]):
            return controller.preview_quote(
                dex_side=DexSide(str(payload["dex_side"])),
                requested_quantity=Decimal(str(payload["requested_quantity"])),
                kdf_available_quantity=Decimal(str(payload["kdf_available_quantity"])),
                premium=(
                    Decimal(str(payload["premium"]))
                    if payload.get("premium") is not None
                    else None
                ),
                market_id=str(payload.get("market_id", controller.default_market_id)),
            )

        def _supervisor_action(self, action: str) -> Any:
            if supervisor is None:
                raise KdfSupervisorError("KDF process management is disabled")
            if action == "start":
                result = (
                    supervisor.payload()
                    if supervisor.status().managed
                    else asdict(supervisor.start())
                )
                if reconciliation is not None:
                    reconciliation.reconcile_once()
                return result
            if action == "stop":
                self._pause_repricing("kdf_stop")
                return asdict(supervisor.stop())
            if action == "restart":
                self._pause_repricing("kdf_restart")
                result = asdict(supervisor.restart())
                if reconciliation is not None:
                    reconciliation.reconcile_once()
                return result
            raise ValueError("unknown supervisor action")

        def _repricing_engine(self) -> RepricingEngine:
            if repricing is None:
                raise ValueError("automatic repricing is disabled")
            return repricing

        def _pause_repricing(self, reason: str) -> None:
            if repricing is not None and repricing.payload()["state"] == "RUNNING":
                repricing.pause(reason=reason)

        def _reconciler(self) -> KdfReconciler:
            if reconciliation is None:
                raise KdfReconciliationError("KDF reconciliation is disabled")
            return reconciliation

        def _ensure_quote_ready(self, market_id: str, dex_side: DexSide) -> None:
            if strategies is not None:
                strategies.assert_legacy_available(market_id, dex_side)
            if reconciliation is None or not controller.kdf.orders_enabled:
                return
            blocked = reconciliation.block_quote(
                market_id, dex_side
            )
            if blocked is not None:
                raise KdfReconciliationError(blocked)

        def _authenticated(self) -> bool:
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            path = urlparse(self.path).path
            worker_paths = (WALLET_WORKER_GET_PATHS if self.command == "GET"
                            else WALLET_WORKER_POST_PATHS)
            expected = (wallet_worker_token if wallet_mode and path in worker_paths
                        else token)
            if expected and hmac.compare_digest(supplied, expected):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def _body(self) -> Mapping[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request body size")
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValueError("request body is not valid JSON") from exc
            if not isinstance(payload, Mapping):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A read-only desktop client may time out while a slower KDF
                # balance RPC is still completing. The next refresh retries it.
                return

    return AgentHandler


def _query_integer(
    query: Mapping[str, list[str]],
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid query parameter {name}") from exc
    if value < minimum:
        raise ValueError(f"query parameter {name} must be at least {minimum}")
    return value


def build_controller(
    settings: Settings, *, coverage: CoverageGuard | None = None
) -> VpsController:
    state_path = Path(settings.state_db)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ownership = OrderOwnershipStore(state_path)
    kdf = KdfRpcClient(
        rpc_url=settings.kdf_rpc_url,
        userpass=settings.kdf_rpc_userpass,
        orders_enabled=settings.kdf_order_writes,
        diagnostic_path=str(state_path.parent / "kdf-rpc-diagnostics.jsonl"),
    )
    markets = select_market_specs(
        settings.markets,
        primary_symbol=settings.pair,
        primary_quote_ticker=settings.kdf_quote_ticker,
        base_ticker=settings.base_ticker,
        quote_cex_symbols=settings.mexc_quote_symbols,
    )
    symbols = tuple(
        dict.fromkeys(symbol for spec in markets for symbol in spec.required_symbols)
    )
    stores = {
        symbol: MarketDataStore(
            symbol=symbol,
            secret=settings.snapshot_secret,
            max_age_ms=settings.market_data_max_age_ms,
            clock_ms=lambda: time.time_ns() // 1_000_000,
        )
        for symbol in symbols
    }
    market = stores[markets[0].arrr_cex_symbol]
    coin_registry = (
        CoinRegistry.from_manifest(settings.kdf_coins_manifest)
        if settings.kdf_coins_manifest
        else CoinRegistry.from_file(settings.kdf_coins_path)
    )
    controller = VpsController(
        kdf=kdf,
        market_data=market,
        ownership=ownership,
        kdf_quote_ticker=settings.kdf_quote_ticker,
        premium=settings.premium,
        cex_taker_fee=settings.cex_taker_fee,
        risk_buffer=settings.risk_buffer,
        max_slippage=settings.max_slippage,
        max_daily_volume_fraction=settings.max_daily_volume_fraction,
        coin_registry=coin_registry,
        markets=markets,
        market_data_by_symbol=stores,
        base_ticker=settings.base_ticker,
        mexc_base_asset=settings.mexc_base_asset,
        mexc_quote_asset=settings.mexc_quote_asset,
        coverage=coverage,
    )
    controller.rebalance_lock_path = str(Path(settings.desktop_journal_db).resolve()) + ".rebalance.lock"
    return controller


def serve(settings: Settings, *, start_kdf: bool = False, with_mexc: bool = False,
          mexc_profile: str = "default", wallet_mode: bool = False,
          on_ready=None) -> dict[str, object]:
    if wallet_mode and (settings.manage_kdf or start_kdf or settings.agent_bind != "127.0.0.1"):
        raise ValueError("wallet mode must attach to an existing local KDF")
    if not settings.agent_token:
        raise ValueError("KDF_MM_AGENT_TOKEN is required")
    if not settings.snapshot_secret:
        raise ValueError("KDF_MM_SNAPSHOT_SECRET is required")
    if not settings.event_secret:
        raise ValueError("KDF_MM_EVENT_SECRET is required")
    if not settings.kdf_rpc_userpass:
        raise ValueError("KDF_RPC_USERPASS is required")
    coverage = CoverageGuard(
        secret=settings.event_secret,
        required=settings.kdf_order_writes,
        audit_db=settings.coverage_audit_db,
        max_override_seconds=settings.coverage_override_seconds,
    )
    controller = build_controller(settings, coverage=coverage)
    if wallet_mode:
        # Wallet strategies register their own venue-qualified markets/stores
        # below, including paused strategies restored from durable storage.
        # Keep market_data as the signing/clock template, but do not retain CLI
        # seed markets whose stores would have no matching wallet feed.
        controller.markets = {}
        controller.market_data_by_symbol = {}
    coin_profiles = CoinProfileStore(settings.coin_profile_path)
    outbox = HedgeEventOutbox(
        settings.outbox_db,
        secret=settings.event_secret,
        hedge_symbol=settings.pair,
        hedge_base_asset=settings.mexc_base_asset,
        hedge_quote_asset=settings.mexc_quote_asset,
        quote_valuation_resolver=lambda order: controller.quote_usdt_valuation(
            order.market_id, order.dex_side
        ),
    )
    supervisor = (
        KdfSupervisor(
            client=controller.kdf,
            manifest_path=settings.kdf_binary_manifest,
            config_path=settings.kdf_config_path,
            coins_path=settings.kdf_coins_path,
            state_path=settings.kdf_supervisor_state,
            log_path=settings.kdf_log_path,
            start_timeout=settings.kdf_start_timeout_seconds,
        )
        if settings.manage_kdf
        else None
    )
    public_feed = (
        MexcPublicFeedGroup(
            {
                symbol: MexcPublicFeed(
                    client=create_client("MEXC", base_url=settings.mexc_base_url,
                                      timeout=max(1.0, min(4.0, settings.market_data_max_age_ms / 2500))),
                    store=store,
                    snapshot_secret=settings.snapshot_secret,
                    symbol=symbol,
                    interval_seconds=settings.mexc_feed_interval_seconds,
                    depth_limit=settings.mexc_depth_limit,
                )
                for symbol, store in ({} if wallet_mode else controller.market_data_by_symbol).items()
            },
            allow_empty=wallet_mode,
        )
        if settings.mexc_public_feed
        else None
    )
    reconciliation = KdfReconciler(
        kdf=controller.kdf,
        ownership=controller.ownership,
        interval_seconds=settings.reconciliation_interval_seconds,
        recent_swap_limit=settings.reconciliation_recent_swap_limit,
        pool_resolver=controller.inventory_pool,
        active_swap_callback=(
            controller.cancel_sibling_orders
            if controller.kdf.orders_enabled
            else None
        ),
        owned_swap_observer=outbox.observe_swap,
    )
    def resume_guard() -> str | None:
        return (
            reconciliation.resume_block_reason()
            or controller.coverage_block_reason()
        )

    repricing = RepricingEngine(
        controller=controller,
        poll_interval_seconds=settings.repricing_poll_interval_seconds,
        min_price_change=settings.repricing_min_price_change,
        min_update_interval_seconds=settings.repricing_min_update_interval_seconds,
        resume_guard=resume_guard,
        quote_blocker=reconciliation.block_quote,
        state_path=settings.repricing_state_path,
    )
    from .strategy_service import StrategyService
    from .strategy_store import StrategyStore
    from .local_worker import LocalMexcWorker, local_settlement
    strategy_store = StrategyStore(settings.state_db + ".strategies.sqlite3")
    if isinstance(public_feed, MexcPublicFeedGroup):
        public_feed.set_sample_sink(strategy_store.record_market_book_sample)
    from .exchanges.balances import PreviewBalances
    from .credentials import LinuxSecretService
    preview_balances = PreviewBalances(
        keyring_factory=lambda: LinuxSecretService(profile=mexc_profile),
        base_urls={"MEXC": settings.mexc_base_url, "GATE": settings.gate_base_url},
    ) if wallet_mode else None
    strategies = StrategyService(controller=controller, store=strategy_store,
        preview_balances=preview_balances,
        feed_group=public_feed,
        public_client_factory=lambda venue: create_public_reader(
            venue, base_url=getattr(settings, venue.lower()+"_base_url", None)),
        public_clients={
            venue: create_client(venue, base_url=getattr(settings,venue.lower()+"_base_url",None))
            for venue in supported_venues()
        },
        venue_fees={venue: Decimal(load_config(venue).taker_fee) if installed_plugins() is not None
            else getattr(settings, "cex_taker_fee" if venue == "MEXC" else venue.lower()+"_taker_fee", settings.cex_taker_fee)
            for venue in supported_venues()},
        reconciliation=reconciliation, repricing=repricing,
        settlement=local_settlement(
            settings.desktop_journal_db, controller.ownership, controller.kdf
        ))
    outbox.hedge_route_resolver = strategies.hedge_route
    def observe_swap(order, swap, status):
        strategies.observe_swap(order, swap, status)
        return outbox.observe_swap(order, swap, status)
    reconciliation.owned_swap_observer = observe_swap
    event_stream = (
        KdfEventStream(
            kdf=controller.kdf,
            client_id=settings.kdf_event_stream_client_id,
            on_event=lambda _event: reconciliation.notify_event(),
        )
        if settings.kdf_event_stream
        else None
    )

    def pause_on_reconciliation_failure(reason: str) -> None:
        if repricing.payload()["state"] == "RUNNING":
            repricing.pause(reason=reason)

    reconciliation.set_pause_callback(pause_on_reconciliation_failure)
    from .wallet_send import WalletSend
    wallet_send = WalletSend(controller, strategies, repricing, settings.desktop_journal_db,
                             enabled=settings.live_transfers)
    worker_token = secrets.token_urlsafe(48) if wallet_mode and with_mexc else None
    server = ThreadingHTTPServer(
        (settings.agent_bind, settings.agent_port),
        handler_factory(
            controller,
            settings.agent_token,
            supervisor=supervisor,
            public_feed=public_feed,
            repricing=repricing,
            reconciliation=reconciliation,
            event_stream=event_stream,
            outbox=outbox,
            coin_profiles=coin_profiles,
            coverage=coverage,
            strategies=strategies,
            wallet_send=wallet_send,
            wallet_mode=wallet_mode,
            wallet_worker_token=worker_token,
            cex_profile=mexc_profile,
            wallet_live_enabled=(settings.kdf_order_writes and settings.auto_hedge
                                 and settings.live_trading),
        ),
    )
    # Port zero allows the wallet to allocate an unused loopback port. The
    # local worker must use the actual bound port for its own authenticated API.
    settings = replace(settings, agent_port=server.server_address[1])
    controller.start_coin_state_worker()
    stop_monitor = threading.Event()

    def monitor_market_freshness() -> None:
        while not stop_monitor.wait(1.0):
            try:
                if isinstance(public_feed, MexcPublicFeedGroup):
                    public_feed.set_active_symbols(
                        controller.required_market_data_symbols()
                    )
                fresh = controller.enforce_fresh_market_data()
                if not fresh and repricing.payload()["state"] == "RUNNING":
                    repricing.pause(reason="market_data_stale")
                covered = controller.enforce_coverage()
                if not covered and repricing.payload()["state"] == "RUNNING":
                    repricing.pause(reason="coverage_unavailable")
                repricing.observe_auto_resume(
                    pause_reason="coverage_unavailable",
                    healthy=fresh and covered,
                )
                repricing.observe_auto_resume(
                    pause_reason="market_data_stale",
                    healthy=fresh and covered,
                )
            except Exception as exc:
                repricing.observe_auto_resume(
                    pause_reason="coverage_unavailable",
                    healthy=False,
                )
                repricing.observe_auto_resume(
                    pause_reason="market_data_stale",
                    healthy=False,
                )
                if repricing.payload()["state"] == "RUNNING":
                    repricing.pause(reason="market_data_stale_or_cancel_failed")
                print(
                    f"ATTENZIONE: circuit breaker non ha cancellato gli ordini: {exc}",
                    file=sys.stderr,
                )

    monitor = threading.Thread(
        target=monitor_market_freshness,
        name="market-freshness-monitor",
        daemon=True,
    )
    monitor.start()
    local_mexc = None
    shutdown_report: dict[str, object] = {"orders_remaining": 0, "cancel_error": None}
    try:
        if with_mexc:
            local_mexc = LocalMexcWorker(
                replace(settings, agent_token=worker_token) if worker_token else settings,
                profile=mexc_profile)
            local_mexc.start()
        if public_feed is not None:
            if isinstance(public_feed, MexcPublicFeedGroup):
                public_feed.set_active_symbols(
                    controller.required_market_data_symbols()
                )
            public_feed.start()
        if start_kdf:
            if supervisor is None:
                raise ValueError("automatic KDF start requires process management")
            supervisor.start()
        reconciliation.start_worker()
        if event_stream is not None:
            event_stream.start()
        repricing.start_worker()
        strategies.start()
        print(
            f"KDF Agent listening on {settings.agent_bind}:{settings.agent_port} "
            f"({controller.status()['mode']})"
        )
        if on_ready is not None:
            on_ready(settings.agent_port, server)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("KDF VPS Agent stopped")
    finally:
        stop_monitor.set()
        controller.stop_coin_state_worker()
        monitor.join(timeout=2.0)
        strategies.close()
        repricing.stop_worker()
        if event_stream is not None:
            event_stream.stop()
        reconciliation.stop_worker()
        if public_feed is not None:
            public_feed.stop()
        if controller.ownership.active() and controller.kdf.orders_enabled:
            try:
                controller.cancel_all_owned()
            except Exception as exc:
                shutdown_report["cancel_error"] = str(exc)
                print(f"ATTENZIONE: ordini posseduti non cancellati: {exc}", file=sys.stderr)
        shutdown_report["orders_remaining"] = len(controller.ownership.active())
        if supervisor is not None:
            supervisor.stop()
        server.server_close()
        if local_mexc is not None:
            local_mexc.close()
        wallet_send.db.close()
        strategy_store.close()
        outbox.close()
        coverage.close()
        controller.ownership.close()
        from .exchanges.plugin_client import close_plugins
        close_plugins()
    return shutdown_report


def local_settings(settings: Settings, config_path: str | Path) -> Settings:
    path = Path(config_path).resolve()
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("KDF config permissions must be 0600")
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read the local KDF config") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("local KDF config must be a JSON object")
    password = str(loaded.get("rpc_password", ""))
    port = loaded.get("rpcport", 7783)
    if not password or not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("local KDF config has invalid RPC settings")
    return replace(
        settings,
        kdf_config_path=str(path),
        kdf_rpc_url=f"http://127.0.0.1:{port}",
        kdf_rpc_userpass=password,
        manage_kdf=True,
        mexc_public_feed=True,
    )
