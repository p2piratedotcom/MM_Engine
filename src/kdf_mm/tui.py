from __future__ import annotations

import curses
import json
import os
import queue
import sys
import threading
import textwrap
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .desktop_status import DesktopJournalStatus


@dataclass(frozen=True, slots=True)
class TuiTheme:
    title: int
    section: int
    muted: int
    success: int
    warning: int
    error: int
    selected: int
    unicode: bool
    menu: int = 0


_HOME_LOGO = (
    " _  __  ____   _____",
    "| |/ / |  _ \\ |  ___|",
    "| ' /  | | | || |_",
    "| . \\ | |_| ||  _|",
    "|_|\\_\\ |____/ |_|   MARKET MAKER",
)

_HOME_MENU = (
    ("KDF", "Stato KDF e attivazione coin"),
    ("WALLET", "Portafoglio KDF"),
    ("STRATEGIES", "Mercato e nuova quotazione"),
    ("ORDERS", "I miei ordini KDF"),
    ("REPRICING", "Repricing automatico"),
    ("SWAPS", "Swap e sicurezza"),
    ("MONITOR", "Eventi e coperture CEX"),
    ("HELP", "Guida dei comandi"),
    ("CEX_BALANCES", "CEX: saldi e riequilibrio"),
)


def _agent_transport_message(exc, *, writing=False):
    timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError)
    message = ('Tempo di risposta del servizio locale scaduto (timeout); non dimostra che KDF sia fermo.'
               if timed_out else 'Connessione al servizio locale interrotta o non disponibile.')
    if writing:
        message += ' Esito della richiesta da verificare: non reinviare.'
    return message


def _agent_error_message(code, body):
    try:
        cause = json.loads(body).get('error')
        if isinstance(cause, str) and cause.strip():
            return cause
    except (ValueError, AttributeError):
        pass
    return f"Servizio locale: errore HTTP {code}. Riprovare la lettura dello stato; verificare i log se persiste."


class AgentApi:
    def __init__(self, *, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path: str) -> Any:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(_agent_error_message(exc.code, message)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(_agent_transport_message(exc)) from exc

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(_agent_error_message(exc.code, message)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(_agent_transport_message(exc, writing=True)) from exc


class TuiSnapshotPoller:
    """Collect slow read-only snapshots without blocking keyboard input."""

    ENDPOINTS = {
        "status": "/v1/status",
        "kdf_status": "/v1/kdf/status",
        "supervisor": "/v1/kdf/supervisor",
        "orders": "/v1/orders",
        "repricing": "/v1/repricing",
        "strategies": "/v1/strategies",
        "reconciliation": "/v1/reconciliation",
        "event_delivery": "/v1/events/status",
        "markets": "/v1/markets",
        "inventory": "/v1/inventory",
        "coverage": "/v1/coverage",
        "wallet": "/v1/wallet",
        "coin_profile": "/v1/kdf/coin-profile",
        "coin_catalog": "/v1/kdf/coins?limit=1",
    }
    OPTIONAL_ENDPOINTS = {"coin_profile", "coin_catalog"}

    def __init__(
        self,
        *,
        api: AgentApi | None,
        desktop_status: DesktopJournalStatus | None,
        market_id: str,
        interval_seconds: float = 3.0,
    ) -> None:
        self.api = api
        self.desktop_status = desktop_status
        self.interval_seconds = interval_seconds
        self._market_id = market_id
        self._market_lock = threading.Lock()
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._snapshots: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(
            target=self._run,
            name="tui-snapshot-poller",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def select_market(self, market_id: str) -> None:
        with self._market_lock:
            self._market_id = market_id
        self.refresh_now()

    def refresh_now(self) -> None:
        self._wakeup.set()

    def latest(self) -> dict[str, Any] | None:
        latest = None
        while True:
            try:
                latest = self._snapshots.get_nowait()
            except queue.Empty:
                return latest

    def close(self) -> None:
        self._stop.set()
        self._wakeup.set()
        self._thread.join(timeout=0.25)

    def _run(self) -> None:
        while not self._stop.is_set():
            payload: dict[str, Any] = {}
            errors: list[str] = []
            with self._market_lock:
                market_id = self._market_id
            if self.api is not None:
                for section, path in self.ENDPOINTS.items():
                    try:
                        payload[section] = self.api.get(path)
                    except RuntimeError as exc:
                        if section not in self.OPTIONAL_ENDPOINTS:
                            errors.append(f"{section}: {exc}")
                try:
                    payload["market"] = self.api.get(
                        f"/v1/market?market_id={market_id}"
                    )
                    payload["market_id"] = market_id
                except RuntimeError as exc:
                    errors.append(f"market: {exc}")
            if self.desktop_status is not None:
                try:
                    payload["desktop_snapshot"] = self.desktop_status.payload()
                except Exception as exc:
                    errors.append(f"desktop: {exc}")
            payload["error"] = "; ".join(errors) if errors else None
            self._publish(payload)
            self._wakeup.wait(self.interval_seconds)
            self._wakeup.clear()

    def _publish(self, payload: dict[str, Any]) -> None:
        try:
            self._snapshots.put_nowait(payload)
            return
        except queue.Full:
            pass
        try:
            self._snapshots.get_nowait()
        except queue.Empty:
            pass
        self._snapshots.put_nowait(payload)


def run_tui(
    *,
    agent_url: str,
    token: str,
    desktop_journal: str | None = None,
    monitor_only: bool = False,
    mexc_profile: str = "default",
) -> None:
    if not monitor_only and not token:
        raise ValueError("KDF_MM_AGENT_TOKEN is required")
    if monitor_only and desktop_journal is None:
        raise ValueError("--monitor-only richiede un journal Desktop")
    api = None if monitor_only else AgentApi(base_url=agent_url, token=token)
    desktop = (
        DesktopJournalStatus(desktop_journal)
        if desktop_journal is not None
        else None
    )
    try:
        from .config import Settings
        from .rebalance import CexRebalanceService
        settings = Settings.from_env()
        journal = desktop_journal or settings.desktop_journal_db
        rebalances = {
            venue: CexRebalanceService(api, settings, journal, venue=venue, profile=mexc_profile)
            for venue in ('MEXC', 'GATE')
        }
        curses.wrapper(_main, api, desktop, rebalances)
    except KeyboardInterrupt:
        # Ctrl+C must restore the terminal without printing an implementation
        # traceback. The polling worker is a daemon and ends with this process.
        return


def _main(
    screen: Any,
    api: AgentApi | None,
    desktop_status: DesktopJournalStatus | None = None,
    rebalance_services=None,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        # Some otherwise usable terminals cannot change cursor visibility.
        pass
    screen.keypad(True)
    screen.timeout(250)
    try:
        curses.set_escdelay(50)
    except (AttributeError, curses.error):
        pass
    theme = _init_theme()
    status: dict[str, Any] = {}
    kdf_status: dict[str, Any] = {}
    supervisor: dict[str, Any] = {}
    market: dict[str, Any] = {}
    markets: dict[str, Any] = {"markets": {"ARRR-USDT-BEP20": {}}}
    wallet: dict[str, Any] = {}
    coin_profile: dict[str, Any] = {"exists": False, "tickers": []}
    coin_catalog: dict[str, Any] = {}
    inventory: dict[str, Any] = {"pools": {}}
    coverage: dict[str, Any] = {"state": "DISABLED", "blocked": False}
    orders: dict[str, Any] = {"orders": []}
    repricing: dict[str, Any] = {}
    strategies: dict[str, Any] = {}
    legacy_repricing = False
    repricing_return = 'HOME'
    repricing_scroll = 0
    orders_scroll = 0
    reconciliation: dict[str, Any] = {}
    event_delivery: dict[str, Any] = {
        "enabled": False,
        "offline": api is None,
    }
    desktop_snapshot: dict[str, Any] = {
        "available": False,
        "reason": "monitor Desktop non configurato",
        "delivery": {},
        "hedges": {},
        "alarms": [],
        "recent": [],
    }
    error: str | None = None
    activation_tasks: dict[str, int] = {}
    last_action = ""
    dex_side = "SELL_ARRR"
    market_id = "ARRR-USDT-BEP20"
    requested_quantity = Decimal("5")
    kdf_available_quantity = Decimal("5")
    premium = Decimal("0.02")
    preview: dict[str, Any] = {}
    view = "MONITOR" if api is None else "HOME"
    selected_cex = "MEXC"
    cex_index = 0
    cex_target = "MONITOR"
    menu_index = 0
    show_help = False
    poller = TuiSnapshotPoller(
        api=api,
        desktop_status=desktop_status,
        market_id=market_id,
    )
    poller.start()

    while True:
        snapshot = poller.latest()
        if snapshot is not None:
            status = snapshot.get("status", status)
            kdf_status = snapshot.get("kdf_status", kdf_status)
            supervisor = snapshot.get("supervisor", supervisor)
            orders = snapshot.get("orders", orders)
            orders = {**orders, '_stale': 'orders' not in snapshot}
            repricing = snapshot.get("repricing", repricing)
            strategies = snapshot.get("strategies", {})  # never label cached data as active
            reconciliation = snapshot.get("reconciliation", reconciliation)
            event_delivery = snapshot.get("event_delivery", event_delivery)
            markets = snapshot.get("markets", markets)
            inventory = snapshot.get("inventory", inventory)
            coverage = snapshot.get("coverage", coverage)
            wallet = snapshot.get("wallet", wallet)
            coin_profile = snapshot.get("coin_profile", coin_profile)
            coin_catalog = snapshot.get("coin_catalog", coin_catalog)
            desktop_snapshot = snapshot.get("desktop_snapshot", desktop_snapshot)
            if snapshot.get("market_id") == market_id:
                market = snapshot.get("market", market)
            error = snapshot.get("error")
            available_markets = tuple(markets.get("markets", {}))
            if available_markets and market_id not in available_markets:
                market_id = available_markets[0]
                poller.select_market(market_id)

        screen.erase()
        if show_help:
            _render_help(screen, theme=theme, monitor_only=api is None)
        elif view == "HOME":
            _render_home(
                screen,
                status=status,
                kdf_status=kdf_status,
                supervisor=supervisor,
                wallet=wallet,
                orders=orders,
                repricing=repricing,
                reconciliation=reconciliation,
                selected_index=menu_index,
                last_action=last_action,
                connection_error=error,
                theme=theme,
            )
        elif view == "KDF":
            _render_kdf_page(
                screen,
                status=status,
                kdf_status=kdf_status,
                supervisor=supervisor,
                wallet=wallet,
                coin_profile=coin_profile,
                coin_catalog=coin_catalog,
                activation_tasks=activation_tasks,
                last_action=last_action,
                connection_error=error,
                theme=theme,
            )
        elif view == "WALLET":
            _render_wallet_page(
                screen,
                wallet=wallet,
                last_action=last_action,
                connection_error=error,
                theme=theme,
            )
        elif view == "CEX_SELECT":
            _render_cex_picker(screen, target=cex_target, selected_index=cex_index, theme=theme)
        elif view == "CEX_BALANCES" and rebalance_services is not None:
            from .rebalance_tui import run_cex_page
            view = run_cex_page(screen, rebalance_services[selected_cex], theme)
            poller.refresh_now()
            continue
        elif view == "STRATEGIES" and api is not None:
            from .strategy_tui import run_strategy_page
            view = run_strategy_page(screen, api, theme)
            poller.refresh_now()
            continue
        elif view == "QUOTE":
            strategy_base = str(
                status.get("base_ticker") or market.get("base_ticker") or "ARRR"
            )
            _render_quote_page(
                screen,
                market=market,
                inventory=inventory,
                orders=orders,
                market_id=market_id,
                dex_side=dex_side,
                requested_quantity=requested_quantity,
                kdf_available_quantity=kdf_available_quantity,
                premium=premium,
                preview=preview,
                last_action=last_action,
                connection_error=error,
                theme=theme,
                base_ticker=strategy_base,
            )
        elif view == "ORDERS":
            _render_orders_page(
                screen,
                orders=orders,
                strategies=strategies,
                scroll=orders_scroll,
                market_id=market_id,
                dex_side=dex_side,
                last_action=last_action,
                connection_error=error,
                theme=theme,
                base_ticker=str(status.get("base_ticker") or "ARRR"),
            )
        elif view == "REPRICING":
            _render_repricing_page(
                screen,
                repricing=repricing,
                strategies=strategies,
                orders=orders,
                legacy_view=legacy_repricing,
                scroll=repricing_scroll,
                market_id=market_id,
                dex_side=dex_side,
                requested_quantity=requested_quantity,
                kdf_available_quantity=kdf_available_quantity,
                premium=premium,
                last_action=last_action,
                connection_error=error,
                theme=theme,
                base_ticker=str(status.get("base_ticker") or "ARRR"),
            )
        elif view == "SWAPS":
            _render_swaps_page(
                screen,
                reconciliation=reconciliation,
                market_id=market_id,
                dex_side=dex_side,
                last_action=last_action,
                connection_error=error,
                theme=theme,
                base_ticker=str(status.get("base_ticker") or "ARRR"),
            )
        elif view == "MONITOR":
            _render_monitor(
                screen,
                event_delivery=event_delivery,
                desktop=desktop_snapshot,
                coverage=coverage,
                cex=selected_cex,
                connection_error=error,
                theme=theme,
            )
        screen.refresh()

        key = _normalize_key(screen, screen.getch())
        if _quit_requested(key, view=view, help_visible=show_help):
            poller.close()
            return
        if key == curses.KEY_RESIZE:
            continue
        if key == ord("?"):
            show_help = not show_help
            continue
        if show_help:
            if key == 27:
                show_help = False
            continue
        if view == "HOME":
            repricing_return = 'HOME'
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                step = -1 if key == curses.KEY_UP else 1
                menu_index = (menu_index + step) % len(_HOME_MENU)
            elif ord("0") <= key < ord("0") + len(_HOME_MENU):
                menu_index = key - ord("0")
                destination = _HOME_MENU[menu_index][0]
                if destination == "HELP":
                    show_help = True
                elif destination in {"MONITOR", "CEX_BALANCES"}:
                    cex_target = destination
                    view = "CEX_SELECT"
                else:
                    view = destination
            elif key in (10, 13, curses.KEY_ENTER):
                destination = _HOME_MENU[menu_index][0]
                if destination == "HELP":
                    show_help = True
                elif destination in {"MONITOR", "CEX_BALANCES"}:
                    cex_target = destination
                    view = "CEX_SELECT"
                else:
                    view = destination
            if key != -1:
                poller.refresh_now()
            continue
        if view == "CEX_SELECT":
            if key == 27:
                view = "HOME"
            elif key in (curses.KEY_UP, curses.KEY_LEFT, curses.KEY_DOWN, curses.KEY_RIGHT, 9):
                cex_index = 1 - cex_index
            elif key in (10, 13, curses.KEY_ENTER):
                selected_cex = ('MEXC', 'GATE')[cex_index]
                view = cex_target
            continue
        if key in (9, ord("z"), ord("Z")) and api is not None:
            if view == "MONITOR":
                view = "HOME"
            else:
                cex_target = "MONITOR"
                view = "CEX_SELECT"
            continue
        if view == 'WALLET' and key in (ord('s'), ord('S')):
            from .wallet_send_tui import run_send_page
            run_send_page(screen, api, wallet, theme)
            poller.refresh_now()
            continue
        if view == "MONITOR":
            if key in (ord("o"), ord("O")) and api is not None:
                confirmation = _prompt_text(
                    screen,
                    "RISCHIO: scrivi FORZA COPERTURA per ignorare i saldi per 5 minuti",
                )
                if confirmation is not None:
                    try:
                        coverage = api.post(
                            "/v1/coverage/override",
                            {
                                "enabled": True,
                                "confirmation": confirmation,
                                "duration_seconds": 300,
                            },
                        )
                        last_action = "Forzatura copertura attiva per massimo 5 minuti"
                        error = None
                    except RuntimeError as exc:
                        error = str(exc)
            elif (
                key in (ord("n"), ord("N"))
                and api is not None
                and coverage.get("override_active")
                and _confirm(screen, "Terminare subito la forzatura copertura? [s/N] ")
            ):
                try:
                    coverage = api.post(
                        "/v1/coverage/override",
                        {
                            "enabled": False,
                            "confirmation": "TERMINA FORZATURA",
                        },
                    )
                    last_action = "Forzatura copertura terminata"
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
            elif key == 27 and api is not None:
                cex_target = "MONITOR"
                view = "CEX_SELECT"
            if key != -1:
                poller.refresh_now()
            continue
        if key == 27:
            view = repricing_return if view == 'REPRICING' else 'HOME'
            repricing_return = 'HOME'
            continue
        if view == 'ORDERS' and key == ord('4'):
            view, legacy_repricing = 'REPRICING', False
            repricing_return = 'ORDERS'
            continue
        if view == 'ORDERS' and key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            orders_scroll = max(0, min(max(0, len(_visible_orders(orders)) - 1),
                orders_scroll + (-1 if key == curses.KEY_PPAGE else 1)))
            continue
        if view == 'ORDERS' and key in tuple(map(ord, 'pPaA')):
            rows = strategies.get('strategies')
            if not isinstance(rows, list):
                last_action = 'Stato strategie non disponibile: aggiornare e riprovare.'
                error = None
            else:
                enable_all = key in (ord('a'), ord('A'))
                targets = ([row for row in rows if not row.get('enabled') and row.get('state') == 'PAUSED']
                           if enable_all else [row for row in rows if row.get('enabled')])
                verb = 'Riattivare' if enable_all else 'Mettere in pausa'
                consequence = ('Le strategie torneranno abilitate e potranno pubblicare ordini.' if enable_all else
                               'Gli ordini KDF associati verranno ritirati in modo controllato.')
                if not targets:
                    last_action = ('Nessuna strategia in pausa da riattivare.' if enable_all else
                                   'Tutte le strategie risultano già in pausa.')
                    error = None
                elif _confirm(screen, f'{verb} {len(targets)} strategie? {consequence} [s/N] '):
                    try:
                        path = '/v1/strategies/start-all' if enable_all else '/v1/strategies/pause-all'
                        phrase = 'AVVIA TUTTE' if enable_all else 'PAUSA TUTTE'
                        strategies = api.post(path, {'confirmation': phrase})
                        changed = len(strategies.get('changed_strategy_ids', ()))
                        last_action = (f'{changed} strategie riattivate.' if enable_all else
                                       f'{changed} strategie messe in pausa; ordini associati ritirati.')
                        error = None
                    except RuntimeError as exc:
                        error = str(exc)
            poller.refresh_now()
            continue
        if view == "REPRICING":
            if key in (ord('l'), ord('L')):
                legacy_repricing = not legacy_repricing
                continue
            if key == ord('2'):
                view = 'STRATEGIES'
                continue
            if not legacy_repricing:
                if key in (curses.KEY_UP, curses.KEY_DOWN):
                    repricing_scroll = max(0, min(max(0, len(strategies.get('strategies', [])) - 1),
                        repricing_scroll + (-1 if key == curses.KEY_UP else 1)))
                if key in tuple(map(ord, 'gGhHeEdDtTfF')):
                    last_action = 'Gestisci avvio, pausa e parametri delle strategie dalla pagina [2].'
                continue  # no legacy write shortcuts in the strategy overview
        if view in {"QUOTE", "ORDERS", "REPRICING", "SWAPS"} and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            available_markets = tuple(markets.get("markets", {}))
            if available_markets:
                position = available_markets.index(market_id)
                step = -1 if key == curses.KEY_LEFT else 1
                market_id = available_markets[(position + step) % len(available_markets)]
                poller.select_market(market_id)
                preview = {}
                last_action = f"Mercato selezionato: {market_id}"
            continue
        if view in {"QUOTE", "ORDERS", "REPRICING", "SWAPS"} and key in (curses.KEY_UP, curses.KEY_DOWN):
            dex_side = "BUY_ARRR" if dex_side == "SELL_ARRR" else "SELL_ARRR"
            preview = {}
            last_action = f"Lato selezionato: {dex_side}"
            continue
        if view == "KDF" and key in (ord("k"), ord("K")):
            try:
                supervisor = api.post("/v1/kdf/start", {})
                last_action = "KDF avviata dal Supervisor"
                error = None
            except RuntimeError as exc:
                error = str(exc)
        if view == "KDF" and key in (ord("x"), ord("X")) and _confirm(screen, "Arrestare KDF? [s/N] "):
            try:
                supervisor = api.post("/v1/kdf/stop", {})
                last_action = "KDF arrestata"
                error = None
            except RuntimeError as exc:
                error = str(exc)
        if view == "KDF" and key in (ord("r"), ord("R")):
            if _confirm(screen, "Riavviare KDF? [s/N] "):
                try:
                    supervisor = api.post("/v1/kdf/restart", {})
                    last_action = "KDF riavviata"
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "KDF" and key in (ord("a"), ord("A")):
            ticker = _prompt_text(screen, "Ticker da attivare (es. BTC o USDC-BEP20)")
            if ticker is not None:
                try:
                    result = api.post(
                        "/v1/kdf/activate/coin", {"ticker": ticker.upper()}
                    )
                    _remember_activation_tasks(
                        activation_tasks, result, fallback_ticker=ticker
                    )
                    last_action = _activation_batch_message(
                        result, fallback_ticker=ticker
                    )
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "KDF" and key in (ord("d"), ord("D")):
            ticker = _prompt_text(screen, "Ticker attivo da disattivare")
            selected_ticker = ticker.strip().upper() if ticker else ""
            if selected_ticker and _confirm(
                screen,
                (
                    f"Disattivare {selected_ticker}? KDF cancellera tutti gli "
                    "ordini che usano questa coin. [s/N] "
                ),
            ):
                try:
                    result = api.post(
                        "/v1/kdf/deactivate/coin",
                        {"ticker": selected_ticker},
                    )
                    cancelled = len(result.get("cancelled_orders", ()))
                    last_action = (
                        f"{selected_ticker} disattivata; "
                        f"{cancelled} ordini cancellati da KDF. "
                        "Il profilo salvato non e stato modificato"
                    )
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view in {"QUOTE", "ORDERS", "REPRICING", "SWAPS"} and key in (ord("b"), ord("B")):
            available_markets = tuple(markets.get("markets", {}))
            if available_markets:
                position = available_markets.index(market_id)
                market_id = available_markets[(position + 1) % len(available_markets)]
                poller.select_market(market_id)
                preview = {}
                last_action = f"Mercato selezionato: {market_id}"
        if view in {"QUOTE", "ORDERS", "REPRICING", "SWAPS"} and key in (ord("l"), ord("L")):
            dex_side = "BUY_ARRR" if dex_side == "SELL_ARRR" else "SELL_ARRR"
            preview = {}
            last_action = f"Lato selezionato: {dex_side}"
        if view in {"QUOTE", "REPRICING"} and key in (ord("n"), ord("N")):
            base_ticker = str(status.get("base_ticker") or "ARRR")
            value = _prompt_decimal(
                screen, f"Quantita {base_ticker}", requested_quantity
            )
            if value is not None and value > 0:
                requested_quantity = value
                preview = {}
        if view in {"QUOTE", "REPRICING"} and key in (ord("i"), ord("I")):
            value = _prompt_decimal(
                screen,
                f"Disponibilita massima KDF in {str(status.get('base_ticker') or 'ARRR')}",
                kdf_available_quantity,
            )
            if value is not None and value > 0:
                kdf_available_quantity = value
                preview = {}
        if view in {"QUOTE", "REPRICING"} and key in (ord("p"), ord("P")):
            value = _prompt_decimal(screen, "Premium decimale (0.02 = 2%)", premium)
            if value is not None:
                premium = value
                preview = {}
        if view == "QUOTE" and key in (10, 13, curses.KEY_ENTER, ord("v"), ord("V")):
            try:
                preview = api.post(
                    "/v1/quote/preview",
                    _quote_payload(
                        dex_side,
                        market_id,
                        requested_quantity,
                        kdf_available_quantity,
                        premium,
                    ),
                )
                last_action = "Anteprima aggiornata; nessun ordine creato"
                error = None
            except RuntimeError as exc:
                error = str(exc)

        if view == "QUOTE" and key in (ord("o"), ord("O")):
            if _confirm(screen, "Pubblicare l'ordine KDF mostrato? [s/N] "):
                try:
                    result = api.post(
                        "/v1/orders/publish",
                        _quote_payload(
                            dex_side,
                            market_id,
                            requested_quantity,
                            kdf_available_quantity,
                            premium,
                        ),
                    )
                    last_action = f"Ordine pubblicato: {result.get('order_uuid', '-')}"
                    preview = {}
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "REPRICING" and key in (ord("e"), ord("E")):
            if _confirm(
                screen,
                f"Configurare repricing automatico {dex_side} con questi valori? [s/N] ",
            ):
                try:
                    repricing = api.post(
                        "/v1/repricing/configure",
                        _quote_payload(
                            dex_side,
                            market_id,
                            requested_quantity,
                            kdf_available_quantity,
                            premium,
                        ),
                    )
                    last_action = f"Repricing configurato per {dex_side}; stato invariato"
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "REPRICING" and key in (ord("d"), ord("D")):
            if _confirm(
                screen,
                f"Rimuovere {dex_side} dal repricing? L'ordine resta aperto. [s/N] ",
            ):
                try:
                    repricing = api.post(
                        "/v1/repricing/remove",
                        {"dex_side": dex_side, "market_id": market_id},
                    )
                    last_action = f"Repricing rimosso per {dex_side}; ordine non cancellato"
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "REPRICING" and key in (ord("t"), ord("T")):
            try:
                current = Decimal(str(repricing.get("min_price_change", "0.0025")))
                value = _prompt_decimal(
                    screen,
                    "Soglia variazione prezzo (0.0025 = 0.25%)",
                    current,
                )
                if value is not None and 0 <= value < 1:
                    repricing = api.post(
                        "/v1/repricing/policy",
                        {
                            "min_price_change": str(value),
                            "min_update_interval_seconds": str(
                                repricing.get("min_update_interval_seconds", 15)
                            ),
                        },
                    )
                    last_action = f"Soglia repricing impostata a {value}"
                    error = None
            except (InvalidOperation, RuntimeError) as exc:
                error = str(exc)
        if view == "REPRICING" and key in (ord("f"), ord("F")):
            try:
                current = Decimal(
                    str(repricing.get("min_update_interval_seconds", 15))
                )
                value = _prompt_decimal(
                    screen,
                    "Intervallo minimo tra scritture, secondi",
                    current,
                )
                if value is not None and value > 0:
                    repricing = api.post(
                        "/v1/repricing/policy",
                        {
                            "min_price_change": str(
                                repricing.get("min_price_change", "0.0025")
                            ),
                            "min_update_interval_seconds": str(value),
                        },
                    )
                    last_action = f"Intervallo minimo repricing impostato a {value}s"
                    error = None
            except (InvalidOperation, RuntimeError) as exc:
                error = str(exc)
        if view == "REPRICING":
            repricing_action = _repricing_control_action(
                key, str(repricing.get("state", "PAUSED"))
            )
            if repricing_action == "resume":
                mode = status.get("mode", "SCONOSCIUTA")
                if repricing.get("state") == "RUNNING":
                    last_action = "Repricing gia attivo"
                    error = None
                elif _confirm(
                    screen,
                    f"Riprendere il repricing? Modalita {mode}. [s/N] ",
                ):
                    try:
                        repricing = api.post("/v1/repricing/resume", {})
                        last_action = f"Repricing ripreso in modalita {mode}"
                        error = None
                    except RuntimeError as exc:
                        error = str(exc)
            elif repricing_action == "pause":
                try:
                    repricing = api.post("/v1/repricing/pause", {})
                    last_action = (
                        "Repricing in pausa; gli ordini aperti restano su KDF"
                    )
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "SWAPS" and key in (ord("j"), ord("J")):
            try:
                reconciliation = api.post("/v1/reconciliation/run", {})
                last_action = "Stato ordini e swap riletto da KDF"
                error = None
            except RuntimeError as exc:
                error = str(exc)
        if view == "SWAPS" and key in (ord("y"), ord("Y")):
            if _confirm(
                screen,
                f"Confermare {dex_side} solo dopo aver verificato lo swap? [s/N] ",
            ):
                try:
                    reconciliation = api.post(
                        "/v1/reconciliation/acknowledge",
                        {"dex_side": dex_side, "market_id": market_id},
                    )
                    last_action = (
                        f"Verifica manuale registrata per {dex_side}; "
                        "il lato puo essere riattivato"
                    )
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view in {"QUOTE", "ORDERS"} and key in (ord("m"), ord("M")):
            selected_order = _first_order_for_quote(orders, market_id, dex_side)
            if selected_order:
                order_uuid = str(selected_order.get("order_uuid", ""))
                if order_uuid and _confirm(
                    screen, f"Aggiornare {_short(order_uuid, 18)} con la quota corrente? [s/N] "
                ):
                    try:
                        payload = _quote_payload(
                            dex_side,
                            market_id,
                            requested_quantity,
                            kdf_available_quantity,
                            premium,
                        )
                        payload["order_uuid"] = order_uuid
                        api.post("/v1/orders/update", payload)
                        last_action = f"Ordine aggiornato: {order_uuid}"
                        preview = {}
                        error = None
                    except RuntimeError as exc:
                        error = str(exc)
            else:
                last_action = f"Nessun ordine {dex_side} gestito da aggiornare"
        if view in {"QUOTE", "ORDERS"} and key in (ord("c"), ord("C")):
            selected_order = _first_order_for_quote(orders, market_id, dex_side)
            if selected_order:
                order_uuid = str(selected_order.get("order_uuid", ""))
                if order_uuid and _confirm(
                    screen, f"Cancellare {_short(order_uuid, 18)}? [s/N] "
                ):
                    try:
                        api.post("/v1/orders/cancel", {"order_uuid": order_uuid})
                        last_action = f"Ordine cancellato: {order_uuid}"
                        error = None
                    except RuntimeError as exc:
                        error = str(exc)
            else:
                last_action = f"Nessun ordine {dex_side} gestito da cancellare"
        if view == "KDF" and key in (ord("v"), ord("V")):
            try:
                coin_profile = api.post("/v1/kdf/coin-profile/save", {})
                saved = ", ".join(coin_profile.get("tickers", ()))
                skipped = ", ".join(coin_profile.get("skipped_tickers", ()))
                last_action = f"Profilo coin salvato: {saved}"
                if skipped:
                    last_action += f" · esclusi (handler mancante): {skipped}"
                error = None
            except RuntimeError as exc:
                error = str(exc)
        if view == "KDF" and key in (ord("p"), ord("P")):
            profile_tickers = tuple(coin_profile.get("tickers", ()))
            if not profile_tickers:
                last_action = "Nessun profilo coin salvato"
            elif _confirm(
                screen,
                f"Attivare il profilo ({len(profile_tickers)} coin)? [s/N] ",
            ):
                try:
                    result = api.post("/v1/kdf/coin-profile/activate", {})
                    _remember_activation_tasks(activation_tasks, result)
                    last_action = _activation_batch_message(result)
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
        if view == "KDF" and key in (ord("s"), ord("S")):
            try:
                task_states: list[str] = []
                details = []
                inspected = set(activation_tasks)
                finished: list[str] = []
                for ticker, task_id in tuple(activation_tasks.items()):
                    result = api.post(
                        "/v1/kdf/activation/coin/status",
                        {"ticker": ticker, "task_id": task_id},
                    )
                    state = str(result.get("status", "SCONOSCIUTO"))
                    task_states.append(f"{ticker}={state}")
                    details.append(f"{ticker}: {state} — {json.dumps(result.get('details', ''), ensure_ascii=False)}")
                    if state in {"Ok", "Error", "Cancelled"}:
                        finished.append(ticker)
                for ticker in finished:
                    activation_tasks.pop(ticker, None)
                last_action = "  ".join(task_states) if task_states else "Nessun task avviato"
                error = None
                for ticker, result in kdf_status.get("activations", {}).items():
                    if ticker not in inspected:
                        details.append(f"{ticker}: {result.get('status')} — {json.dumps(result.get('details', ''), ensure_ascii=False)}")
                if details:
                    _activation_details(screen, details, theme)
            except RuntimeError as exc:
                error = str(exc)

        if key != -1:
            poller.refresh_now()


def _init_theme() -> TuiTheme:
    unicode_terminal = "utf" in (sys.stdout.encoding or "").lower()
    plain = TuiTheme(
        title=curses.A_BOLD,
        section=curses.A_BOLD,
        muted=curses.A_DIM,
        success=curses.A_BOLD,
        warning=curses.A_BOLD,
        error=curses.A_BOLD,
        selected=curses.A_REVERSE | curses.A_BOLD,
        unicode=unicode_terminal,
        menu=curses.A_BOLD,
    )
    if os.environ.get("NO_COLOR"):
        return plain
    try:
        if not curses.has_colors():
            return plain
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, background)
        curses.init_pair(2, curses.COLOR_GREEN, background)
        curses.init_pair(3, curses.COLOR_YELLOW, background)
        curses.init_pair(4, curses.COLOR_RED, background)
        curses.init_pair(5, curses.COLOR_BLUE, background)
        return TuiTheme(
            title=curses.A_BOLD | curses.color_pair(2),
            section=curses.A_BOLD | curses.color_pair(1),
            muted=curses.A_DIM,
            success=curses.A_BOLD | curses.color_pair(2),
            warning=curses.A_BOLD | curses.color_pair(3),
            error=curses.A_BOLD | curses.color_pair(4),
            selected=curses.A_REVERSE | curses.A_BOLD | curses.color_pair(5),
            unicode=unicode_terminal,
            menu=curses.A_BOLD | curses.color_pair(5),
        )
    except curses.error:
        return plain


def _normalize_key(screen: Any, key: int) -> int:
    """Accept both terminal arrow encodings and swallow unknown escape tails.

    ``keypad(True)`` normally turns arrows into ``curses.KEY_*`` values. Some
    terminals can still send the alternate CSI form; without consuming its
    tail, the final ``C`` or ``D`` could be mistaken for an order command.
    """
    if key != 27:
        return key
    try:
        screen.timeout(30)
        prefix = screen.getch()
        if prefix == -1:
            return 27
        if prefix not in (ord("["), ord("O")):
            return -1
        suffix = screen.getch()
        return {
            ord("A"): curses.KEY_UP,
            ord("B"): curses.KEY_DOWN,
            ord("C"): curses.KEY_RIGHT,
            ord("D"): curses.KEY_LEFT,
        }.get(suffix, -1)
    finally:
        screen.timeout(250)


def _quit_requested(key: int, *, view: str, help_visible: bool) -> bool:
    if key == 3:
        return True
    return (
        view == "HOME"
        and not help_visible
        and key in (ord("q"), ord("Q"))
    )


def _render_home(
    screen: Any,
    *,
    status: dict[str, Any],
    kdf_status: dict[str, Any],
    supervisor: dict[str, Any],
    wallet: dict[str, Any],
    orders: dict[str, Any],
    repricing: dict[str, Any],
    reconciliation: dict[str, Any],
    selected_index: int,
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    height, width = screen.getmaxyx()
    if width < 64 or height < 24:
        _render_size_gate(
            screen,
            theme=theme,
            current=(width, height),
            allow_quit=True,
        )
        return

    for row, line in enumerate(_HOME_LOGO):
        _center(screen, row, line, theme.title)
    _center(screen, 6, "Console per atomic swap · KDF + CEX", theme.section)

    balances = wallet.get("balances", {})
    active_coins = sum(bool(item.get("available")) for item in balances.values())
    total_coins = len(balances)
    kdf_running = _kdf_active(supervisor)
    rpc_ready = bool(kdf_status.get("reachable"))
    status_style = theme.success if kdf_running and rpc_ready else theme.warning
    _center(
        screen,
        8,
        (
            f"[KDF {_human_kdf_state(supervisor.get('state'))}]  "
            f"[RPC {'OK' if rpc_ready else 'NON PRONTO'}]  "
            f"[{active_coins}/{total_coins} COIN ATTIVE]"
        ),
        status_style,
    )
    _center(
        screen,
        9,
        (
            f"[Ordini: {len(orders.get('orders', []))}]  "
            f"[Swap: {reconciliation.get('active_owned_swaps', 0)}]  "
            f"[Problemi: {reconciliation.get('problem_orders', 0)}]  "
            f"[Repricing: {repricing.get('state', 'SCONOSCIUTO')}]"
        ),
        theme.warning,
    )
    _center(screen, 10, _human_mode(status.get("mode")), theme.muted)

    _write(screen, 11, max(2, (width - 46) // 2), "MENU PRINCIPALE", theme.section)
    menu_x = max(2, (width - 46) // 2)
    for index, (_, label) in enumerate(_HOME_MENU):
        selected = index == selected_index
        pointer = ">" if selected else " "
        _write(
            screen,
            12 + index,
            menu_x,
            f"{pointer} [{index}] {label}",
            theme.selected if selected else theme.menu,
        )

    _action_message(
        screen,
        last_action=last_action,
        connection_error=connection_error,
        theme=theme,
    )
    _footer(
        screen,
        "[↑/↓] scegli   [Invio o 0-7] apri",
        "[?] guida   [Q] esci",
        theme,
    )


def _render_kdf_page(
    screen: Any,
    *,
    status: dict[str, Any],
    kdf_status: dict[str, Any],
    supervisor: dict[str, Any],
    wallet: dict[str, Any],
    coin_profile: dict[str, Any],
    coin_catalog: dict[str, Any],
    activation_tasks: dict[str, int],
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    if not _page_header(screen, "STATO KDF E ATTIVAZIONE COIN", theme):
        return
    running = _kdf_active(supervisor)
    reachable = bool(kdf_status.get("reachable"))
    rows = (
        (4, "Processo KDF", f"{_mark(running)} {_human_kdf_state(supervisor.get('state'))}", running),
        (5, "Collegamento RPC", f"{_mark(reachable)} {'RAGGIUNGIBILE' if reachable else 'NON PRONTO'}", reachable),
        (6, "Modalità", _human_mode(status.get("mode")), True),
        (
            7,
            "Coin attivabili",
            str(coin_catalog.get("supported_total", "—")),
            bool(coin_catalog.get("supported_total")),
        ),
    )
    for row, label, value, ok in rows:
        _write(screen, row, 3, f"{label:<24} {value}", theme.success if ok else theme.warning)

    profile_tickers = tuple(coin_profile.get("tickers", ()))
    profile_text = ", ".join(map(str, profile_tickers)) if profile_tickers else "non salvato"
    task_text = ", ".join(
        f"{ticker} #{task_id}" for ticker, task_id in activation_tasks.items()
    ) or "nessuno"
    _write(screen, 9, 3, f"Profilo salvato          {profile_text}", theme.success if profile_tickers else theme.muted)
    _write(screen, 10, 3, f"Attivazioni in corso     {task_text}", theme.warning if activation_tasks else theme.muted)
    activations = kdf_status.get("activations", {})
    if activations:
        labels = {"Error": "tutti i nodi falliti", "Ok": "attiva", "InProgress": "tentativi in corso", "Unknown": "esito da verificare", "Cancelled": "annullata"}
        summary = " | ".join(f"{coin}: {labels.get(item.get('status'), item.get('status'))}" for coin, item in activations.items())
        _write(screen, 11, 3, f"[S] dettagli nodi: {summary}", theme.warning)

    _section(screen, 12, "COIN ATTIVE E SALDI", theme)
    _write(screen, 13, 3, f"{'COIN':<18}{'STATO':<18}{'SALDO':>18}", theme.muted)
    _write(screen, 14, 3, "-" * 54, theme.section)
    balances = wallet.get("balances", {})
    for offset, (ticker, item) in enumerate(tuple(balances.items())[:5]):
        active = bool(item.get("available"))
        balance = _number(item.get("balance")) if active else "—"
        _write(
            screen,
            15 + offset,
            3,
            f"{ticker:<18}{('[OK] ATTIVA' if active else '[--] NON ATTIVA'):<18}{balance:>18}",
            theme.success if active else theme.warning,
        )
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        "[K] avvia   [X] arresta   [R] riavvia   [A] attiva   [D] disattiva",
        "[V] salva attive   [P] attiva profilo   [S] stato attivazioni   [Esc] menu",
        theme,
    )


def _render_wallet_page(
    screen: Any,
    *,
    wallet: dict[str, Any],
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    if not _page_header(screen, "PORTAFOGLIO KDF", theme):
        return
    _write(screen, 4, 2, f"{'COIN':<16}{'STATO':<14}{'SALDO':>16}  INDIRIZZO", theme.muted)
    _write(screen, 5, 2, "-" * 74, theme.section)
    balances = wallet.get("balances", {})
    if not balances:
        _write(screen, 7, 2, "Nessuna coin disponibile. Attivale dalla pagina [0].", theme.warning)
    for offset, (ticker, item) in enumerate(tuple(balances.items())[:14]):
        active = bool(item.get("available"))
        balance = _number(item.get("balance")) if active else "—"
        address = str(item.get("address") or "—")
        _write(
            screen,
            6 + offset,
            2,
            f"{ticker:<16}{('[OK] ATTIVA' if active else '[--] FERMA'):<14}{balance:>16}  {address}",
            theme.success if active else theme.warning,
        )
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        "[S] Invia coin / storico invii — conferma finale obbligatoria",
        "[Esc] menu   [?] guida",
        theme,
    )


def _render_quote_page(
    screen: Any,
    *,
    market: dict[str, Any],
    inventory: dict[str, Any],
    orders: dict[str, Any],
    market_id: str,
    dex_side: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
    preview: dict[str, Any],
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
    base_ticker: str = "ARRR",
) -> None:
    if not _page_header(screen, "MERCATO E NUOVA QUOTAZIONE", theme):
        return
    base_ticker = str(market.get("base_ticker") or base_ticker).upper()
    quote_currency = str(
        market.get("quote_ticker")
        or market_id.removeprefix(f"{base_ticker}-")
    )
    fresh = market.get("state") == "FRESH"
    _write(screen, 4, 2, f"Mercato       {_mark(fresh)} {market_id}  ·  {_human_side(dex_side, base_ticker)}", theme.selected)
    _write(screen, 5, 2, f"Operazione    {_route(dex_side, base_ticker, quote_currency)}", theme.section)
    _write(
        screen,
        6,
        2,
        f"CEX            Bid {_number(market.get('best_bid'))}  ·  Ask {_number(market.get('best_ask'))}  ·  Spread {_percent(market.get('spread_fraction'))}",
    )
    _write(
        screen,
        7,
        2,
        f"Liquidità      Tetto vendi {_number(market.get('suggested_sell_base_max', market.get('suggested_sell_arrr_max')), 4)}  ·  compra {_number(market.get('suggested_buy_base_max', market.get('suggested_buy_arrr_max')), 4)} {base_ticker}  ·  età {_age(market.get('age_ms'))}",
        theme.success if fresh else theme.warning,
    )

    _section(screen, 9, "PARAMETRI ORDINE", theme)
    sign = "+" if premium >= 0 else ""
    _write(screen, 10, 3, f"Quantità {base_ticker:<13} {_number(requested_quantity)}")
    _write(screen, 11, 3, f"Disponibilità massima  {_number(kdf_available_quantity)} {base_ticker}")
    _write(screen, 12, 3, f"Premium                {sign}{_percent(premium)}", theme.warning)

    _section(screen, 14, "ANTEPRIMA — NON PUBBLICA", theme)
    if preview:
        _write(
            screen,
            15,
            3,
            f"Prezzo proposto   {_number(preview.get('human_price_quote_per_base', preview.get('human_price_quote_per_arrr')))} {preview.get('quote_currency', quote_currency)}/{base_ticker}",
            theme.success,
        )
        _write(screen, 16, 3, f"Riferimento VWAP  {_number(preview.get('reference_vwap'))}")
        _write(
            screen,
            17,
            3,
            f"Scostamento eff. {_percent(preview.get('effective_edge'))}  ·  fee {_percent(preview.get('cex_taker_fee'))}  ·  buffer {_percent(preview.get('risk_buffer'))}",
        )
    else:
        _write(screen, 15, 3, "Premi Invio per calcolare il prezzo. Nessun ordine verrà creato.", theme.muted)
    pool_name = base_ticker if dex_side == "SELL_ARRR" else quote_currency
    pool = inventory.get("pools", {}).get(pool_name, {})
    _write(
        screen,
        19,
        3,
        f"Pool {pool_name}: disponibile {_number(pool.get('max_maker_volume'))}  ·  bloccato {_number(pool.get('locked_by_swaps'))}  ·  ordini aperti {len(orders.get('orders', []))}",
    )
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        "[←/→] mercato   [↑/↓] lato   [N] quantità   [I] limite   [P] premium",
        "[Invio] anteprima   [O] pubblica   [M] aggiorna   [C] cancella   [Esc] menu",
        theme,
    )


def _order_table_number(value, *, premium=False):
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            return 'n/d'
        return f'{(n * 100).normalize():+.6g}%' if premium else f'{n.normalize():.8g}'
    except (InvalidOperation, ValueError, TypeError):
        return 'n/d'


def _visible_orders(payload):
    """Stable logical rows; placeholders never enter the actionable orders list."""
    rows = [dict(order) for order in payload.get('orders', [])]
    states = payload.get('strategy_states', {})
    for strategy in states.get('strategies', []):
        if strategy.get('state') == 'DELETED':
            continue
        sid, spec = strategy['id'], strategy['spec']
        bound = [row for row in rows if row.get('strategy_id') == sid]
        if bound:
            for row in bound:
                row['status'] = 'AGGIORNAMENTO' if strategy.get('state') == 'WRITING' else row.get('status', 'OPEN')
            continue
        base, quote = spec.get('base', {}).get('ticker', '?'), spec.get('quote', {}).get('ticker', '?')
        sold, bought = (base, quote) if spec.get('side') == 'SELL_ARRR' else (quote, base)
        state = strategy.get('state')
        label = {'PAUSED': 'IN PAUSA', 'WAITING': 'SOSPESO', 'STABILIZING': 'IN ATTESA',
                 'WRITING': 'PUBBLICAZIONE', 'REVIEW_REQUIRED': 'DA VERIFICARE',
                 'EXHAUSTED': 'ESAURITO'}.get(state, 'NON PUBBLICATO')
        rows.append({'strategy_id': sid, 'kdf_base': sold, 'kdf_rel': bought,
                     'configured_premium': spec.get('premium'), 'status': label,
                     'display_only': True, 'detail': strategy.get('detail') or
                     'Nessun ordine pubblicato; parametri e avvio nella pagina [2].'})
    rows.sort(key=lambda row: (str(row.get('strategy_id') or row.get('order_uuid', '')),
                               str(row.get('order_uuid', ''))))
    if payload.get('_stale'):
        for row in rows:
            row['status'] = 'NON VERIFICATO'
    return rows


def _order_table(orders, width):
    extra = any(o.get('price_usdt_needed', o.get('kdf_rel') not in {'USDT', 'USDT-BEP20', 'USDT-ERC20'}) for o in orders)
    headers = ['N', 'VENDI > COMPRA', 'QUANTITA', 'PREZZO', 'PREMIUM'] + (['USDT/COIN'] if extra else []) + ['STATO']
    rows = []
    for i, o in enumerate(orders, 1):
        row = [str(i), f"{o.get('kdf_base', '?')}>{o.get('kdf_rel', '?')}",
               _order_table_number(o.get('kdf_volume')), _order_table_number(o.get('kdf_price')),
               _order_table_number(o.get('configured_premium'), premium=True)]
        if extra:
            value = _order_table_number(o.get('price_usdt'))
            row.append('già PREZZO' if o.get('price_usdt_needed') is False else
                       value + ('/' + str(o.get('valuation_asset', '?')) if value != 'n/d' else ''))
        row.append(str(o.get('status', 'n/d')))
        rows.append(row)
    sizes = [max(_cell_width(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    compact = sum(sizes) + 2 * (len(headers) - 1) > width
    start = 2 if compact else 0
    def line(row):
        return '  '.join((' ' * max(0, sizes[i] - _cell_width(row[i])) + row[i]) for i in range(start, len(headers)))
    rendered = [[f'{row[0]}. {row[1]}', line(row)] if compact else [line(row)] for row in rows]
    for order, block in zip(orders, rendered):
        if order.get('display_only'):
            detail = textwrap.wrap('Non pubblicato: ' + str(order.get('detail', '')),
                                   width=max(20, width - 2))
            block.extend('  ' + part for part in detail[:2])
            if len(detail) > 2:
                block[-1] = block[-1][:max(0, width - 18)] + '… dettagli [4]'
    return line(headers), rendered


def _render_orders_page(
    screen: Any,
    *,
    orders: dict[str, Any],
    market_id: str,
    dex_side: str,
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
    base_ticker: str = "ARRR",
    strategies: dict[str, Any] | None = None,
    scroll: int = 0,
) -> None:
    if not _page_header(screen, "I MIEI ORDINI KDF", theme):
        return
    _write(screen, 4, 2, f"Filtro azioni: {market_id} · {_human_side(dex_side, base_ticker)}", theme.selected)
    if strategies is not None:
        summary = ('Elenco non aggiornato: stato NON VERIFICATO' if orders.get('_stale') else
                   _strategy_repricing_summary(orders.get('strategy_states', strategies)))
        _write(screen, 5, 2, summary + ' — dettagli [4]', theme.section)
    height, width = screen.getmaxyx()
    active_orders = _visible_orders(orders)
    header, blocks = _order_table(active_orders, width - 5)
    _write(screen, 6, 2, header, theme.muted)
    _write(screen, 7, 2, '-' * (width - 5), theme.section)
    if not active_orders:
        _write(screen, 9, 2, "Nessun ordine gestito aperto.", theme.warning)
    y = 8
    start = min(scroll, max(0, len(blocks) - 1))
    for index, block in enumerate(blocks[start:], start):
        if y + len(block) > height - (7 if orders.get('_stale') else 6):
            break
        for line in block:
            _write(screen, y, 2, line, theme.warning if active_orders[index].get('display_only') or orders.get('_stale') else theme.success)
            y += 1
    if orders.get('_stale'):
        _write(screen, height - 7, 2, 'Dati non aggiornati: ultimo elenco noto, stato da verificare.', theme.warning)
    _write(screen, height - 6, 2, 'PREZZO: coin comprata/venduta. PREMIUM: impostato, senza fee.', theme.muted)
    _write(screen, height - 5, 2, 'n/d: nessun valore pubblicato/disponibile. USDT non è USD.', theme.muted)
    _write(screen, height - 4, 2, 'M/C: ordine del filtro. [Pg↑/Pg↓] scorri ordini. [4] repricing.', theme.muted)
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        "[←/→] mercato   [↑/↓] lato/filtro   [M] aggiorna   [C] cancella",
        "[P] pausa tutte   [A] riattiva tutte   [Esc] menu   [?] guida",
        theme,
    )


def _strategy_repricing_label(row, worker_running):
    spec = row.get('spec', {})
    if spec.get('price_mode') == 'fixed':
        return 'PREZZO FISSO (nessun repricing)'
    if spec.get('price_mode') != 'auto':
        return 'REPRICING NON DISPONIBILE'
    if worker_running is None:
        return 'REPRICING NON VERIFICATO'
    if not worker_running:
        return 'REPRICING FERMO: motore non avviato'
    if not row.get('enabled'):
        return 'REPRICING IN PAUSA' if row.get('state') == 'PAUSED' else 'REPRICING FERMO: ' + str(row.get('state', '?'))
    state = row.get('state')
    if state in {'RUNNING', 'STABILIZING', 'WRITING'}:
        suffix = {'RUNNING': '', 'STABILIZING': ' — attesa stabilità/intervallo', 'WRITING': ' — aggiornamento in corso'}[state]
        return 'REPRICING ATTIVO' + suffix
    return 'REPRICING SOSPESO: ' + str(state or 'stato non disponibile')


def _strategy_repricing_summary(payload):
    if not isinstance(payload.get('strategies'), list) or 'worker_running' not in payload:
        return 'Repricing strategie: NON DISPONIBILE'
    rows = payload['strategies']
    active = sum(_strategy_repricing_label(r, payload['worker_running']).startswith('REPRICING ATTIVO') for r in rows)
    fixed = sum(r.get('spec', {}).get('price_mode') == 'fixed' for r in rows)
    return f'Repricing strategie: ATTIVO {active} | fisso {fixed} | non attivo {len(rows) - active - fixed}'


def _render_strategy_repricing(screen, strategies, orders, legacy, scroll, theme):
    _write(screen, 4, 2, _strategy_repricing_summary(strategies), theme.section)
    _write(screen, 5, 2, f"Ordini KDF pubblicati: {len(orders.get('orders', []))} (diversi dalle strategie abilitate)")
    _write(screen, 6, 2, 'Avvio e pausa per singola strategia: pagina [2].')
    rows = strategies.get('strategies', [])
    height, _ = screen.getmaxyx()
    capacity = max(1, (height - 14) // 4)
    start = min(scroll, max(0, len(rows) - capacity))
    if not rows:
        _write(screen, 8, 2, 'Nessuna strategia configurata.' if 'strategies' in strategies else 'Stato strategie non disponibile.', theme.warning)
    for index, row in enumerate(rows[start:start + capacity]):
        y = 8 + index * 4
        spec = row.get('spec', {})
        base, quote = spec.get('base', {}).get('ticker', '?'), spec.get('quote', {}).get('ticker', '?')
        sold, bought = (base, quote) if spec.get('side') == 'SELL_ARRR' else (quote, base)
        label = _strategy_repricing_label(row, strategies.get('worker_running'))
        _write(screen, y, 2, f"{row.get('id', '?')}  {sold} -> {bought}", theme.section)
        _write(screen, y + 1, 3, label, theme.success if label.startswith('REPRICING ATTIVO') else theme.warning)
        _write(screen, y + 2, 3, f"Intervallo min. {spec.get('update_seconds', '?')} s | soglia {_percent(spec.get('price_threshold'))}")
        _write(screen, y + 3, 3, str(row.get('detail') or 'Nessun avviso'), theme.muted)
    configured = bool(legacy.get('quotes', legacy.get('sides', {})))
    _write(screen, height - 5, 2, 'Motore precedente: ' + (str(legacy.get('state', 'NON DISPONIBILE')) + ' — dettagli [L]' if configured
        else 'NON UTILIZZATO (nessun lato configurato)'), theme.muted)
    _write(screen, height - 4, 2, 'La pausa del motore precedente NON ferma le strategie.', theme.warning)
    _footer(screen, '[↑/↓] scorri strategie   [2] gestisci strategie', '[L] motore precedente   [Esc] indietro', theme)


def _render_repricing_page(
    screen: Any,
    *,
    repricing: dict[str, Any],
    market_id: str,
    dex_side: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
    base_ticker: str = "ARRR",
    strategies: dict[str, Any] | None = None,
    orders: dict[str, Any] | None = None,
    legacy_view: bool = False,
    scroll: int = 0,
) -> None:
    if not _page_header(screen, "REPRICING AUTOMATICO", theme):
        return
    if strategies is not None and not legacy_view:
        _render_strategy_repricing(screen, strategies, orders or {}, repricing, scroll, theme)
        _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
        return
    if legacy_view:
        _write(screen, 3, 2, 'MOTORE PRECEDENTE — non controlla le strategie della pagina [2]', theme.warning)
    running = repricing.get("state") == "RUNNING"
    _write(
        screen,
        4,
        2,
        f"Stato                 {_mark(running)} {repricing.get('state', 'SCONOSCIUTO')}",
        theme.success if running else theme.warning,
    )
    _write(screen, 5, 2, f"Soglia variazione     {_percent(repricing.get('min_price_change'))}")
    _write(screen, 6, 2, f"Intervallo minimo     {repricing.get('min_update_interval_seconds', '—')} secondi")

    quote_key = f"{market_id}:{dex_side}"
    selected = repricing.get("quotes", repricing.get("sides", {})).get(
        quote_key, repricing.get("sides", {}).get(dex_side, {})
    )
    if not running:
        _write(
            screen,
            7,
            2,
            f"Motivo pausa          {_human_pause_reason(repricing.get('pause_reason'))}",
            theme.warning,
        )
        resume_block = _repricing_resume_block(repricing, selected)
        _write(
            screen,
            8,
            2,
            (
                f"Ripresa               [!] BLOCCATA: {resume_block}"
                if resume_block
                else "Ripresa               [OK] pronta: premi G oppure H"
            ),
            theme.error if resume_block else theme.success,
        )

    _section(screen, 10, "CONFIGURAZIONE SELEZIONATA", theme)
    _write(screen, 11, 3, f"Mercato               {market_id}", theme.selected)
    _write(screen, 12, 3, f"Lato                  {_human_side(dex_side, base_ticker)}")
    _write(screen, 13, 3, f"Quantità              {_number(requested_quantity)} {base_ticker}")
    _write(screen, 14, 3, f"Disponibilità KDF     {_number(kdf_available_quantity)} {base_ticker}")
    _write(screen, 15, 3, f"Premium               {('+' if premium >= 0 else '')}{_percent(premium)}")
    _write(screen, 17, 3, f"Ultima azione         {selected.get('last_action', 'non configurato')}", theme.muted)
    _write(screen, 19, 2, "La pausa non cancella gli ordini già aperti su KDF.", theme.warning)
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        (
            "[E] configura   [D] rimuovi   [H] pausa"
            if running
            else "[E] configura   [D] rimuovi   [G/H] riprendi"
        ),
        "[T/F] soglia/intervallo   [←/→↑↓] mercato/lato   [L] strategie   [Esc] menu",
        theme,
    )


def _repricing_control_action(key: int, state: str) -> str | None:
    if key in (ord("g"), ord("G")):
        return "resume"
    if key in (ord("h"), ord("H")):
        return "pause" if state == "RUNNING" else "resume"
    return None


def _repricing_resume_block(
    repricing: dict[str, Any], selected: dict[str, Any]
) -> str | None:
    failed_resume = repricing.get("resume_block_reason")
    if failed_resume:
        return str(failed_resume)
    quotes = repricing.get("quotes", repricing.get("sides", {}))
    if not quotes:
        return "configura almeno un lato con E"
    if repricing.get("orders_enabled"):
        reconciliation_guard = repricing.get("reconciliation_guard")
        if reconciliation_guard:
            return str(reconciliation_guard)
        quote_block = selected.get("reconciliation_block")
        if quote_block:
            return str(quote_block)
    return None


def _human_pause_reason(reason: Any) -> str:
    value = str(reason or "manual")
    return {
        "manual": "manuale",
        "market_data_stale": "prezzi di mercato non aggiornati",
        "market_data_stale_or_cancel_failed": (
            "prezzi non aggiornati o cancellazione ordini fallita"
        ),
        "order_limit_violation": "più di un ordine attivo sullo stesso lato",
        "service_stopped": "servizio arrestato",
        "kdf_reconciliation_failed": "controllo ordini e swap KDF fallito",
        "coverage_unavailable": "copertura CEX assente, scaduta o insufficiente",
    }.get(value, value)


def _render_swaps_page(
    screen: Any,
    *,
    reconciliation: dict[str, Any],
    market_id: str,
    dex_side: str,
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
    base_ticker: str = "ARRR",
) -> None:
    if not _page_header(screen, "SWAP E SICUREZZA", theme):
        return
    ready = bool(reconciliation.get("ready"))
    _write(
        screen,
        4,
        2,
        f"Controllo KDF          {_mark(ready)} {'PRONTO' if ready else 'ATTENZIONE RICHIESTA'}",
        theme.success if ready else theme.error,
    )
    _write(screen, 6, 2, f"Swap attivi            {reconciliation.get('active_owned_swaps', '—')}")
    _write(screen, 7, 2, f"Swap da confermare     {reconciliation.get('terminal_swaps_to_acknowledge', '—')}")
    _write(screen, 8, 2, f"Ordini problematici   {reconciliation.get('problem_orders', '—')}")
    _section(screen, 10, "CONTESTO DELLA VERIFICA", theme)
    _write(screen, 11, 3, f"Mercato selezionato   {market_id}", theme.selected)
    _write(screen, 12, 3, f"Lato selezionato      {_human_side(dex_side, base_ticker)}")
    _write(screen, 14, 3, "J rilegge ordini e swap direttamente da KDF.", theme.muted)
    _write(screen, 15, 3, "Y registra una conferma solo dopo la tua verifica manuale.", theme.warning)
    _write(screen, 17, 3, "Questa pagina non esegue coperture CEX e non sposta fondi.", theme.muted)
    _action_message(screen, last_action=last_action, connection_error=connection_error, theme=theme)
    _footer(
        screen,
        "[←/→] mercato   [↑/↓] lato   [J] rileggi KDF   [Y] conferma verifica",
        "[Tab] eventi/coperture   [Esc] menu   [?] guida",
        theme,
    )


def _page_header(screen: Any, title: str, theme: TuiTheme) -> bool:
    height, width = screen.getmaxyx()
    if width < 64 or height < 24:
        _render_size_gate(screen, theme=theme, current=(width, height))
        return False
    _center(screen, 0, "KDF MARKET MAKER", theme.title)
    _write(screen, 1, 2, f"MENU PRINCIPALE  >  {title}", theme.section)
    _write(screen, 2, 2, "=" * max(0, width - 5), theme.section)
    return True


def _activation_details(screen, details, theme):
    offset = 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        _write(screen, 0, 1, "ATTIVAZIONE COIN — ESITO DEI NODI", theme.title)
        lines = [line for item in details for line in (textwrap.wrap(item, max(10, width - 4)) + [""])]
        page = max(1, height - 4)
        offset = min(offset, max(0, len(lines) - page))
        for row, line in enumerate(lines[offset:offset + page], 2):
            _write(screen, row, 1, line, theme.warning)
        _write(screen, height - 1, 1, "[↑/↓] scorri   [Esc/Invio] indietro", theme.menu)
        screen.refresh()
        key = screen.getch()
        if key in (27, 10, 13):
            return
        if key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_UP:
            offset = max(0, offset - 1)


def _action_message(
    screen: Any,
    *,
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    height, _ = screen.getmaxyx()
    if connection_error:
        _write(screen, height - 3, 2, f"[ERRORE] {connection_error}", theme.error)
    elif last_action:
        _write(screen, height - 3, 2, f"[OK] {last_action}", theme.success)


def _footer(screen: Any, first: str, second: str, theme: TuiTheme) -> None:
    height, _ = screen.getmaxyx()
    _write(screen, height - 2, 0, first, theme.menu)
    _write(screen, height - 1, 0, second, theme.warning)


def _center(screen: Any, row: int, text: str, style: int = 0) -> None:
    _, width = screen.getmaxyx()
    _write(screen, row, max(0, (width - _cell_width(text)) // 2), text, style)


def _render_trading(
    screen: Any,
    *,
    status: dict[str, Any],
    kdf_status: dict[str, Any],
    supervisor: dict[str, Any],
    market: dict[str, Any],
    wallet: dict[str, Any],
    inventory: dict[str, Any],
    orders: dict[str, Any],
    repricing: dict[str, Any],
    reconciliation: dict[str, Any],
    market_id: str,
    dex_side: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
    preview: dict[str, Any],
    last_action: str,
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    height, width = screen.getmaxyx()
    if width < 64 or height < 24:
        _render_size_gate(screen, theme=theme, current=(width, height))
        return

    mode = _human_mode(status.get("mode"))
    _write(screen, 0, 0, f"KDF MARKET MAKER  ·  OPERAZIONI  ·  {mode}", theme.title)
    supervisor_running = _kdf_active(supervisor)
    rpc_ready = bool(kdf_status.get("reachable"))
    market_ready = market.get("state") == "FRESH"
    service_style = (
        theme.success
        if supervisor_running and rpc_ready and market_ready and not connection_error
        else theme.warning
    )
    _write(
        screen,
        1,
        0,
        "  ".join(
            (
                f"KDF {_mark(supervisor_running)} {supervisor.get('state', 'SCONOSCIUTO')}",
                f"RPC {_mark(rpc_ready)}",
                f"MERCATO {_mark(market_ready)} {market.get('state', 'SCONOSCIUTO')}",
                f"ORDINI {len(orders.get('orders', []))}",
            )
        ),
        service_style,
    )

    render = (
        _render_trading_wide
        if width >= 110 and height >= 25
        else _render_trading_compact
    )
    render(
        screen,
        market=market,
        wallet=wallet,
        inventory=inventory,
        orders=orders,
        repricing=repricing,
        reconciliation=reconciliation,
        market_id=market_id,
        dex_side=dex_side,
        requested_quantity=requested_quantity,
        kdf_available_quantity=kdf_available_quantity,
        premium=premium,
        preview=preview,
        theme=theme,
    )
    message = (
        f"[ERRORE] {connection_error}"
        if connection_error
        else f"[OK] {last_action}"
        if last_action
        else "Pronto. Seleziona mercato e lato, poi premi Invio per l'anteprima."
    )
    _write(
        screen,
        height - 3,
        0,
        message,
        theme.error if connection_error else theme.success if last_action else theme.muted,
    )
    _write(
        screen,
        height - 2,
        0,
        "[←/→] mercato  [↑/↓] lato  [Invio] anteprima  [Tab] eventi",
        theme.selected,
    )
    _write(
        screen,
        height - 1,
        0,
        "[N] quantità  [P] premium  [O] pubblica  [?] tutti i comandi  [Esc] menu",
        theme.section,
    )


def _render_trading_wide(
    screen: Any,
    *,
    market: dict[str, Any],
    wallet: dict[str, Any],
    inventory: dict[str, Any],
    orders: dict[str, Any],
    repricing: dict[str, Any],
    reconciliation: dict[str, Any],
    market_id: str,
    dex_side: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
    preview: dict[str, Any],
    theme: TuiTheme,
) -> None:
    base_ticker = str(market.get("base_ticker") or "ARRR").upper()
    height, width = screen.getmaxyx()
    left_width = max(62, int(width * 0.58))
    right_x = left_width + 1
    right_width = width - right_x - 1
    top_y, top_height = 3, 8
    lower_y = top_y + top_height + 1
    lower_height = min(11, height - 3 - lower_y)

    _draw_panel(screen, top_y, 0, top_height, left_width, "MERCATO", theme)
    _draw_panel(screen, top_y, right_x, top_height, right_width, "PORTAFOGLIO KDF", theme)
    _draw_panel(screen, lower_y, 0, lower_height, left_width, "QUOTAZIONE", theme)
    _draw_panel(screen, lower_y, right_x, lower_height, right_width, "AUTOMAZIONE E SICUREZZA", theme)

    market_state = str(market.get("state", "SCONOSCIUTO"))
    _panel_write(
        screen, top_y, 0, top_height, left_width, 0,
        f"{_mark(market_state == 'FRESH')} {market_id}  {market_state}  ·  età {_age(market.get('age_ms'))}",
        theme.selected,
    )
    symbols = market.get("required_symbols") or market.get("symbol", "ARRRUSDT")
    if isinstance(symbols, list):
        symbols = " + ".join(map(str, symbols))
    _panel_write(screen, top_y, 0, top_height, left_width, 1, f"Fonte CEX: {symbols}", theme.muted)
    _panel_write(screen, top_y, 0, top_height, left_width, 2, f"Bid  {_number(market.get('best_bid'))}")
    _panel_write(screen, top_y, 0, top_height, left_width, 3, f"Ask  {_number(market.get('best_ask'))}")
    _panel_write(
        screen, top_y, 0, top_height, left_width, 4,
        f"Spread {_percent(market.get('spread_fraction'))}  ·  Volume 24h {_number(market.get('base_volume_24h'), 2)} {base_ticker}",
    )
    _panel_write(
        screen, top_y, 0, top_height, left_width, 5,
        f"Tetto: vendi {_number(market.get('suggested_sell_base_max', market.get('suggested_sell_arrr_max')), 4)} · compra {_number(market.get('suggested_buy_base_max', market.get('suggested_buy_arrr_max')), 4)} {base_ticker}",
    )

    balances = wallet.get("balances", {})
    _panel_write(screen, top_y, right_x, top_height, right_width, 0, "ASSET          SALDO", theme.muted)
    for offset, (ticker, item) in enumerate(tuple(balances.items())[:4], start=1):
        available = bool(item.get("available"))
        balance = _number(item.get("balance")) if available else "non attiva"
        _panel_write(
            screen, top_y, right_x, top_height, right_width, offset,
            f"{_mark(available)} {ticker:<12} {balance:>16}",
            theme.success if available else theme.warning,
        )
    if len(balances) > 4:
        _panel_write(
            screen, top_y, right_x, top_height, right_width, 5,
            f"+ altre {len(balances) - 4} coin nella GUI", theme.muted,
        )

    quote_currency = str(
        market.get("quote_ticker")
        or market_id.removeprefix(f"{base_ticker}-")
    )
    pool_name = base_ticker if dex_side == "SELL_ARRR" else quote_currency
    pool = inventory.get("pools", {}).get(pool_name, {})
    active_orders = orders.get("orders", [])
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 0,
        f"{_human_side(dex_side, base_ticker)}  ·  {_route(dex_side, base_ticker, quote_currency)}",
        theme.selected,
    )
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 1,
        f"Quantità {base_ticker:<8} {_number(requested_quantity)}",
    )
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 2,
        f"Limite KDF        {_number(kdf_available_quantity)} {base_ticker}",
    )
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 3,
        f"Premium           {_percent(premium)} ({premium})",
        theme.warning if premium < 0 else 0,
    )
    if preview:
        _panel_write(
            screen, lower_y, 0, lower_height, left_width, 4,
            f"Prezzo proposto   {_number(preview.get('human_price_quote_per_base', preview.get('human_price_quote_per_arrr')))} {preview.get('quote_currency', quote_currency)}/{base_ticker}",
            theme.success,
        )
        _panel_write(
            screen, lower_y, 0, lower_height, left_width, 5,
            f"Riferimento VWAP  {_number(preview.get('reference_vwap'))}",
        )
    else:
        _panel_write(
            screen, lower_y, 0, lower_height, left_width, 4,
            "Premi Invio per calcolare; non crea ordini.", theme.muted,
        )
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 6,
        f"Pool {pool_name}: disponibile {_number(pool.get('max_maker_volume'))} · bloccato {_number(pool.get('locked_by_swaps'))}",
    )
    _panel_write(
        screen, lower_y, 0, lower_height, left_width, 7,
        f"Ordini gestiti aperti: {len(active_orders)}",
    )

    quote_key = f"{market_id}:{dex_side}"
    selected_auto = repricing.get("quotes", repricing.get("sides", {})).get(
        quote_key, repricing.get("sides", {}).get(dex_side, {})
    )
    repricing_running = repricing.get("state") == "RUNNING"
    ready = bool(reconciliation.get("ready"))
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 0,
        f"{_mark(repricing_running)} Repricing {repricing.get('state', 'SCONOSCIUTO')}",
        theme.success if repricing_running else theme.warning,
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 1,
        f"Soglia variazione  {_percent(repricing.get('min_price_change'))}",
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 2,
        f"Intervallo minimo  {repricing.get('min_update_interval_seconds', '-')} s",
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 3,
        f"Ultima azione      {selected_auto.get('last_action', 'non configurato')}",
        theme.muted,
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 5,
        f"{_mark(ready)} Controllo KDF {'PRONTO' if ready else 'NON PRONTO'}",
        theme.success if ready else theme.warning,
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 6,
        f"Swap attivi {reconciliation.get('active_owned_swaps', '-')}  ·  da confermare {reconciliation.get('terminal_swaps_to_acknowledge', '-')}",
    )
    _panel_write(
        screen, lower_y, right_x, lower_height, right_width, 7,
        f"Ordini problematici {reconciliation.get('problem_orders', '-')}",
        theme.error if reconciliation.get("problem_orders") else 0,
    )


def _render_trading_compact(
    screen: Any,
    *,
    market: dict[str, Any],
    wallet: dict[str, Any],
    inventory: dict[str, Any],
    orders: dict[str, Any],
    repricing: dict[str, Any],
    reconciliation: dict[str, Any],
    market_id: str,
    dex_side: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
    preview: dict[str, Any],
    theme: TuiTheme,
) -> None:
    base_ticker = str(market.get("base_ticker") or "ARRR").upper()
    _section(screen, 2, "MERCATO", theme)
    market_state = str(market.get("state", "SCONOSCIUTO"))
    _write(
        screen, 3, 0,
        f"{_mark(market_state == 'FRESH')} {market_id}  {market_state}  ·  età {_age(market.get('age_ms'))}",
        theme.selected,
    )
    _write(
        screen, 4, 0,
        f"Bid {_number(market.get('best_bid'))}  ·  Ask {_number(market.get('best_ask'))}  ·  Spread {_percent(market.get('spread_fraction'))}",
    )
    _write(
        screen, 5, 0,
        f"Tetto: vendi {_number(market.get('suggested_sell_base_max', market.get('suggested_sell_arrr_max')), 4)} · compra {_number(market.get('suggested_buy_base_max', market.get('suggested_buy_arrr_max')), 4)} {base_ticker}",
    )
    feed_running = bool(market.get("feed", {}).get("running"))
    _write(
        screen, 6, 0,
        f"Feed CEX {_mark(feed_running)} {'ATTIVO' if feed_running else 'FERMO'}",
        theme.success if feed_running else theme.warning,
    )

    _section(screen, 7, "PORTAFOGLIO KDF", theme)
    balances = wallet.get("balances", {})
    for offset, (ticker, item) in enumerate(tuple(balances.items())[:4]):
        available = bool(item.get("available"))
        balance = _number(item.get("balance")) if available else "non attiva"
        _write(
            screen, 8 + offset, 0,
            f"{_mark(available)} {ticker:<14} {balance:>18}",
            theme.success if available else theme.warning,
        )

    _section(screen, 12, "QUOTAZIONE", theme)
    quote_currency = str(
        market.get("quote_ticker")
        or market_id.removeprefix(f"{base_ticker}-")
    )
    _write(
        screen, 13, 0,
        f"{_human_side(dex_side, base_ticker)}  ·  {_route(dex_side, base_ticker, quote_currency)}",
        theme.selected,
    )
    _write(
        screen, 14, 0,
        f"Quantità {_number(requested_quantity)} {base_ticker}  ·  limite {_number(kdf_available_quantity)}  ·  premium {_percent(premium)}",
    )
    if preview:
        _write(
            screen, 15, 0,
            f"Prezzo proposto {_number(preview.get('human_price_quote_per_base', preview.get('human_price_quote_per_arrr')))} {preview.get('quote_currency', quote_currency)}/{base_ticker}  ·  VWAP {_number(preview.get('reference_vwap'))}",
            theme.success,
        )
    else:
        _write(screen, 15, 0, "Premi Invio per calcolare; non crea ordini.", theme.muted)
    pool_name = base_ticker if dex_side == "SELL_ARRR" else quote_currency
    pool = inventory.get("pools", {}).get(pool_name, {})
    _write(
        screen, 16, 0,
        f"Pool {pool_name}: disponibile {_number(pool.get('max_maker_volume'))} · bloccato {_number(pool.get('locked_by_swaps'))}",
    )
    _write(screen, 17, 0, f"Ordini gestiti aperti: {len(orders.get('orders', []))}")

    _section(screen, 18, "AUTOMAZIONE E SICUREZZA", theme)
    repricing_running = repricing.get("state") == "RUNNING"
    _write(
        screen, 19, 0,
        f"Repricing {_mark(repricing_running)} {repricing.get('state', 'SCONOSCIUTO')}  ·  soglia {_percent(repricing.get('min_price_change'))}  ·  minimo {repricing.get('min_update_interval_seconds', '-')} s",
        theme.success if repricing_running else theme.warning,
    )
    ready = bool(reconciliation.get("ready"))
    _write(
        screen, 20, 0,
        f"Controllo KDF {_mark(ready)} {'PRONTO' if ready else 'NON PRONTO'}  ·  swap {reconciliation.get('active_owned_swaps', '-')}  ·  conferme {reconciliation.get('terminal_swaps_to_acknowledge', '-')}  ·  problemi {reconciliation.get('problem_orders', '-')}",
        theme.success if ready else theme.warning,
    )


def _render_help(screen: Any, *, theme: TuiTheme, monitor_only: bool) -> None:
    height, width = screen.getmaxyx()
    if width < 52 or height < 16:
        _render_size_gate(screen, theme=theme, current=(width, height), help_mode=True)
        return
    _center(screen, 0, "KDF MARKET MAKER", theme.title)
    _write(screen, 1, 2, "GUIDA DEI COMANDI", theme.section)
    _write(screen, 2, 2, "=" * max(0, width - 5), theme.section)
    rows = [
        (4, "MENU", theme.section),
        (5, "[0] KDF e coin   [1] Portafoglio   [2] Quotazione   [3] Ordini", theme.menu),
        (6, "[4] Repricing    [5] Swap          [6] Eventi       [7] Guida", theme.menu),
        (7, "[8] CEX: saldi, rebalance e trasferimenti manuali", theme.menu),
        (8, "NAVIGAZIONE", theme.section),
        (9, "↑/↓ scegli voce o lato   ←/→ cambia mercato   Invio apre/anteprima", 0),
        (10, "Esc torna al menu   Tab apre Eventi   ? apre/chiude guida", 0),
        (12, "COMANDI NELLE PAGINE", theme.section),
        (13, "KDF: K avvia · X arresta · R riavvia · A attiva · D disattiva", 0),
        (14, "     V salva le coin attive · P attiva il profilo · S aggiorna task", 0),
        (15, "Quota: N quantità · I limite · P premium · Invio anteprima · O pubblica", 0),
        (16, "Ordini: M aggiorna · C cancella", 0),
        (17, "Repricing: E/D configura · T/F politica · G avvia · H pausa", 0),
        (18, "Swap: J rilegge KDF · Y registra la verifica manuale", 0),
        (20, "I comandi operativi funzionano solo nella pagina a cui appartengono.", theme.muted),
    ]
    if monitor_only:
        rows = [
            (4, "NAVIGAZIONE", theme.section),
            (5, "? chiudi aiuto   Ctrl+C uscita di emergenza", 0),
            (7, "Questa modalità mostra soltanto il journal Desktop.", theme.muted),
        ]
    for row, line, style in rows:
        if row < height - 2:
            _write(screen, row, 0, line, style)
    _write(
        screen,
        height - 1,
        0,
        "[? / Esc] chiudi guida   [Ctrl+C] uscita di emergenza",
        theme.warning,
    )


def _render_cex_picker(screen: Any, *, target: str, selected_index: int, theme: TuiTheme) -> None:
    height, width = screen.getmaxyx()
    if width < 52 or height < 14:
        _render_size_gate(screen, theme=theme, current=(width, height))
        return
    title = 'EVENTI E COPERTURE CEX' if target == 'MONITOR' else 'CEX: SALDI E RIEQUILIBRIO'
    _center(screen, 1, 'KDF MARKET MAKER', theme.title)
    _center(screen, 3, title, theme.section)
    _center(screen, 5, 'Scegli il CEX da consultare', theme.muted)
    for index, venue in enumerate(('MEXC', 'GATE')):
        pointer = '>' if index == selected_index else ' '
        _center(screen, 7 + index * 2, f'{pointer} [{venue}]',
                theme.selected if index == selected_index else theme.menu)
    _footer(screen, '[↑/↓ o ←/→] scegli   [Invio] apri', '[Esc] menu principale', theme)


def _render_size_gate(
    screen: Any,
    *,
    theme: TuiTheme,
    current: tuple[int, int],
    help_mode: bool = False,
    allow_quit: bool = False,
) -> None:
    height, _ = screen.getmaxyx()
    _write(screen, 0, 0, "KDF MARKET MAKER", theme.title)
    _write(screen, 2, 0, "Terminale troppo piccolo per questa schermata.", theme.warning)
    _write(screen, 3, 0, f"Dimensione attuale: {current[0]}×{current[1]}")
    _write(screen, 4, 0, "Allarga la finestra ad almeno 64×24.", theme.muted)
    footer = "[? / Esc] chiudi aiuto" if help_mode else "[?] aiuto"
    footer += "   [Q] esci" if allow_quit else "   [Ctrl+C] emergenza"
    _write(screen, max(0, height - 1), 0, footer, theme.selected)


def _draw_panel(
    screen: Any,
    y: int,
    x: int,
    height: int,
    width: int,
    title: str,
    theme: TuiTheme,
) -> None:
    if height < 3 or width < 8:
        return
    if theme.unicode:
        top_left, top_right, bottom_left, bottom_right, horizontal, vertical = (
            "┌", "┐", "└", "┘", "─", "│"
        )
    else:
        top_left = top_right = bottom_left = bottom_right = "+"
        horizontal, vertical = "-", "|"
    _write(screen, y, x, top_left + horizontal * (width - 2) + top_right, theme.section)
    _write(screen, y, x + 2, f" {title} ", theme.section)
    for row in range(y + 1, y + height - 1):
        _write(screen, row, x, vertical, theme.section)
        _write(screen, row, x + width - 1, vertical, theme.section)
    _write(
        screen,
        y + height - 1,
        x,
        bottom_left + horizontal * (width - 2) + bottom_right,
        theme.section,
    )


def _panel_write(
    screen: Any,
    panel_y: int,
    panel_x: int,
    panel_height: int,
    panel_width: int,
    content_row: int,
    text: str,
    style: int = 0,
) -> None:
    if content_row >= panel_height - 2:
        return
    _write(
        screen,
        panel_y + 1 + content_row,
        panel_x + 2,
        _fit_text(text, max(0, panel_width - 4)),
        style,
    )


def _section(screen: Any, row: int, title: str, theme: TuiTheme) -> None:
    _, width = screen.getmaxyx()
    separator = "─" if theme.unicode else "-"
    prefix = f" {title} "
    _write(screen, row, 0, prefix + separator * max(0, width - _cell_width(prefix) - 1), theme.section)


def _mark(ok: bool) -> str:
    return "[OK]" if ok else "[--]"


def _human_mode(value: Any) -> str:
    return {
        "SIMULATION": "SIMULAZIONE",
        "ORDERS_ENABLED": "ORDINI ABILITATI",
    }.get(str(value), str(value or "STATO SCONOSCIUTO"))


def _kdf_active(supervisor: dict[str, Any]) -> bool:
    return str(supervisor.get("state")) in {"RUNNING", "EXTERNAL"}


def _human_kdf_state(value: Any) -> str:
    return {
        "RUNNING": "ATTIVO",
        "EXTERNAL": "ATTIVO ESTERNO",
        "STOPPED": "FERMO",
        "STARTING": "IN AVVIO",
        "STOPPING": "IN ARRESTO",
    }.get(str(value), str(value or "SCONOSCIUTO"))


def _human_side(dex_side: str, base_ticker: str = "ARRR") -> str:
    return (
        f"VENDI {base_ticker}"
        if dex_side == "SELL_ARRR"
        else f"COMPRA {base_ticker}"
    )


def _route(dex_side: str, base_ticker: str, quote_currency: str) -> str:
    return (
        f"{base_ticker} → {quote_currency}"
        if dex_side == "SELL_ARRR"
        else f"{quote_currency} → {base_ticker}"
    )


def _number(value: Any, decimal_places: int = 8) -> str:
    if value in (None, "", "-"):
        return "—"
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not parsed.is_finite():
        return "—"
    rendered = f"{parsed:.{decimal_places}f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _percent(value: Any) -> str:
    if value in (None, "", "-"):
        return "—"
    try:
        parsed = Decimal(str(value)) * Decimal("100")
    except (InvalidOperation, ValueError):
        return str(value)
    rendered = f"{parsed:.3f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _age(value: Any) -> str:
    try:
        milliseconds = max(0, int(value))
    except (TypeError, ValueError):
        return "—"
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    return f"{milliseconds / 1000:.1f} s"


def _render_monitor(
    screen: Any,
    *,
    event_delivery: dict[str, Any],
    desktop: dict[str, Any],
    coverage: dict[str, Any] | None = None,
    cex: str = 'MEXC',
    connection_error: str | None,
    theme: TuiTheme,
) -> None:
    height, width = screen.getmaxyx()
    if width < 64 or height < 18:
        _render_size_gate(screen, theme=theme, current=(width, height))
        return
    headings = {
        "SICUREZZA ORDINI KDF",
        "CONSEGNA EVENTI",
        f"COPERTURE {cex}",
        "ALLARMI",
        "ULTIME COPERTURE",
    }
    if height >= 24:
        _center(screen, 0, "KDF MARKET MAKER", theme.title)
        _write(screen, 1, 2, f"MENU PRINCIPALE  >  EVENTI E COPERTURE CEX  >  {cex}", theme.section)
        _write(screen, 2, 2, "=" * max(0, width - 5), theme.section)
        content_row = 4
    else:
        _write(screen, 0, 0, "KDF MARKET MAKER  ·  EVENTI E COPERTURE", theme.title)
        content_row = 2
    for line in _monitor_rows(event_delivery, desktop, coverage, cex=cex)[2:]:
        if content_row >= height - 3:
            break
        if line in headings:
            style = theme.section
        elif line.startswith("[CRITICO]"):
            style = theme.error
        elif line.startswith("[AVVISO]") or "NON DISPONIBILE" in line:
            style = theme.warning
        elif line == "Nessun allarme":
            style = theme.success
        else:
            style = 0
        _write(screen, content_row, 0, line, style)
        content_row += 1
    if connection_error:
        _write(
            screen,
            max(0, height - 3),
            0,
            f"[ERRORE] Collegamento VPS: {connection_error}",
            theme.error,
        )
    _write(
        screen,
        max(0, height - 1),
        0,
        "[?] guida   [Ctrl+C] uscita di emergenza"
        if event_delivery.get("offline")
        else (
            "[N] termina forzatura   [Esc o Tab] menu   [?] guida"
            if (coverage or {}).get("override_active")
            else "[O] forza 5 min   [Esc o Tab] menu   [?] guida"
        ),
        theme.warning,
    )


def _monitor_rows(
    event_delivery: dict[str, Any],
    desktop: dict[str, Any],
    coverage: dict[str, Any] | None = None,
    *,
    cex: str = 'MEXC',
) -> list[str]:
    coverage = coverage or {"state": "DISABLED", "blocked": False}
    cex = str(cex).upper()
    required = _venue_balance_map(coverage.get('required_balances', {}), cex)
    free = _venue_balance_map(coverage.get('free_balances', {}), cex)
    selected_gaps = {asset: amount for asset, amount in required.items()
                     if Decimal(str(free.get(asset, '0'))) < Decimal(str(amount))}
    enabled = bool(event_delivery.get("enabled"))
    offline = bool(event_delivery.get("offline"))
    coverage_state = str(coverage.get("state", "SCONOSCIUTO"))
    if coverage_state == "OK" or (coverage_state == 'BLOCKED' and required and not selected_gaps):
        coverage_line = f"[OK] COPERTURA {cex} OK — ordini KDF autorizzati"
    elif coverage_state == "OVERRIDE":
        remaining = coverage.get("override_remaining_seconds")
        suffix = f" — scade tra {remaining}s" if remaining is not None else ""
        coverage_line = (
            "[CRITICO] FORZATURA ATTIVA — controllo fondi ignorato" + suffix
        )
    elif coverage_state == "BLOCKED":
        coverage_line = (
            "[CRITICO] MERCATO BLOCCATO — "
            + str(coverage.get("reason") or f"copertura {cex} non disponibile")
        )
    else:
        coverage_line = "[AVVISO] Controllo copertura non attivo in questa modalita"
    rows = [
        "KDF MARKET MAKER — EVENTI E COPERTURE",
        "",
        "SICUREZZA ORDINI KDF",
        coverage_line,
        (
            "Richiesti: "
            + _balance_summary(required)
            + f"  ·  Liberi {cex}: "
            + _balance_summary(free)
        ),
        "",
        "CONSEGNA EVENTI",
        (
            "VPS outbox: "
            f"{'NON COLLEGATA' if offline else ('ATTIVA' if enabled else 'DISATTIVA')}  "
            f"totali {event_delivery.get('total', 0)}  "
            f"confermati {event_delivery.get('acknowledged', 0)}  "
            f"in attesa {event_delivery.get('unacknowledged', 0)}"
        ),
    ]
    if not desktop.get("available"):
        rows.extend(
            [
                f"Desktop journal: NON DISPONIBILE — {desktop.get('reason', '-')}",
                "",
                (
                    "[Ctrl+C] uscita di emergenza"
                    if offline
                    else "[Esc] torna al menu principale"
                ),
            ]
        )
        return rows

    delivery = desktop.get("delivery", {})
    venue_status = desktop.get('venues', {}).get(cex, {})
    hedges = venue_status or (desktop.get("hedges", {}) if cex == 'MEXC' else {})
    rows.extend(
        [
            (
                "Desktop: "
                f"ricevuti {delivery.get('total', 0)}  "
                f"confermati {delivery.get('acknowledged', 0)}  "
                f"ACK pendenti {delivery.get('pending_acknowledgement', 0)}  "
                f"cursor {delivery.get('cursor', 0)}"
            ),
            "",
            f"COPERTURE {cex}",
            (
                f"Totali {hedges.get('total', 0)}  "
                f"da validare {hedges.get('pending_validation', 0)}  "
                f"in corso {hedges.get('in_progress', 0)}  "
                f"validate {hedges.get('validated', 0)}  "
                f"attenzione {hedges.get('attention', 0)}"
            ),
            "",
            "ALLARMI",
        ]
    )
    if event_delivery.get("unacknowledged", 0):
        rows.append(
            f"[AVVISO] {event_delivery['unacknowledged']} eventi VPS non confermati"
        )
    if not enabled and not offline:
        rows.append("[AVVISO] outbox eventi VPS non disponibile")
    selected_swaps = {str(item.get('swap_uuid')) for item in desktop.get('recent', [])
                      if str(item.get('cex', 'MEXC')).upper() == cex}
    alarms = [alarm for alarm in desktop.get("alarms", [])
              if not alarm.get('swap_uuid') or str(alarm.get('swap_uuid')) in selected_swaps]
    if (
        not alarms
        and not event_delivery.get("unacknowledged", 0)
        and (enabled or offline)
    ):
        rows.append("Nessun allarme")
    for alarm in alarms[:5]:
        severity = "CRITICO" if alarm.get("severity") == "CRITICAL" else "AVVISO"
        swap = (
            f" {_short(str(alarm.get('swap_uuid')), 20)}"
            if alarm.get("swap_uuid")
            else ""
        )
        rows.append(
            f"[{severity}]{swap} {_short(str(alarm.get('message', '-')), 110)}"
        )
    rows.extend(["", "ULTIME COPERTURE"])
    recent = venue_status.get('recent', [item for item in desktop.get("recent", [])
                                         if str(item.get('cex', 'MEXC')).upper() == cex])
    if not recent:
        rows.append("Nessuna copertura registrata")
    for item in recent:
        ack = "ACK" if item.get("acknowledged") else "ACK PENDENTE"
        rows.append(
            f"{_short(str(item.get('swap_uuid', '-')), 24):24}  "
            f"{str(item.get('state', '-')):16}  "
            f"{item.get('hedge_side', '-')} {item.get('target_quantity', '-')} "
            f"{item.get('base_ticker', 'BASE')}  "
            f"{item.get('market_id', '-')}  {ack}  "
            f"tentativo {item.get('attempt_status', '-')}"
        )
        for leg in item.get("basket_legs", []):
            rows.append(f"  {leg['symbol']} {leg['side']}: {leg['state']}  eseguito {leg.get('executed_quantity') or '0'}/{leg['quantity']}  residuo precisione {leg['dust']}")
    return rows


def _venue_balance_map(payload: Any, cex: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    prefix = f'{cex}:'
    if cex == 'MEXC':
        return {asset: amount for asset, amount in payload.items() if ':' not in str(asset)}
    return {str(asset)[len(prefix):]: amount for asset, amount in payload.items()
            if str(asset).upper().startswith(prefix)}


def _balance_summary(payload: Any) -> str:
    if not isinstance(payload, dict) or not payload:
        return "nessuno"
    return ", ".join(
        f"{asset} {_number(amount)}" for asset, amount in sorted(payload.items())
    )


def _write(screen: Any, row: int, column: int, text: str, style: int = 0) -> None:
    height, width = screen.getmaxyx()
    if row < 0 or row >= height or column >= width:
        return
    fitted = _fit_text(_safe_text(str(text)), max(0, width - column - 1))
    try:
        screen.addstr(row, column, fitted, style)
    except curses.error:
        pass


def _safe_text(value: str) -> str:
    return "".join(
        " " if character in "\r\n\t" else "?"
        if unicodedata.category(character) in {"Cc", "Cs"}
        else character
        for character in value
    )


def _fit_text(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _cell_width(value) <= width:
        return value
    ellipsis = "…" if "utf" in (sys.stdout.encoding or "").lower() else "..."
    target = max(0, width - _cell_width(ellipsis))
    result: list[str] = []
    used = 0
    for character in value:
        character_width = _character_width(character)
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    return "".join(result) + ellipsis


def _cell_width(value: str) -> int:
    return sum(_character_width(character) for character in value)


def _character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _task_id(result: Any) -> int:
    if not isinstance(result, dict) or "task_id" not in result:
        raise RuntimeError("KDF non ha restituito un task_id")
    return int(result["task_id"])


def _remember_activation_tasks(
    tasks: dict[str, int],
    result: Any,
    *,
    fallback_ticker: str | None = None,
) -> None:
    if not isinstance(result, dict):
        return
    for item in result.get("pending_tasks", ()):
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        task_id = item.get("task_id")
        if ticker and task_id is not None:
            tasks[ticker] = int(task_id)
    # Compatibility with an Agent process started before the batch response
    # format was introduced.  The user can safely restart it later.
    if result.get("task_id") is not None and fallback_ticker:
        tasks[fallback_ticker.strip().upper()] = int(result["task_id"])


def _activation_batch_message(
    result: Any, *, fallback_ticker: str | None = None
) -> str:
    if not isinstance(result, dict):
        return "Risposta di attivazione non valida"
    pending = result.get("pending_tasks", ())
    if pending:
        labels = ", ".join(
            f"{item.get('ticker')} #{item.get('task_id')}"
            for item in pending
            if isinstance(item, dict)
        )
        return f"Attivazione avviata: {labels}"
    if result.get("task_id") is not None and fallback_ticker:
        return (
            f"Attivazione avviata: {fallback_ticker.strip().upper()} "
            f"#{result['task_id']}"
        )
    activated = [
        str(ticker)
        for item in result.get("activations", ())
        if isinstance(item, dict)
        for ticker in item.get("tickers", ())
    ]
    if activated:
        return f"Coin attivate: {', '.join(dict.fromkeys(activated))}"
    already = ", ".join(map(str, result.get("already_enabled", ())))
    return f"Coin già attive: {already}" if already else "Nessuna coin da attivare"


def _first_order_for_quote(
    orders: dict[str, Any], market_id: str, dex_side: str
) -> dict[str, Any] | None:
    return next(
        (
            order
            for order in orders.get("orders", [])
            if order.get("dex_side") == dex_side
            and order.get("market_id") == market_id
        ),
        None,
    )


def _short_json(value: Any, *, limit: int = 100) -> str:
    rendered = json.dumps(value, separators=(",", ":"), default=str)
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


def _quote_payload(
    dex_side: str,
    market_id: str,
    requested_quantity: Decimal,
    kdf_available_quantity: Decimal,
    premium: Decimal,
) -> dict[str, str]:
    return {
        "market_id": market_id,
        "dex_side": dex_side,
        "requested_quantity": str(requested_quantity),
        "kdf_available_quantity": str(kdf_available_quantity),
        "premium": str(premium),
    }


def _prompt_decimal(screen: Any, label: str, current: Decimal) -> Decimal | None:
    height, _ = screen.getmaxyx()
    row = max(0, height - 1)
    screen.nodelay(False)
    screen.timeout(-1)
    curses.echo()
    try:
        curses.curs_set(1)
        screen.move(row, 0)
        screen.clrtoeol()
        prompt = f"{label} [{current}]: "
        _write(screen, row, 0, prompt)
        screen.refresh()
        raw = screen.getstr(row, len(prompt), 40).decode("utf-8").strip()
        if not raw:
            return current
        value = Decimal(raw)
        return value if value.is_finite() else None
    except (curses.error, UnicodeDecodeError, InvalidOperation):
        return None
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        screen.timeout(250)


def _prompt_text(screen: Any, label: str, *, limit: int = 64) -> str | None:
    height, width = screen.getmaxyx()
    row = max(0, height - 1)
    screen.nodelay(False)
    screen.timeout(-1)
    curses.echo()
    try:
        curses.curs_set(1)
        screen.move(row, 0)
        screen.clrtoeol()
        prompt = f"{label}: "
        _write(screen, row, 0, prompt)
        screen.refresh()
        available = max(1, min(limit, width - len(prompt) - 1))
        raw = screen.getstr(row, len(prompt), available).decode("utf-8").strip()
        return raw or None
    except (curses.error, UnicodeDecodeError):
        return None
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        screen.timeout(250)


def _confirm(screen: Any, prompt: str) -> bool:
    height, _ = screen.getmaxyx()
    row = max(0, height - 1)
    screen.nodelay(False)
    screen.timeout(-1)
    try:
        screen.move(row, 0)
        screen.clrtoeol()
        _write(screen, row, 0, prompt, curses.A_BOLD)
        screen.refresh()
        return screen.getch() in (ord("s"), ord("S"), ord("y"), ord("Y"))
    finally:
        screen.nodelay(True)
        screen.timeout(250)


def _short(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit < 8:
        return value[:limit]
    return value[: limit - 5] + "..." + value[-2:]
