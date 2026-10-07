"""Private bounded diagnostic metadata; never accepts request/response payloads."""
from __future__ import annotations
import json
import logging
import math
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


class PrivateDiagnosticLog(RotatingFileHandler):
    def handleError(self, record):
        # Let the asynchronous writer count failures without stderr dumps.
        raise OSError("private diagnostic write failed")

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')


# Values come exclusively from internal metadata, never arbitrary exception text.
TEXT = {
    'event', 'request_id', 'method', 'venue', 'symbol', 'lane', 'phase',
    'outcome', 'failure_kind', 'reason', 'source', 'route', 'transport_phase', 'transport_failure', 'resolver_frontend', 'upstream_dns_scope', 'link_kind',
}
NUMBER = {
    'elapsed_ms', 'queue_ms', 'startup_ms', 'send_ms', 'reply_wait_ms',
    'resolver_queue_ms', 'resolver_call_ms', 'interface_index', 'link_carrier',
    'path_sample_interval_ms', 'link_carrier_changes_total', 'link_carrier_changes_delta',
    'link_rx_errors_total', 'link_tx_errors_total', 'link_rx_dropped_total', 'link_tx_dropped_total',
    'link_rx_errors_delta', 'link_tx_errors_delta', 'link_rx_dropped_delta', 'link_tx_dropped_delta',
    'wifi_quality_raw', 'wifi_signal_dbm', 'wifi_retry_discard_total', 'wifi_misc_discard_total',
    'wifi_missed_beacon_total', 'wifi_retry_discard_delta', 'wifi_misc_discard_delta', 'wifi_missed_beacon_delta',
    'resolver_transactions_total', 'resolver_cache_hits_total', 'resolver_cache_misses_total',
    'resolver_transactions_delta', 'resolver_cache_hits_delta', 'resolver_cache_misses_delta',
    'plugin_pid', 'plugin_exit_code', 'protocol_sequence', 'http_sequence',
    'adapter_thread_cpu_ms', 'adapter_process_cpu_ms', 'transport_errno', 'transport_http_status',
    'transport_phase_elapsed_ms', 'http_elapsed_ms', 'http_thread_cpu_ms', 'dns_ms', 'connect_tls_ms', 'connection_total_ms',
    'connect_or_headers_ms', 'connection_queue_ms', 'request_send_ms', 'headers_wait_ms',
    'response_body_ms', 'response_decode_ms', 'diagnostic_write_failures',
    'adapter_ms', 'timeout_ms', 'reply_budget_ms', 'http_status',
    'book_observed_ms', 'volume_observed_ms', 'now_ms', 'book_age_ms',
    'volume_age_ms', 'book_max_age_ms', 'volume_max_age_ms', 'sequence',
    'failures', 'retry_delay_ms', 'started_at_ms', 'wall_elapsed_ms', 'dropped_records', 'wakeup_lag_ms',
}
BOOL = {'execution_unknown', 'mutating', 'session_reused', 'connection_reused', 'dns_cache_hit',
        'resolver_inflight', 'network_path_available', 'resolver_config_available',
        'resolver_stats_available', 'wifi_stats_available', 'link_sample_reset'}
_sink = None
_sink_lock = threading.RLock()


class NetworkDiagnostics:
    def __init__(self, path):
        self.handler = PrivateDiagnosticLog(str(path), maxBytes=16*1024*1024, backupCount=2)
        incident_path = (str(path).replace('-diagnostics.jsonl', '-incidents.jsonl')
                         if str(path).endswith('-diagnostics.jsonl') else str(path)+'.incidents')
        self.incidents = PrivateDiagnosticLog(incident_path,
                                             maxBytes=8*1024*1024, backupCount=2)
        self._last_heartbeat = 0.0
        self._write_failures = 0
        self._last = {}
        self._lock = threading.RLock()
        self._queue = queue.Queue(maxsize=2048)
        self._dropped = 0
        threading.Thread(target=self._write, name='network-diagnostics', daemon=True).start()

    def _write(self):
        while True:
            record = self._queue.get()
            try:
                with self._lock:
                    dropped = self._dropped
                record['dropped_records'] = dropped
                record['diagnostic_write_failures'] = self._write_failures
                self.handler.handle(logging.LogRecord(__name__, logging.INFO, '', 0,
                                    json.dumps(record, separators=(',', ':')), (), None))
                incident = (bool(record.get('transport_failure')) or
                            (record.get('transport_http_status') or 0) >= 400 or record.get('outcome') in {'error', 'transport_error', 'client_error',
                            'rpc_error', 'invalid_response'} or record.get('event') in
                            {'market_rejected', 'plugin_lifecycle', 'network_path_sample'} or record.get('elapsed_ms', 0) >= 5000)
                if incident:
                    self.incidents.handle(logging.LogRecord(__name__, logging.INFO, '', 0,
                        json.dumps(record, separators=(',', ':')), (), None))
                if time.monotonic()-self._last_heartbeat >= 60:
                    heartbeat = {k: record[k] for k in ('schema','observed_at_utc','pid',
                                 'dropped_records','diagnostic_write_failures')}
                    heartbeat['event'] = 'diagnostic_heartbeat'
                    self.incidents.handle(logging.LogRecord(__name__, logging.INFO, '', 0,
                        json.dumps(heartbeat, separators=(',', ':')), (), None))
                    self._last_heartbeat = time.monotonic()
            except Exception:
                self._write_failures += 1
            finally:
                self._queue.task_done()

    def emit(self, event, *, throttle_key=None, **fields):
        try:
            if throttle_key is not None:
                with self._lock:
                    now = time.monotonic()
                    if now - self._last.get(throttle_key, float('-inf')) < 5:
                        return
                    # Keep observability memory bounded even with dynamic symbols.
                    if len(self._last) >= 256:
                        self._last.clear()
                    self._last[throttle_key] = now
            record = {'schema': 1, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
                      'pid': os.getpid()}
            for key, value in {'event': event, **fields}.items():
                if value is None:
                    continue
                if key in TEXT and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:.-]{1,96}', value):
                    record[key] = value
                elif key in NUMBER and type(value) in (int, float) and math.isfinite(value):
                    record[key] = value
                elif key in BOOL and type(value) is bool:
                    record[key] = value
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                with self._lock:
                    self._dropped += 1
        except Exception:
            pass  # Diagnostics must never affect orders, refreshes or RPC outcomes.


def configure(path):
    global _sink
    with _sink_lock:
        if _sink is None:
            try:
                _sink = NetworkDiagnostics(path)
                from .network_path import start_path_sampler
                start_path_sampler(_sink)
            except Exception:
                pass
        return _sink


def emit(event, **fields):
    if _sink is not None:
        _sink.emit(event, **fields)
