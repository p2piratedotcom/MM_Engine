"""Bounded stdio RPC; no transparent retries, especially after uncertain writes."""

from __future__ import annotations
import atexit
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

from ..mexc import MexcError, MexcSymbolRules, MexcTimeSync, SymbolCheck
from ..models import OrderBook, PriceLevel
from .plugin_catalog import plugin_directory, plugin_state_directory

_pool = {}
_pool_lock = threading.RLock()
MAX_FRAME = 1024 * 1024


def decode(value):
    if not isinstance(value, dict) or set(value) != {"type", "value"}:
        return value
    fields = value["value"]
    if not isinstance(fields, dict):
        raise ValueError("invalid plugin result")
    if value["type"] == "OrderBook":

        def levels(name):
            result = []
            for row in fields[name]:
                if row.get("type") != "PriceLevel":
                    raise ValueError("invalid price-level type")
                price = Decimal(row["value"]["price"])
                quantity = Decimal(row["value"]["quantity"])
                if not price.is_finite() or not quantity.is_finite():
                    raise ValueError("non-finite order-book decimal")
                result.append(PriceLevel(price, quantity))
            return tuple(result)

        return OrderBook(levels("bids"), levels("asks"), fields["observed_at_ms"])
    if value["type"] == "SymbolRules":
        fields = dict(fields)
        for key in (
            "quantity_step",
            "price_step",
            "min_quote_amount",
            "max_quote_amount",
        ):
            if fields[key] is not None:
                fields[key] = Decimal(fields[key])
                if not fields[key].is_finite() or fields[key] <= 0:
                    raise ValueError("invalid symbol-rule decimal")
        fields["order_types"] = tuple(fields["order_types"])
        return MexcSymbolRules(**fields)
    if value["type"] == "TimeSync":
        if (
            any(type(v) is not int for v in fields.values())
            or fields.get("round_trip_ms", -1) < 0
        ):
            raise ValueError("invalid time-sync result")
        return MexcTimeSync(**fields)
    if value["type"] == "SymbolCheck":
        return SymbolCheck(
            **{
                **fields,
                "order_types": tuple(fields["order_types"]),
                "problems": tuple(fields["problems"]),
            }
        )
    raise ValueError("unknown plugin result type")


class SpotPluginClient:
    supports_read_deadlines = True

    def __init__(
        self,
        item,
        *,
        api_key=None,
        api_secret=None,
        trading_enabled=False,
        timeout=10.0,
    ):
        self.item = item
        self.options = dict(
            api_key=api_key,
            api_secret=api_secret,
            trading_enabled=bool(trading_enabled),
            timeout=timeout,
        )
        self.trading_enabled = bool(trading_enabled)
        self._lock = threading.RLock()
        self._process = None
        self._sequence = 0
        self._buffer = b""

    def _read(self, deadline):
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("plugin deadline")
            with selectors.DefaultSelector() as selector:
                selector.register(self._process.stdout, selectors.EVENT_READ)
                if not selector.select(remaining):
                    raise TimeoutError("plugin deadline")
            chunk = os.read(self._process.stdout.fileno(), 65536)
            if not chunk:
                raise OSError("plugin stopped")
            self._buffer += chunk
            if len(self._buffer) > MAX_FRAME:
                raise ValueError("plugin response too large")
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def _start(self):
        from .. import http

        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "PATH",
                "HOME",
                "LD_LIBRARY_PATH",
                "LANG",
                "TMPDIR",
                "XDG_DATA_HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
            )
        }
        if getattr(sys, "frozen", False):
            command = [sys.executable, "exchange-plugin-host"]
        else:
            command = [sys.executable, "-m", "kdf_mm.exchange_plugin_host"]
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=environment,
        )
        from ..network_diagnostics import emit as diagnostic
        diagnostic("plugin_lifecycle", venue=self.item["venue"], plugin_pid=self._process.pid,
                   phase="spawn", outcome="started")
        self._buffer = b""
        os.set_blocking(self._process.stdin.fileno(), False)
        bootstrap = dict(
            catalog_dir=str(plugin_directory()),
            venue=self.item["venue"],
            options=self.options,
            adapter_state_dir=plugin_state_directory(self.item["venue"]),
            network_mode="tor" if http._wallet_proxy_url is not None else "direct",
            proxy=http._wallet_proxy_url,
        )
        self._send(bootstrap)
        ready = self._read(time.monotonic() + 10)
        from ..exchange_plugin_host import METHODS

        if ready != dict(
            protocol=1,
            venue=self.item["venue"],
            version=self.item["version"],
            methods=sorted(METHODS),
        ):
            raise ValueError("incompatible plugin handshake")

    def _send(self, value):
        line = (
            json.dumps(value, allow_nan=False, default=lambda v: str(v)) + "\n"
        ).encode()
        if len(line) > 65536:
            raise ValueError("plugin request too large")
        # Requests are small and the host continuously consumes stdin.
        view = memoryview(line)
        deadline = time.monotonic() + 5
        while view:
            with selectors.DefaultSelector() as selector:
                selector.register(self._process.stdin, selectors.EVENT_WRITE)
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("plugin input deadline")
            try:
                size = os.write(self._process.stdin.fileno(), view)
            except BlockingIOError:
                continue
            if size <= 0:
                raise OSError("plugin input closed")
            view = view[size:]

    def call(self, method, *args, **kwargs):
        from ..exchange_plugin_host import METHODS
        from ..network_diagnostics import emit as diagnostic
        from uuid import uuid4
        from .. import http

        if method not in METHODS:
            raise ValueError("unsupported Spot method")
        if method in ("place_limit_order", "cancel_order") and not self.trading_enabled:
            raise MexcError("Spot trading is disabled")
        started, identity = time.monotonic(), uuid4().hex
        started_at_ms = time.time_ns() // 1_000_000
        # Only known public symbol argument positions; never account/order IDs.
        symbol = args[0] if args and method in {
            'order_book', 'ticker_24h', 'symbol_rules', 'check_symbol'} else None
        diagnostic('plugin_request_start', request_id=identity, venue=self.item['venue'],
                   symbol=symbol, method=method, started_at_ms=started_at_ms)
        with self._lock:
            metrics = dict(queue_ms=round((time.monotonic()-started)*1000, 2))
            phase, outcome, failure_kind, status = 'startup', 'received', None, None
            adapter_ms, unknown = None, False
            plugin_pid = None
            transport_records = []
            transport_phase = None
            transport_phase_received = None
            try:
                stage = time.monotonic()
                try:
                    if self._process is None or self._process.poll() is not None:
                        self.close()
                        self._start()
                finally:
                    metrics['startup_ms'] = round((time.monotonic()-stage)*1000, 2)
                plugin_pid = self._process.pid
                self._sequence += 1
                timeout = (kwargs.get("total_timeout") or kwargs.get("timeout")
                           or self.options["timeout"])
                budget = min(35.0, max(float(timeout)+1,
                             kwargs.get("max_round_trip_ms", 0)/1000+1))
                metrics.update(timeout_ms=round(float(timeout)*1000), reply_budget_ms=round(budget*1000))
                phase, stage = 'send', time.monotonic()
                self._send(dict(id=self._sequence, method=method, args=args, kwargs=kwargs))
                metrics['send_ms'] = round((time.monotonic()-stage)*1000, 2)
                phase, stage = 'reply_wait', time.monotonic()
                try:
                    reply_deadline = time.monotonic()+budget
                    # Optional host phase frames share the original deadline.
                    # They cannot extend a request or contain payloads.
                    for _ in range(128):
                        response = self._read(reply_deadline)
                        if isinstance(response, dict) and set(response) == {'id', 'diagnostic_phase'}:
                            if response['id'] != self._sequence or response['diagnostic_phase'] not in {
                                'connect_or_headers', 'connect_tls', 'dns', 'request_send',
                                'headers_wait', 'response_body', 'response_decode', 'connection_queue'}:
                                raise ValueError('invalid plugin diagnostic frame')
                            transport_phase = response['diagnostic_phase']
                            transport_phase_received = time.monotonic()
                            continue
                        break
                    else:
                        raise ValueError('too many plugin diagnostic frames')
                finally:
                    metrics['reply_wait_ms'] = round((time.monotonic()-stage)*1000, 2)
                phase = 'decode'
                if not isinstance(response, dict) or response.get("id") != self._sequence:
                    raise ValueError("invalid plugin response identity")
                adapter_ms = response.get('adapter_ms')
                from ..network_diagnostics import NUMBER
                for metric in ('adapter_thread_cpu_ms', 'adapter_process_cpu_ms'):
                    if type(response.get(metric)) in (int, float):
                        metrics[metric] = response[metric]
                raw_records = response.get('transport_records', [])
                if isinstance(raw_records, list):
                    # Never forward URLs, headers, bodies or account fields.
                    allowed = {'transport_phase', 'transport_failure', 'session_reused',
                               'connection_reused', 'dns_cache_hit', 'resolver_inflight',
                               'resolver_queue_ms', 'resolver_call_ms'} | {k for k in NUMBER if
                        k not in {'http_sequence'} and k.startswith(('http_', 'transport_', 'dns_', 'connect_', 'connection_',
                                      'request_send_', 'headers_wait_', 'response_body_', 'response_decode_'))}
                    transport_records = [{k:v for k,v in row.items() if k in allowed}
                                         for row in raw_records[:8] if isinstance(row, dict)]
                if "error" in response:
                    phase = 'adapter'
                    error = response["error"]
                    failure_kind = error.get('failure_kind')
                    if failure_kind not in {'timeout','http_rejected','invalid_data','connection_error','adapter_error'}:
                        failure_kind = 'timeout' if error.get('retryable_timeout') else 'adapter_error'
                    exc = MexcError(f"{self.item['venue']} adapter request failed"
                        + (" (timeout)" if error.get("retryable_timeout") else ""),
                        status=error.get("status"), payload={"code": error.get("code")},
                        execution_unknown=bool(error.get("execution_unknown")))
                    exc.venue = self.item["venue"]
                    exc.failure_kind = failure_kind
                    exc.phase = phase
                    raise exc
                result = decode(response["result"])
                if isinstance(result, OrderBook) and (
                    type(result.observed_at_ms) is not int
                    or result.observed_at_ms > time.time_ns()//1000000):
                    raise ValueError("invalid order-book observation time")
                return result
            except MexcError as exc:
                outcome, status = 'error', exc.status
                unknown = bool(getattr(exc, 'execution_unknown', False))
                raise
            except Exception as caught:
                outcome, failure_kind = 'error', ('timeout' if isinstance(caught, TimeoutError)
                                                 else type(caught).__name__)
                self.close()
                unknown = method in ("place_limit_order", "cancel_order")
                exc = MexcError(f"{self.item['venue']} plugin unavailable", execution_unknown=unknown)
                exc.venue = self.item["venue"]
                exc.failure_kind = failure_kind
                exc.phase = phase
                raise exc from None
            finally:
                for index, transport in enumerate(transport_records, 1):
                    diagnostic('http_request_end', request_id=identity, venue=self.item['venue'],
                               method=method, plugin_pid=plugin_pid, http_sequence=index,
                               lane='private' if self.options['api_key'] else 'public',
                               route='tor' if http._wallet_proxy_url is not None else 'direct', **transport)
                diagnostic('plugin_request_end', request_id=identity, venue=self.item['venue'],
                           symbol=symbol, method=method, phase=phase, outcome=outcome,
                           plugin_pid=plugin_pid, protocol_sequence=self._sequence,
                           transport_phase=transport_phase,
                           transport_phase_elapsed_ms=(round((time.monotonic()-transport_phase_received)*1000, 2)
                               if transport_phase_received is not None else None),
                           failure_kind=failure_kind, http_status=status, adapter_ms=adapter_ms,
                           execution_unknown=unknown, lane='private' if self.options['api_key'] else 'public',
                           route='tor' if http._wallet_proxy_url is not None else 'direct',
                           elapsed_ms=round((time.monotonic()-started)*1000, 2),
                           wall_elapsed_ms=time.time_ns()//1_000_000-started_at_ms, **metrics)

    def __getattr__(self, name):
        from ..exchange_plugin_host import METHODS

        if name not in METHODS:
            raise AttributeError(name)
        return lambda *args, **kwargs: self.call(name, *args, **kwargs)

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        process, self._process = self._process, None
        if process is None:
            return
        from ..network_diagnostics import emit as diagnostic
        diagnostic("plugin_lifecycle", venue=self.item["venue"], plugin_pid=process.pid,
                   phase="close", outcome="closing", plugin_exit_code=process.poll())
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        process.stdout.close()
        diagnostic("plugin_lifecycle", venue=self.item["venue"], plugin_pid=process.pid,
                   phase="exit", outcome="stopped", plugin_exit_code=process.returncode)


def client_for(item, **kwargs):
    # Pooling prevents every preview or account refresh starting another process.
    key = (
        str(plugin_directory()),
        item["venue"],
        kwargs.get("api_key"),
        kwargs.get("api_secret"),
        bool(kwargs.get("trading_enabled", False)),
        kwargs.get("timeout", 10.0),
    )
    with _pool_lock:
        client = _pool.get(key)
        if client is None:
            # Remove old credential versions; never keep obsolete signing keys.
            for previous in list(_pool):
                if previous[:2] == key[:2] and previous[4] == key[4]:
                    _pool.pop(previous).close()
            client = SpotPluginClient(item, **kwargs)
            _pool[key] = client
        return client


def close_plugins():
    with _pool_lock:
        for client in _pool.values():
            client.close()
        _pool.clear()


atexit.register(close_plugins)
