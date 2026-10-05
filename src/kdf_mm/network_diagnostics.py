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
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')


# Values come exclusively from internal metadata, never arbitrary exception text.
TEXT = {
    'event', 'request_id', 'method', 'venue', 'symbol', 'lane', 'phase',
    'outcome', 'failure_kind', 'reason', 'source', 'route',
}
NUMBER = {
    'elapsed_ms', 'queue_ms', 'startup_ms', 'send_ms', 'reply_wait_ms',
    'adapter_ms', 'timeout_ms', 'reply_budget_ms', 'http_status',
    'book_observed_ms', 'volume_observed_ms', 'now_ms', 'book_age_ms',
    'volume_age_ms', 'book_max_age_ms', 'volume_max_age_ms', 'sequence',
    'failures', 'retry_delay_ms', 'started_at_ms', 'wall_elapsed_ms', 'dropped_records', 'wakeup_lag_ms',
}
BOOL = {'execution_unknown', 'mutating'}
_sink = None
_sink_lock = threading.RLock()


class NetworkDiagnostics:
    def __init__(self, path):
        self.handler = PrivateDiagnosticLog(str(path), maxBytes=16*1024*1024, backupCount=2)
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
                self.handler.handle(logging.LogRecord(__name__, logging.INFO, '', 0,
                                    json.dumps(record, separators=(',', ':')), (), None))
            except Exception:
                pass
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
            except Exception:
                pass
        return _sink


def emit(event, **fields):
    if _sink is not None:
        _sink.emit(event, **fields)
