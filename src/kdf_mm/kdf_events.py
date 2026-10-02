from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .kdf import KdfRpcClient


@dataclass(frozen=True, slots=True)
class KdfEventStreamStatus:
    running: bool
    connected: bool
    client_id: int
    events_received: int
    last_event_type: str | None
    last_event_ms: int | None
    consecutive_failures: int
    last_error: str | None


class KdfEventStream:
    """SSE accelerator for KDF state changes; durable polling remains authoritative."""

    def __init__(
        self,
        *,
        kdf: KdfRpcClient,
        client_id: int,
        on_event: Callable[[Mapping[str, Any]], None],
        reconnect_seconds: float = 2.0,
        open_timeout_seconds: float = 15.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if client_id < 0:
            raise ValueError("KDF event stream client id cannot be negative")
        if reconnect_seconds < 0.5 or open_timeout_seconds <= 0:
            raise ValueError("invalid KDF event stream timing")
        self.kdf = kdf
        self.client_id = client_id
        self.on_event = on_event
        self.reconnect_seconds = reconnect_seconds
        self.open_timeout_seconds = open_timeout_seconds
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.url = _event_stream_url(kdf.rpc_url, client_id)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._events_received = 0
        self._last_event_type: str | None = None
        self._last_event_ms: int | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kdf-event-stream",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return asdict(
                KdfEventStreamStatus(
                    running=self._thread is not None and self._thread.is_alive(),
                    connected=self._connected,
                    client_id=self.client_id,
                    events_received=self._events_received,
                    last_event_type=self._last_event_type,
                    last_event_ms=self._last_event_ms,
                    consecutive_failures=self._consecutive_failures,
                    last_error=self._last_error,
                )
            )

    @staticmethod
    def parse_data_line(line: bytes | str) -> Mapping[str, Any] | None:
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        if not text.startswith("data:"):
            return None
        try:
            payload = json.loads(text[5:].strip())
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, Mapping) else None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                request = Request(self.url, headers={"Accept": "text/event-stream"})
                with urlopen(request, timeout=self.open_timeout_seconds) as response:
                    with self._lock:
                        self._connected = True
                    # The connection must exist before KDF accepts the client_id.
                    self.kdf.enable_order_status_stream(self.client_id)
                    self.kdf.enable_swap_status_stream(self.client_id)
                    with self._lock:
                        self._consecutive_failures = 0
                        self._last_error = None
                    for line in response:
                        if self._stop.is_set():
                            break
                        event = self.parse_data_line(line)
                        if event is None:
                            continue
                        event_type = str(event.get("_type", ""))
                        if event_type not in {"ORDER_STATUS", "SWAP_STATUS"}:
                            continue
                        with self._lock:
                            self._events_received += 1
                            self._last_event_type = event_type
                            self._last_event_ms = self.clock_ms()
                        self.on_event(event)
            except Exception as exc:
                with self._lock:
                    self._consecutive_failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._connected = False
            self._stop.wait(self.reconnect_seconds)


def _event_stream_url(rpc_url: str, client_id: int) -> str:
    parsed = urlsplit(rpc_url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/event-stream", urlencode({"id": client_id}), "")
    )
