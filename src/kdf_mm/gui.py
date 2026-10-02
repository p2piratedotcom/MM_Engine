from __future__ import annotations

import sys
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any, Callable

from .desktop_dashboard import DesktopDashboardSource, ReadOnlyAgentApi
from .mexc_status import KeyringMexcStatus
from .portfolio import PortfolioPolicy


class DesktopGuiUnavailable(RuntimeError):
    pass


def load_qt(importer: Callable[[str], Any] = import_module) -> tuple[Any, Any, Any]:
    try:
        return (
            importer("PySide6.QtCore"),
            importer("PySide6.QtGui"),
            importer("PySide6.QtWidgets"),
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise DesktopGuiUnavailable(
            "PySide6 non installato. Esegui: python -m pip install -e '.[desktop]'"
        ) from exc


def run_gui(
    *,
    agent_url: str,
    token: str,
    desktop_journal: str | None,
    offline: bool = False,
    refresh_interval: float = 3.0,
    mexc_enabled: bool = True,
    mexc_profile: str = "default",
    mexc_base_url: str = "https://api.mexc.com",
    mexc_symbol: str = "ARRRUSDT",
    mexc_base_asset: str = "ARRR",
    mexc_quote_asset: str = "USDT",
    base_ticker: str | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
) -> int:
    if refresh_interval < 0.5:
        raise ValueError("--refresh-interval deve essere almeno 0.5 secondi")
    if not offline and not token:
        raise ValueError("KDF_MM_AGENT_TOKEN is required")
    if offline and desktop_journal is None:
        raise ValueError("--offline richiede un journal Desktop")

    QtCore, QtGui, QtWidgets = load_qt()
    api = None if offline else ReadOnlyAgentApi(base_url=agent_url, token=token)
    mexc = (
        KeyringMexcStatus(
            profile=mexc_profile,
            base_url=mexc_base_url,
            symbol=mexc_symbol,
            base_asset=mexc_base_asset,
            quote_asset=mexc_quote_asset,
        )
        if mexc_enabled
        else None
    )
    source = DesktopDashboardSource(
        api=api,
        desktop_journal=desktop_journal,
        mexc=mexc,
        portfolio_policy=portfolio_policy,
    )
    classes = _build_classes(QtCore, QtGui, QtWidgets)
    application = QtWidgets.QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QtWidgets.QApplication(sys.argv)
    application.setApplicationName("KDF Market Maker Monitor")
    window = classes["MainWindow"](
        source=source,
        refresh_interval=refresh_interval,
        initial_base_ticker=base_ticker or mexc_base_asset,
        initial_quote_asset=mexc_quote_asset,
        initial_symbol=mexc_symbol,
    )
    window.show()
    if owns_application:
        return int(application.exec())
    return 0


def _build_classes(QtCore: Any, QtGui: Any, QtWidgets: Any) -> dict[str, Any]:
    Signal = QtCore.Signal
    Slot = QtCore.Slot

    class SnapshotWorker(QtCore.QThread):
        snapshot_ready = Signal(dict)

        def __init__(self, source: DesktopDashboardSource, interval: float) -> None:
            super().__init__()
            self.source = source
            self.interval = interval
            self.wakeup = threading.Event()

        def refresh_now(self) -> None:
            self.wakeup.set()

        def run(self) -> None:
            while not self.isInterruptionRequested():
                try:
                    payload = self.source.snapshot()
                except Exception as exc:
                    payload = {
                        "collected_at_ms": int(datetime.now().timestamp() * 1000),
                        "offline": self.source.api is None,
                        "connected": False,
                        "remote_errors": {"dashboard": str(exc)[:300]},
                        "desktop": {
                            "available": False,
                            "reason": "lettura monitor non riuscita",
                            "delivery": {},
                            "hedges": {},
                            "alarms": [],
                            "recent": [],
                        },
                    }
                self.snapshot_ready.emit(payload)
                self.wakeup.wait(self.interval)
                self.wakeup.clear()

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(
            self,
            *,
            source: DesktopDashboardSource,
            refresh_interval: float,
            initial_base_ticker: str = "ARRR",
            initial_quote_asset: str = "USDT",
            initial_symbol: str = "ARRRUSDT",
        ):
            super().__init__()
            self.setWindowTitle("KDF Market Maker — Monitor locale")
            self.resize(1120, 760)
            self._labels: dict[str, Any] = {}
            self._close_pending = False
            self._base_ticker = initial_base_ticker.strip().upper() or "BASE"
            self._quote_asset = initial_quote_asset.strip().upper() or "QUOTE"
            self._mexc_symbol = initial_symbol.strip().upper() or "SIMBOLO"
            self._build_ui()
            self.worker = SnapshotWorker(source, refresh_interval)
            self.worker.snapshot_ready.connect(self.apply_snapshot)
            self.worker.finished.connect(self._complete_deferred_close)
            self.refresh_button.clicked.connect(self.worker.refresh_now)
            self.worker.start()

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            root = QtWidgets.QVBoxLayout(central)
            root.setContentsMargins(18, 16, 18, 14)
            root.setSpacing(10)

            banner = QtWidgets.QLabel(
                "SOLA LETTURA  •  nessun ordine, acquisto o trasferimento può essere eseguito"
            )
            banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            banner.setStyleSheet(
                "background:#16324f;color:#ffffff;padding:10px;border-radius:6px;"
                "font-weight:700;"
            )
            root.addWidget(banner)

            header = QtWidgets.QHBoxLayout()
            self.connection_label = QtWidgets.QLabel("Avvio monitor…")
            self.connection_label.setStyleSheet("font-size:16px;font-weight:600;")
            self.updated_label = QtWidgets.QLabel("Ultimo aggiornamento: —")
            header.addWidget(self.connection_label)
            header.addStretch()
            header.addWidget(self.updated_label)
            root.addLayout(header)

            tabs = QtWidgets.QTabWidget()
            tabs.addTab(self._overview_tab(), "Panoramica")
            tabs.addTab(self._exposure_tab(), "Esposizione")
            tabs.addTab(self._ledger_tab(), "Risultati")
            tabs.addTab(self._markets_tab(), "Mercati e ordini")
            tabs.addTab(self._events_tab(), "Eventi e coperture")
            tabs.addTab(self._wallet_tab(), "Portafoglio KDF")
            tabs.addTab(self._mexc_tab(), "MEXC")
            root.addWidget(tabs, 1)

            actions = QtWidgets.QHBoxLayout()
            hint = QtWidgets.QLabel(
                "Le chiavi MEXC restano nel portachiavi; ordini e trasferimenti sono bloccati."
            )
            hint.setStyleSheet("color:#667085;")
            self.refresh_button = QtWidgets.QPushButton("Aggiorna ora")
            close_button = QtWidgets.QPushButton("Chiudi")
            close_button.clicked.connect(self.close)
            actions.addWidget(hint)
            actions.addStretch()
            actions.addWidget(self.refresh_button)
            actions.addWidget(close_button)
            root.addLayout(actions)
            self.setCentralWidget(central)

        def _overview_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            cards = QtWidgets.QGridLayout()
            cards.addWidget(self._card("KDF", (
                ("kdf_reachable", "RPC"),
                ("supervisor_state", "Supervisor"),
                ("service_mode", "Modalità ordini"),
            )), 0, 0)
            cards.addWidget(self._card("Quotazione", (
                ("repricing_state", "Repricing"),
                ("reconciliation_ready", "Riconciliazione"),
                ("owned_orders", "Ordini aperti"),
            )), 0, 1)
            cards.addWidget(self._card("Consegna VPS", (
                ("outbox_enabled", "Outbox"),
                ("outbox_unacked", "Da confermare"),
                ("active_swaps", "Swap attivi"),
            )), 1, 0)
            cards.addWidget(self._card("Coperture Desktop", (
                ("hedge_total", "Totali"),
                ("hedge_validated", "Convalidate"),
                ("hedge_attention", "Da controllare"),
            )), 1, 1)
            cards.addWidget(self._card("Sicurezza ordini KDF", (
                ("coverage_state", "Copertura"),
                ("coverage_required", "Fondi richiesti"),
                ("coverage_free", "Fondi liberi MEXC"),
            )), 2, 0, 1, 2)
            cards.addWidget(self._card("MEXC privato — sola lettura", (
                ("mexc_state", "Connessione"),
                ("mexc_usdt", f"{self._quote_asset} disponibile"),
                ("mexc_arrr", f"{self._base_ticker} disponibile"),
                ("mexc_orders", f"Ordini {self._mexc_symbol} aperti"),
            )), 3, 0, 1, 2)
            layout.addLayout(cards)
            alarms_group = QtWidgets.QGroupBox("Avvisi operativi")
            alarms_layout = QtWidgets.QVBoxLayout(alarms_group)
            self.overview_alarms = QtWidgets.QListWidget()
            self.overview_alarms.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.NoSelection
            )
            alarms_layout.addWidget(self.overview_alarms)
            layout.addWidget(alarms_group, 1)
            return tab

        def _exposure_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            banner = QtWidgets.QLabel(
                "SUGGERIMENTI SOLA LETTURA  •  nessun trasferimento può essere eseguito"
            )
            banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            banner.setStyleSheet(
                "background:#fffaeb;color:#8a5b00;padding:8px;border:1px solid #fedf89;"
                "border-radius:5px;font-weight:700;"
            )
            layout.addWidget(banner)
            self.portfolio_summary = QtWidgets.QLabel(
                "Esposizione: in attesa dei dati KDF e MEXC…"
            )
            self.portfolio_summary.setWordWrap(True)
            layout.addWidget(self.portfolio_summary)

            cards = QtWidgets.QGridLayout()
            cards.addWidget(self._card("Portafoglio complessivo", (
                ("portfolio_value", "Valore stimato"),
                ("portfolio_arrr", f"{self._base_ticker} totali"),
                ("portfolio_usdt", f"{self._quote_asset} totali"),
                ("portfolio_arrr_share", f"Quota valore {self._base_ticker}"),
            )), 0, 0)
            cards.addWidget(self._card("Distribuzione per sede", (
                ("portfolio_kdf_value", "Valore KDF"),
                ("portfolio_mexc_value", "Valore MEXC"),
                ("portfolio_kdf_share", "Quota valore su KDF"),
                ("portfolio_price", f"Prezzo {self._base_ticker}/{self._quote_asset}"),
            )), 0, 1)
            cards.addWidget(self._card("Capacità indicativa", (
                ("portfolio_sell_capacity", f"Vendita {self._base_ticker} su KDF"),
                ("portfolio_buy_capacity", f"Acquisto {self._base_ticker} su KDF"),
                ("portfolio_target_arrr", f"Obiettivo {self._base_ticker} su KDF"),
                ("portfolio_target_usdt", f"Obiettivo {self._quote_asset} su KDF"),
            )), 0, 2)
            layout.addLayout(cards)

            self.exposure_table = self._table(
                ("Asset", "KDF", "MEXC Spot", "Totale", "Valore stimato USDT")
            )
            layout.addWidget(self.exposure_table)
            layout.addWidget(QtWidgets.QLabel("Suggerimenti di riequilibrio"))
            self.rebalance_table = self._table(
                ("Asset", "Direzione", "Quantità", "Valore USDT", "Motivo")
            )
            layout.addWidget(self.rebalance_table, 1)
            self.rebalance_notice = QtWidgets.QLabel("—")
            self.rebalance_notice.setWordWrap(True)
            self.rebalance_notice.setStyleSheet("color:#667085;")
            layout.addWidget(self.rebalance_notice)
            self.pnl_notice = QtWidgets.QLabel(
                "P/L non disponibile: manca ancora il costo storico completo."
            )
            self.pnl_notice.setStyleSheet("color:#667085;")
            layout.addWidget(self.pnl_notice)
            return tab

        def _ledger_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            banner = QtWidgets.QLabel(
                "LEDGER SOLA LETTURA  •  nessun profitto viene stimato senza dati completi"
            )
            banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            banner.setStyleSheet(
                "background:#eef4ff;color:#1849a9;padding:8px;border:1px solid #b2ccff;"
                "border-radius:5px;font-weight:700;"
            )
            layout.addWidget(banner)

            cards = QtWidgets.QGridLayout()
            cards.addWidget(self._card("P/L dei cicli", (
                ("ledger_realized", "Realizzato netto"),
                ("ledger_unrealized", "Non realizzato"),
                ("ledger_total_pnl", "Totale stimato"),
                ("ledger_fees", "Commissioni note"),
            )), 0, 0)
            cards.addWidget(self._card("Stato del ledger", (
                ("ledger_cycles", "Cicli registrati"),
                ("ledger_settled", "Esiti KDF ricevuti"),
                ("ledger_pending", "In attesa esito KDF"),
                ("ledger_open", "Esposizioni residue"),
                ("ledger_fills", "Fill MEXC verificati"),
                ("ledger_fills_missing", "Cicli senza fill verificati"),
            )), 0, 1)
            self.inventory_group = self._card("Inventario asset base", (
                ("inventory_quantity", "Quantita calcolata"),
                ("inventory_total_cost", "Costo totale residuo"),
                ("inventory_average_cost", "Costo medio"),
                ("inventory_market_value", "Valore corrente"),
                ("inventory_unrealized", "P/L non realizzato"),
                ("inventory_swaps", "Swap applicati"),
                ("inventory_adjustments", "Movimenti esterni"),
            ))
            cards.addWidget(self.inventory_group, 0, 2)
            layout.addLayout(cards)

            self.ledger_table = self._table((
                "Swap", "Mercato", "Lato KDF", "Esito KDF", "Asset base",
                "Prezzo medio MEXC", "Residuo base", "P/L USDT",
            ))
            layout.addWidget(self.ledger_table, 1)
            self.ledger_notice = QtWidgets.QLabel("Ledger: in attesa dei dati…")
            self.ledger_notice.setWordWrap(True)
            layout.addWidget(self.ledger_notice)
            self.inventory_pnl_notice = QtWidgets.QLabel(
                "P/L inventario non disponibile: costo iniziale non registrato."
            )
            self.inventory_pnl_notice.setWordWrap(True)
            self.inventory_pnl_notice.setStyleSheet("color:#667085;")
            layout.addWidget(self.inventory_pnl_notice)
            return tab

        def _card(self, title: str, fields: tuple[tuple[str, str], ...]) -> Any:
            group = QtWidgets.QGroupBox(title)
            layout = QtWidgets.QFormLayout(group)
            for key, label in fields:
                value = QtWidgets.QLabel("—")
                caption = QtWidgets.QLabel(label)
                value.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                )
                self._labels[key] = value
                self._labels[f"{key}__caption"] = caption
                layout.addRow(caption, value)
            return group

        def _markets_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            layout.addWidget(QtWidgets.QLabel("Mercati pubblici e freschezza del feed"))
            self.markets_table = self._table(
                ("Mercato", "Stato", "Bid", "Ask", "Età ms", "Tetto sell", "Tetto buy")
            )
            layout.addWidget(self.markets_table, 1)
            layout.addWidget(QtWidgets.QLabel("Ordini KDF posseduti dal servizio"))
            self.orders_table = self._table(
                ("Mercato", "Lato", "Prezzo", "Volume", "Stato", "UUID")
            )
            layout.addWidget(self.orders_table, 1)
            return tab

        def _events_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            self.events_summary = QtWidgets.QLabel("Journal Desktop: —")
            layout.addWidget(self.events_summary)
            self.events_table = self._table(
                ("Swap", "Mercato", "Lato", "Quantità", "Stato", "ACK", "Prezzo limite")
            )
            layout.addWidget(self.events_table, 1)
            return tab

        def _wallet_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            info = QtWidgets.QLabel(
                "Saldo e indirizzo pubblico restituiti da KDF. Nessun comando di prelievo è disponibile."
            )
            info.setWordWrap(True)
            layout.addWidget(info)
            self.wallet_table = self._table(("Coin", "Saldo", "Indirizzo", "Stato"))
            layout.addWidget(self.wallet_table, 1)
            return tab

        def _mexc_tab(self) -> Any:
            tab = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(tab)
            safety = QtWidgets.QLabel(
                "SOLA LETTURA  •  ordini reali bloccati  •  trasferimenti bloccati"
            )
            safety.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            safety.setStyleSheet(
                "background:#ecfdf3;color:#18794e;padding:8px;border:1px solid #abefc6;"
                "border-radius:5px;font-weight:700;"
            )
            layout.addWidget(safety)
            self.mexc_summary = QtWidgets.QLabel("MEXC: in attesa della prima lettura…")
            self.mexc_summary.setWordWrap(True)
            layout.addWidget(self.mexc_summary)

            details = QtWidgets.QGroupBox("Conto e accesso API")
            form = QtWidgets.QFormLayout(details)
            for key, label in (
                ("mexc_account_type", "Tipo conto"),
                ("mexc_can_trade", "Conto abilitato al trading"),
                ("mexc_can_deposit", "Conto abilitato ai depositi"),
                ("mexc_can_withdraw", "Conto abilitato ai prelievi"),
                ("mexc_symbol_allowed", f"Chiave abilitata per {self._mexc_symbol}"),
                ("mexc_permissions", "Categorie conto dichiarate"),
                ("mexc_time", "Sincronizzazione orario"),
            ):
                value = QtWidgets.QLabel("—")
                caption = QtWidgets.QLabel(label)
                value.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                )
                self._labels[key] = value
                self._labels[f"{key}__caption"] = caption
                form.addRow(caption, value)
            layout.addWidget(details)
            account_note = QtWidgets.QLabel(
                "Nota: le capacità del conto provengono da /api/v3/account e non "
                "certificano i permessi specifici della chiave API. I trasferimenti "
                "restano comunque bloccati localmente."
            )
            account_note.setWordWrap(True)
            account_note.setStyleSheet("color:#667085;")
            layout.addWidget(account_note)

            layout.addWidget(QtWidgets.QLabel("Saldi Spot usati dal market maker"))
            self.mexc_balances_table = self._table(
                ("Asset", "Disponibile", "Bloccato", "Totale")
            )
            layout.addWidget(self.mexc_balances_table)
            self.mexc_orders_title = QtWidgets.QLabel(
                f"Ordini MEXC aperti su {self._mexc_symbol}"
            )
            layout.addWidget(self.mexc_orders_title)
            self.mexc_orders_table = self._table(
                ("Lato", "Tipo", "Prezzo", "Quantità", "Eseguita", "Stato", "Client ID")
            )
            layout.addWidget(self.mexc_orders_table, 1)
            return tab

        def _table(self, headers: tuple[str, ...]) -> Any:
            table = QtWidgets.QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            )
            table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            table.setAlternatingRowColors(True)
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().setVisible(False)
            return table

        @Slot(dict)
        def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
            offline = bool(snapshot.get("offline"))
            connected = bool(snapshot.get("connected"))
            errors = snapshot.get("remote_errors", {})
            if offline:
                connection = (
                    "Senza VPS — journal Desktop e monitor MEXC"
                    if snapshot.get("mexc", {}).get("configured")
                    else "Senza VPS — solo journal Desktop"
                )
                color = "#8a5b00"
            elif connected and not errors:
                connection = "VPS/KDF collegato"
                color = "#18794e"
            elif connected:
                connection = f"VPS collegato con {len(errors)} letture non disponibili"
                color = "#8a5b00"
            else:
                connection = "VPS/KDF non raggiungibile"
                color = "#b42318"
            self.connection_label.setText(connection)
            self.connection_label.setStyleSheet(
                f"font-size:16px;font-weight:600;color:{color};"
            )
            timestamp = snapshot.get("collected_at_ms")
            if timestamp:
                formatted = datetime.fromtimestamp(int(timestamp) / 1000).strftime(
                    "%d/%m/%Y %H:%M:%S"
                )
                self.updated_label.setText(f"Ultimo aggiornamento: {formatted}")

            service = snapshot.get("service", {})
            kdf = snapshot.get("kdf", {})
            supervisor = snapshot.get("supervisor", {})
            repricing = snapshot.get("repricing", {})
            reconciliation = snapshot.get("reconciliation", {})
            delivery = snapshot.get("event_delivery", {})
            desktop = snapshot.get("desktop", {})
            mexc = snapshot.get("mexc", {})
            coverage = snapshot.get("coverage", {})
            hedges = desktop.get("hedges", {})
            orders = snapshot.get("orders", {}).get("orders", [])
            mexc_balances = mexc.get("balances", {})
            base_ticker = str(service.get("base_ticker") or "ARRR")
            base_asset = str(
                mexc.get("base_asset") or base_ticker.split("-", 1)[0]
            )
            quote_asset = str(mexc.get("quote_asset") or "USDT")
            symbol = str(
                mexc.get("symbol") or service.get("hedge_symbol") or "ARRRUSDT"
            )
            self._base_ticker = base_ticker
            self._quote_asset = quote_asset
            self._mexc_symbol = symbol

            values = {
                "kdf_reachable": "Raggiungibile" if kdf.get("reachable") else "Non raggiungibile",
                "supervisor_state": supervisor.get("state", "—"),
                "service_mode": service.get("mode", "—"),
                "repricing_state": repricing.get("state", "—"),
                "reconciliation_ready": "Pronta" if reconciliation.get("ready") else "Non pronta",
                "owned_orders": len(orders),
                "outbox_enabled": "Attiva" if delivery.get("enabled") else "Non attiva",
                "outbox_unacked": delivery.get("unacknowledged", "—"),
                "active_swaps": reconciliation.get("active_owned_swaps", "—"),
                "hedge_total": hedges.get("total", "—"),
                "hedge_validated": hedges.get("validated", "—"),
                "hedge_attention": hedges.get("attention", "—"),
                "coverage_state": _coverage_label(coverage),
                "coverage_required": _balance_text(
                    coverage.get("required_balances")
                ),
                "coverage_free": _balance_text(coverage.get("free_balances")),
                "mexc_state": (
                    "Collegato" if mexc.get("available") else
                    "Non disponibile" if mexc.get("configured") else "Disattivato"
                ),
                "mexc_usdt": mexc_balances.get(quote_asset, {}).get("free", "—"),
                "mexc_arrr": mexc_balances.get(base_asset, {}).get("free", "—"),
                "mexc_orders": len(mexc.get("open_orders", [])),
            }
            for key, value in values.items():
                self._labels[key].setText(str(value))
            self._set_caption("mexc_usdt", f"{quote_asset} disponibile")
            self._set_caption("mexc_arrr", f"{base_asset} disponibile")
            self._set_caption("mexc_orders", f"Ordini {symbol} aperti")
            self._set_caption(
                "mexc_symbol_allowed", f"Chiave abilitata per {symbol}"
            )
            self.mexc_orders_title.setText(f"Ordini MEXC aperti su {symbol}")

            self._fill_markets(snapshot.get("markets", {}).get("markets", {}))
            self._fill_orders(orders)
            self._fill_events(desktop)
            self._fill_wallet(snapshot.get("wallet", {}).get("balances", {}))
            self._fill_mexc(mexc)
            self._fill_portfolio(snapshot.get("portfolio", {}))
            self._fill_ledger(snapshot.get("ledger", {}))
            self._fill_alarms(snapshot)

        def _fill_markets(self, markets: Any) -> None:
            rows = []
            if isinstance(markets, dict):
                for market_id, item in markets.items():
                    item = item if isinstance(item, dict) else {}
                    rows.append((
                        market_id,
                        item.get("state", "—"),
                        item.get("best_bid", "—"),
                        item.get("best_ask", "—"),
                        item.get("age_ms", "—"),
                        item.get(
                            "suggested_sell_base_max",
                            item.get("suggested_sell_arrr_max", "—"),
                        ),
                        item.get(
                            "suggested_buy_base_max",
                            item.get("suggested_buy_arrr_max", "—"),
                        ),
                    ))
            self._set_rows(self.markets_table, rows)

        def _fill_orders(self, orders: Any) -> None:
            rows = []
            for item in orders if isinstance(orders, list) else []:
                rows.append((
                    item.get("market_id", "—"),
                    item.get("dex_side", "—"),
                    item.get("kdf_price", "—"),
                    item.get("kdf_volume", "—"),
                    item.get("status", "—"),
                    item.get("order_uuid", "—"),
                ))
            self._set_rows(self.orders_table, rows)

        def _fill_events(self, desktop: dict[str, Any]) -> None:
            delivery = desktop.get("delivery", {})
            reason = desktop.get("reason")
            if desktop.get("available"):
                self.events_summary.setText(
                    "Eventi ricevuti: {total}  •  confermati: {ack}  •  ACK pendenti: {pending}".format(
                        total=delivery.get("total", 0),
                        ack=delivery.get("acknowledged", 0),
                        pending=delivery.get("pending_acknowledgement", 0),
                    )
                )
            else:
                self.events_summary.setText(f"Journal Desktop non disponibile: {reason or '—'}")
            rows = []
            for item in desktop.get("recent", []):
                rows.append((
                    item.get("swap_uuid", "—"),
                    item.get("market_id", "—"),
                    item.get("hedge_side", "—"),
                    item.get("target_quantity", "—"),
                    item.get("state", "—"),
                    "Sì" if item.get("acknowledged") else "No",
                    item.get("limit_price") or "—",
                ))
            self._set_rows(self.events_table, rows)

        def _fill_wallet(self, balances: Any) -> None:
            rows = []
            if isinstance(balances, dict):
                preferred = tuple(
                    ticker
                    for ticker in (getattr(self, "_base_ticker", "ARRR"),)
                    if ticker in balances
                )
                tickers = list(preferred) + sorted(set(balances) - set(preferred))
                for ticker in tickers:
                    item = balances.get(ticker)
                    if not isinstance(item, dict):
                        continue
                    rows.append((
                        ticker,
                        item.get("balance", "—"),
                        item.get("address", "—"),
                        "Attiva" if item.get("available") else "Non attiva",
                    ))
            self._set_rows(self.wallet_table, rows)

        def _fill_mexc(self, mexc: dict[str, Any]) -> None:
            account = mexc.get("account", {})
            errors = mexc.get("errors", {})
            if mexc.get("available"):
                self.mexc_summary.setText(
                    "Conto MEXC letto correttamente. I valori sono informativi e non "
                    "abilitano alcuna operazione."
                )
            elif mexc.get("configured"):
                detail = "; ".join(str(value) for value in errors.values())
                self.mexc_summary.setText(
                    f"Dati MEXC non disponibili: {detail or mexc.get('reason') or '—'}"
                )
            else:
                self.mexc_summary.setText("Monitor MEXC disattivato dalla riga di comando.")

            sync = mexc.get("time_sync", {})
            base_asset = str(mexc.get("base_asset") or "ARRR")
            quote_asset = str(mexc.get("quote_asset") or "USDT")
            values = {
                "mexc_account_type": account.get("account_type", "—"),
                "mexc_can_trade": _yes_no(account.get("can_trade")),
                "mexc_can_deposit": _yes_no(account.get("can_deposit")),
                "mexc_can_withdraw": _yes_no(account.get("can_withdraw")),
                "mexc_symbol_allowed": _yes_no(mexc.get("symbol_allowed")),
                "mexc_permissions": ", ".join(account.get("permissions", [])) or "—",
                "mexc_time": (
                    f"OK, scarto {sync.get('offset_ms')} ms, RTT {sync.get('round_trip_ms')} ms"
                    if sync.get("available") else "Non disponibile"
                ),
            }
            for key, value in values.items():
                self._labels[key].setText(str(value))

            balance_rows = []
            for asset in dict.fromkeys((base_asset, quote_asset)):
                item = mexc.get("balances", {}).get(asset, {})
                balance_rows.append((
                    asset,
                    item.get("free", "—"),
                    item.get("locked", "—"),
                    item.get("total", "—"),
                ))
            self._set_rows(self.mexc_balances_table, balance_rows)

            order_rows = []
            for item in mexc.get("open_orders", []):
                order_rows.append((
                    item.get("side", "—"),
                    item.get("type", "—"),
                    item.get("price", "—"),
                    item.get("original_quantity", "—"),
                    item.get("executed_quantity", "—"),
                    item.get("status", "—"),
                    item.get("client_order_id", "—"),
                ))
            self._set_rows(self.mexc_orders_table, order_rows)

        def _fill_portfolio(self, portfolio: dict[str, Any]) -> None:
            totals = portfolio.get("totals", {})
            venues = portfolio.get("venues", {})
            coverage = portfolio.get("coverage", {})
            rebalance = portfolio.get("rebalance", {})
            price = portfolio.get("price", {})
            assets = portfolio.get("assets", {})
            base_ticker = str(assets.get("base_ticker") or "ARRR")
            kdf_quote_ticker = str(
                assets.get("kdf_quote_ticker") or "USDT-BEP20"
            )
            cex_quote_asset = str(assets.get("cex_quote_asset") or "USDT")
            self._base_ticker = base_ticker
            self._set_caption("portfolio_arrr", f"{base_ticker} totali")
            self._set_caption("portfolio_usdt", f"{cex_quote_asset} totali")
            self._set_caption(
                "portfolio_arrr_share", f"Quota valore {base_ticker}"
            )
            self._set_caption(
                "portfolio_price", f"Prezzo {base_ticker}/{cex_quote_asset}"
            )
            self._set_caption(
                "portfolio_sell_capacity", f"Vendita {base_ticker} su KDF"
            )
            self._set_caption(
                "portfolio_buy_capacity", f"Acquisto {base_ticker} su KDF"
            )
            self._set_caption(
                "portfolio_target_arrr", f"Obiettivo {base_ticker} su KDF"
            )
            self._set_caption(
                "portfolio_target_usdt", f"Obiettivo {kdf_quote_ticker} su KDF"
            )
            if portfolio.get("available"):
                self.portfolio_summary.setText(
                    "Esposizione calcolata sui saldi liberi KDF e MEXC Spot. "
                    "Valori e capacità sono stime conservative, non disponibilità garantite."
                )
            else:
                self.portfolio_summary.setText(
                    f"Esposizione non completa: {portfolio.get('reason') or '—'}"
                )

            values = {
                "portfolio_value": _money(totals.get("estimated_value_usdt")),
                "portfolio_arrr": _amount(totals.get("arrr"), base_ticker),
                "portfolio_usdt": _amount(totals.get("usdt"), cex_quote_asset),
                "portfolio_arrr_share": _percentage(
                    totals.get("arrr_value_fraction")
                ),
                "portfolio_kdf_value": _money(totals.get("kdf_value_usdt")),
                "portfolio_mexc_value": _money(totals.get("mexc_value_usdt")),
                "portfolio_kdf_share": _percentage(
                    totals.get("kdf_value_fraction")
                ),
                "portfolio_price": (
                    f"{price.get('midpoint')} {cex_quote_asset} ({price.get('source')})"
                    if price.get("midpoint") is not None else "—"
                ),
                "portfolio_sell_capacity": _amount(
                    coverage.get("sell_arrr"), base_ticker
                ),
                "portfolio_buy_capacity": _amount(
                    coverage.get("buy_arrr"), base_ticker
                ),
                "portfolio_target_arrr": _amount(
                    rebalance.get("target_kdf_arrr"), base_ticker
                ),
                "portfolio_target_usdt": _amount(
                    rebalance.get("target_kdf_usdt"), cex_quote_asset
                ),
            }
            for key, value in values.items():
                self._labels[key].setText(value)

            kdf = venues.get("kdf", {})
            cex = venues.get("mexc", {})
            exposure_rows = [
                (
                    base_ticker,
                    kdf.get("arrr", "—"),
                    cex.get("arrr", "—"),
                    totals.get("arrr", "—"),
                    totals.get("arrr_value_usdt", "—"),
                ),
                (
                    kdf_quote_ticker,
                    kdf.get("usdt", "—"),
                    cex.get("usdt", "—"),
                    totals.get("usdt", "—"),
                    totals.get("usdt", "—"),
                ),
            ]
            self._set_rows(self.exposure_table, exposure_rows)

            directions = {
                "MEXC_TO_KDF": "MEXC → KDF",
                "KDF_TO_MEXC": "KDF → MEXC",
            }
            suggestions = [
                (
                    item.get("asset", "—"),
                    directions.get(item.get("direction"), item.get("direction", "—")),
                    item.get("amount", "—"),
                    item.get("estimated_value_usdt", "—"),
                    item.get("reason", "—"),
                )
                for item in rebalance.get("suggestions", [])
            ]
            self._set_rows(self.rebalance_table, suggestions)
            if portfolio.get("available") and not suggestions:
                self.rebalance_notice.setText(
                    "Nessun riequilibrio supera la soglia configurata."
                )
            else:
                self.rebalance_notice.setText(
                    str(rebalance.get("notice") or portfolio.get("reason") or "—")
                )
            pnl = portfolio.get("pnl", {})
            if pnl.get("available"):
                completeness = "completo" if pnl.get("complete") else "parziale"
                inventory_text = (
                    f" Inventario {base_ticker}: "
                    + _money(pnl.get("inventory_unrealized_usdt"))
                    + "."
                    if pnl.get("inventory_available")
                    else " Inventario escluso: "
                    + str(pnl.get("inventory_reason") or "baseline non applicabile")
                    + "."
                )
                self.pnl_notice.setText(
                    "P/L cicli {kind}: realizzato {realized}, non realizzato {unrealized}."
                    "{inventory}".format(
                        kind=completeness,
                        realized=_money(pnl.get("realized_usdt")),
                        unrealized=_money(pnl.get("unrealized_usdt")),
                        inventory=inventory_text,
                    )
                )
            else:
                self.pnl_notice.setText(
                    "P/L non disponibile: "
                    + str(pnl.get("reason") or "dati insufficienti")
                )

        def _fill_ledger(self, ledger: dict[str, Any]) -> None:
            summary = ledger.get("summary", {})
            inventory = ledger.get("inventory_pnl", {})
            base_ticker = str(
                ledger.get("base_asset")
                or inventory.get("asset")
                or getattr(self, "_base_ticker", "ARRR")
            )
            self.inventory_group.setTitle(f"Inventario {base_ticker}")
            self.ledger_table.setHorizontalHeaderItem(
                4, QtWidgets.QTableWidgetItem(base_ticker)
            )
            self.ledger_table.setHorizontalHeaderItem(
                6, QtWidgets.QTableWidgetItem(f"Residuo {base_ticker}")
            )
            values = {
                "ledger_realized": _money(summary.get("net_realized_usdt")),
                "ledger_unrealized": _money(summary.get("unrealized_usdt")),
                "ledger_total_pnl": _money(summary.get("estimated_total_pnl_usdt")),
                "ledger_fees": _money(summary.get("known_realized_fees_usdt")),
                "ledger_cycles": summary.get("cycles", "—"),
                "ledger_settled": summary.get("settled", "—"),
                "ledger_pending": summary.get("pending", "—"),
                "ledger_open": summary.get("open_exposure", "—"),
                "ledger_fills": summary.get("mexc_fills_imported", "—"),
                "ledger_fills_missing": summary.get(
                    "cycles_missing_verified_fills", "—"
                ),
                "inventory_quantity": _amount(
                    inventory.get("quantity"),
                    base_ticker,
                ),
                "inventory_total_cost": _money(inventory.get("total_cost_usdt")),
                "inventory_average_cost": _money(inventory.get("average_cost_usdt")),
                "inventory_market_value": _money(inventory.get("market_value_usdt")),
                "inventory_unrealized": _money(inventory.get("unrealized_usdt")),
                "inventory_swaps": inventory.get("swaps_applied", "—"),
                "inventory_adjustments": inventory.get("adjustments_applied", "—"),
            }
            for key, value in values.items():
                self._labels[key].setText(str(value))

            rows = []
            for item in ledger.get("recent", []):
                pnl = item.get("net_pnl_usdt")
                rows.append((
                    item.get("swap_uuid", "—"),
                    item.get("market_id", "—"),
                    item.get("dex_side", "—"),
                    item.get("kdf_outcome", "—"),
                    item.get("base_quantity", item.get("arrr_quantity", "—")),
                    item.get("cex_average_price") or "—",
                    item.get("residual_base", item.get("residual_arrr"))
                    if item.get("residual_base", item.get("residual_arrr")) is not None
                    else "—",
                    f"{pnl} ({item.get('pnl_kind')})" if pnl is not None else "—",
                ))
            self._set_rows(self.ledger_table, rows)

            if ledger.get("available"):
                warnings = ledger.get("warnings", [])
                suffix = "  •  " + "; ".join(map(str, warnings)) if warnings else ""
                self.ledger_notice.setText(str(ledger.get("notice") or "Ledger disponibile") + suffix)
            else:
                self.ledger_notice.setText(
                    "Ledger non disponibile: " + str(ledger.get("reason") or "—")
                )
            if inventory.get("available"):
                self.inventory_pnl_notice.setText(
                    "P/L inventario {asset}: {pnl}; baseline {key}, costo roll-forward "
                    "{cost} dopo {swaps} swap e {moves} movimenti esterni.".format(
                        pnl=_money(inventory.get("unrealized_usdt")),
                        asset=base_ticker,
                        key=inventory.get("baseline_key") or "—",
                        cost=_money(inventory.get("total_cost_usdt")),
                        swaps=inventory.get("swaps_applied", 0),
                        moves=inventory.get("adjustments_applied", 0),
                    )
                )
            else:
                self.inventory_pnl_notice.setText(
                    "P/L inventario non disponibile: "
                    + str(inventory.get("reason") or "dati insufficienti")
                )

        def _fill_alarms(self, snapshot: dict[str, Any]) -> None:
            self.overview_alarms.clear()
            alarms: list[tuple[str, str]] = []
            for section, message in snapshot.get("remote_errors", {}).items():
                alarms.append(("CONNESSIONE", f"{section}: {message}"))
            for alarm in snapshot.get("desktop", {}).get("alarms", []):
                alarms.append((
                    str(alarm.get("severity", "AVVISO")),
                    str(alarm.get("message") or alarm.get("code") or "Controllo richiesto"),
                ))
            for section, message in snapshot.get("mexc", {}).get("errors", {}).items():
                alarms.append(("MEXC", f"{section}: {message}"))
            coverage = snapshot.get("coverage", {})
            if coverage.get("state") == "BLOCKED":
                alarms.append((
                    "CRITICAL",
                    str(coverage.get("reason") or "Mercato KDF bloccato per copertura"),
                ))
            elif coverage.get("state") == "OVERRIDE":
                alarms.append((
                    "CRITICAL",
                    "Forzatura copertura attiva: il controllo fondi MEXC è ignorato",
                ))
            if not snapshot.get("offline"):
                for message in snapshot.get("portfolio", {}).get("warnings", []):
                    alarms.append(("PORTAFOGLIO", str(message)))
            if not alarms:
                item = QtWidgets.QListWidgetItem("Nessun allarme attivo")
                item.setForeground(QtGui.QColor("#18794e"))
                self.overview_alarms.addItem(item)
                return
            for severity, message in alarms:
                item = QtWidgets.QListWidgetItem(f"[{severity}] {message}")
                item.setForeground(QtGui.QColor("#b42318" if severity in {"ERROR", "CRITICAL"} else "#8a5b00"))
                self.overview_alarms.addItem(item)

        def _set_caption(self, key: str, text: str) -> None:
            caption = self._labels.get(f"{key}__caption")
            if caption is not None:
                caption.setText(text)

        def _set_rows(self, table: Any, rows: list[tuple[Any, ...]]) -> None:
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    table.setItem(row_index, column_index, item)
            table.resizeColumnsToContents()

        def closeEvent(self, event: Any) -> None:
            if not self.worker.isRunning():
                event.accept()
                return
            self.worker.requestInterruption()
            self.worker.refresh_now()
            if not self.worker.wait(4_000):
                self._close_pending = True
                self.hide()
                event.ignore()
                return
            event.accept()

        @Slot()
        def _complete_deferred_close(self) -> None:
            if self._close_pending:
                self.close()

    return {"SnapshotWorker": SnapshotWorker, "MainWindow": MainWindow}


def _yes_no(value: Any) -> str:
    if value is True:
        return "Sì"
    if value is False:
        return "No"
    return "Non dichiarato"


def _amount(value: Any, unit: str) -> str:
    return f"{value} {unit}" if value is not None else "—"


def _money(value: Any) -> str:
    return f"{value} USDT" if value is not None else "—"


def _percentage(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{(Decimal(str(value)) * Decimal('100')):.2f}%"
    except (InvalidOperation, TypeError, ValueError):
        return "—"


def _coverage_label(coverage: Any) -> str:
    if not isinstance(coverage, dict):
        return "Non disponibile"
    return {
        "OK": "COPERTURA OK",
        "BLOCKED": "MERCATO BLOCCATO",
        "OVERRIDE": "FORZATURA ATTIVA",
        "MONITOR_ONLY": "Solo monitoraggio",
        "DISABLED": "Disattivata",
    }.get(str(coverage.get("state")), str(coverage.get("state") or "—"))


def _balance_text(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "Nessuno"
    return ", ".join(f"{asset} {amount}" for asset, amount in sorted(value.items()))
