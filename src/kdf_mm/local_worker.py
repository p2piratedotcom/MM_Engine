"""Headless local MEXC worker: no Qt, desktop session UI, or VPS required."""
from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


class WorkerBusyError(ValueError):
    pass


class WorkerLock:
    def __init__(self, journal_path, *, cooperative=False, rebalance=False):
        self.path = Path(str(journal_path) + ".worker.lock")
        self.file = None
        self.cooperative = cooperative
        self.rebalance = rebalance

    def __enter__(self):
        from .rebalance_guard import rebalance_guard
        gate = (nullcontext() if self.rebalance else
                rebalance_guard(str(self.path).removesuffix('.worker.lock') + '.rebalance.lock',
                                exclusive=self.cooperative))
        with gate:
            return self._acquire()

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise WorkerBusyError("un worker MEXC/GATE usa già questo journal; aggiornare il servizio per consentire il riequilibrio coordinato") from None
        try:
            from .rebalance_guard import assert_no_pending
            # A cooperative worker may initialize so the wallet can query an
            # uncertain rebalance after restart. EVERY operational cycle still
            # passes coordinated_cycle's shared gate and assert_no_pending.
            if not self.cooperative:
                assert_no_pending(str(self.path).removesuffix('.worker.lock'))
        except Exception:
            self.file.close()
            raise
        self.file.seek(0)
        self.file.truncate()
        self.file.write('rebalance-cooperative-v1' if self.cooperative else 'exclusive')
        self.file.flush()
        return self

    def __exit__(self, *args):
        self.file.close()


@contextmanager
def rebalance_worker_access(journal_path):
    """Caller MUST hold the exclusive rebalance guard throughout this scope."""
    from .rebalance_guard import assert_no_pending
    assert_no_pending(journal_path)
    lock = WorkerLock(journal_path, rebalance=True)
    try:
        lock.__enter__()
    except WorkerBusyError:
        if lock.path.read_text() != 'rebalance-cooperative-v1':
            raise
        # The live cooperative owner cannot start a cycle under our guard.
        yield
    else:
        try:
            yield
        finally:
            lock.__exit__()


def coordinated_cycle(runtime, journal_path):
    from .rebalance_guard import worker_cycle_guard
    from .desktop_runtime import DesktopRuntimeResult
    try:
        with worker_cycle_guard(str(journal_path) + '.rebalance.lock'):
            return runtime()
    except ValueError as exc:
        return DesktopRuntimeResult(sync=None, coverage=None, hedging=None, errors={'rebalance': str(exc)})


class LocalMexcWorker:
    def __init__(self, settings, *, profile="default"):
        from .auto_hedge import AutomaticHedgeEngine
        from .credentials import LinuxSecretService, SecretServiceError
        from .desktop_agent import DesktopAgent, VpsEventClient
        from .desktop_coverage import DesktopCoveragePublisher
        from .desktop_runtime import DesktopRuntime
        from .journal import HedgeJournal
        from .exchanges import private_client, supported_venues, load_config
        from .exchanges.plugin_catalog import installed_plugins
        if settings.auto_hedge and not settings.live_trading:
            raise ValueError("copertura live richiede KDF_MM_LIVE_TRADING=true")
        from .network_diagnostics import configure
        configure(Path(settings.desktop_journal_db).parent / "worker-network-diagnostics.jsonl")
        keyring = LinuxSecretService(profile=profile)
        self.lock = WorkerLock(settings.desktop_journal_db, cooperative=True)
        self.lock.__enter__()
        try:
            self.journal = HedgeJournal(settings.desktop_journal_db)
            events = VpsEventClient(base_url=f"http://127.0.0.1:{settings.agent_port}", token=settings.agent_token)
            clients = {}
            for venue in supported_venues():
                try:
                    clients[venue] = private_client(venue, keyring,
                        base_url=getattr(settings, venue.lower() + "_base_url", None),
                        trading_enabled=settings.auto_hedge)
                except SecretServiceError:
                    continue
            if not clients:
                raise SecretServiceError("Configurare le credenziali di almeno un CEX Spot")
            self.runtime = DesktopRuntime(
                desktop=DesktopAgent(journal=self.journal, events=events,
                                     event_secret=settings.event_secret, consumer_id=settings.desktop_consumer_id),
                coverage=DesktopCoveragePublisher(mexc=None, clients=clients, vps=events, event_secret=settings.event_secret,
                           consumer_id=settings.desktop_consumer_id, assets=(settings.mexc_base_asset, "USDT"),
                           ttl_seconds=settings.coverage_lease_ttl_seconds, live_hedging_enabled=settings.auto_hedge,
                           include_all_spot_assets=True),
                hedging=AutomaticHedgeEngine(journal=self.journal, mexc=None, clients=clients,
                           venue_fees={venue: Decimal(load_config(venue).taker_fee) if installed_plugins() is not None
                            else getattr(settings, "cex_taker_fee" if venue == "MEXC" else venue.lower()+"_taker_fee",settings.cex_taker_fee)
                            for venue in clients},
                           max_slippage=settings.max_slippage,
                           fee_buffer=settings.cex_taker_fee, depth_limit=settings.mexc_depth_limit,
                           max_attempts=settings.auto_hedge_max_attempts) if settings.auto_hedge else None)
            cycle = self.runtime.run_once
            self.runtime.run_once = lambda: coordinated_cycle(cycle, settings.desktop_journal_db)
        except Exception:
            self.lock.__exit__()
            raise
        self.stop = threading.Event()
        self.interval = settings.desktop_poll_interval_seconds
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.runtime.run_forever,
                                       kwargs={"poll_interval": self.interval, "stop_event": self.stop},
                                       name="local-mexc-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                # Keep ownership until process exit; pending durable intents
                # will be queried, never resent, on next startup.
                return
        self.journal.close()
        self.lock.__exit__()


def _verified_maker_refund(kdf, swap_uuid: str) -> bool:
    """Return true only for a canonical failed swap with a completed refund."""
    if kdf is None:
        return False
    response = kdf.swap_status(swap_uuid)
    status = response.get("swap_data") if isinstance(response, Mapping) else None
    if not isinstance(status, Mapping):
        status = response
    if (
        not isinstance(status, Mapping)
        or status.get("uuid") != swap_uuid
        or not str(status.get("type", "")).startswith("Maker")
        or status.get("is_finished") is not True
        or status.get("is_success") is not False
    ):
        return False
    events = status.get("events")
    if not isinstance(events, list):
        return False
    types = []
    for item in events:
        event = item.get("event") if isinstance(item, Mapping) else None
        value = event.get("type") if isinstance(event, Mapping) else None
        if isinstance(value, str):
            types.append(value)
    if "MakerPaymentRefunded" not in types or not types or types[-1] != "Finished":
        return False
    refund_index = max(i for i, value in enumerate(types) if value == "MakerPaymentRefunded")
    return "MakerPaymentRefundFailed" not in types[refund_index + 1 :]


def local_settlement(journal_path, ownership, kdf=None):
    """Reconcile durable hedge evidence; never infer success or a refund."""
    def check(spec, store):
        if not spec.hedging_enabled:
            with store.lock:
                rows = store.db.execute("SELECT swap_uuid,outcome FROM strategy_consumption WHERE strategy_id=?", (spec.strategy_id,)).fetchall()
            for row in rows:
                swap = ownership.get_swap(row['swap_uuid'])
                order = ownership.get(swap.order_uuid) if swap is not None else None
                if order is None or order.hedging_enabled or swap.acknowledged:
                    continue
                if row['outcome'] == 'SUCCEEDED' and swap.state.value == 'SUCCEEDED':
                    ownership.acknowledge_swap(swap.swap_uuid)
                # Failed unhedged swaps keep the existing manual review gate.
            return
        path = Path(journal_path).resolve()
        if not path.is_file():
            return
        with store.lock:
            rows = store.db.execute("SELECT swap_uuid,outcome FROM strategy_consumption WHERE strategy_id=?", (spec.strategy_id,)).fetchall()
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            for row in rows:
                hedge = db.execute(
                    "SELECT state,target_quantity,filled_quantity FROM hedges WHERE swap_uuid=?",
                    (row["swap_uuid"],),
                ).fetchone()
                outcome = db.execute(
                    "SELECT kdf_success,terminal_event,acknowledged "
                    "FROM received_swap_outcomes WHERE swap_uuid=?",
                    (row["swap_uuid"],),
                ).fetchone()
                legs = db.execute("SELECT state FROM basket_legs WHERE swap_uuid=?", (row["swap_uuid"],)).fetchall()
                swap = ownership.get_swap(row["swap_uuid"])
                try:
                    hedge_complete = bool(
                        hedge
                        and hedge[0] == "FILLED"
                        and Decimal(str(hedge[1])) == Decimal(str(hedge[2]))
                        and legs
                        and all(leg[0] == "FILLED" for leg in legs)
                    )
                except (InvalidOperation, TypeError, ValueError):
                    hedge_complete = False
                if (
                    row["outcome"] == "SUCCEEDED"
                    and hedge_complete
                    and outcome
                    and outcome[0]
                    and swap
                    and not swap.acknowledged
                ):
                    ownership.acknowledge_swap(row["swap_uuid"])
                elif (
                    row["outcome"] in {"FAILED", "REFUNDED"}
                    and hedge_complete
                    and outcome
                    and outcome[0] == 0
                    and outcome[2] == 1
                    and swap
                    and not swap.acknowledged
                    and _verified_maker_refund(kdf, row["swap_uuid"])
                ):
                    ownership.acknowledge_refunded_swap(
                        row["swap_uuid"], terminal_event="MakerPaymentRefunded"
                    )
                    store.mark_refunded(row["swap_uuid"])
    return check
