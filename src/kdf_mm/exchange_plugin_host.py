"""Spot protocol v1 host. One downloaded adapter per process, no KDF access API."""

from __future__ import annotations
import dataclasses
from contextlib import nullcontext
import importlib
import json
import sys
import time
from decimal import Decimal

from .exchanges.plugin_catalog import verify_catalog

METHODS = frozenset(
    {
        "server_time",
        "synchronize_time",
        "check_symbol",
        "symbol_rules",
        "order_book",
        "ticker_24h",
        "self_symbols",
        "account",
        "trade_fee",
        "open_orders",
        "account_trades",
        "test_limit_order",
        "place_limit_order",
        "query_order",
        "cancel_order",
    }
)
MAX_FRAME = 1024 * 1024


def encode(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite protocol decimal")
        return str(value)
    if dataclasses.is_dataclass(value):
        fields = {
            field.name: encode(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        kind = type(value).__name__
        # Tags describe the common contract, not exchange-native responses.
        return {"type": kind, "value": fields}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def reply(payload):
    line = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    if len(line.encode()) > MAX_FRAME:
        raise ValueError("plugin response too large")
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    from pathlib import Path

    bootstrap = sys.stdin.buffer.readline(65537)
    if len(bootstrap) > 65536 or not bootstrap.endswith(b"\n"):
        return 2
    bootstrap = json.loads(bootstrap)
    entries = verify_catalog(Path(bootstrap["catalog_dir"]))
    item = entries[bootstrap["venue"]]
    sys.path.insert(0, str(Path(bootstrap["catalog_dir"]) / item["adapter"]))
    module = importlib.import_module("cex_plugin.adapter")
    route = importlib.import_module("cex_plugin.http")
    mode = bootstrap["network_mode"]
    if (
        mode not in ("tor", "direct")
        or mode == "tor"
        and not bootstrap.get("proxy")
        or mode == "direct"
        and bootstrap.get("proxy") is not None
    ):
        return 2
    route.configure_wallet_proxy(bootstrap.get("proxy") if mode == "tor" else None)
    options = bootstrap["options"]
    if (
        set(options) - {"api_key", "api_secret", "trading_enabled", "timeout"}
        or type(options.get("trading_enabled")) is not bool
    ):
        return 2
    client = module.create_client(
        item["configuration"], state_dir=bootstrap.get("adapter_state_dir"), **options
    )
    if any(not callable(getattr(client, name, None)) for name in METHODS):
        raise ValueError("adapter does not implement the Spot contract")
    reply(
        {
            "protocol": 1,
            "venue": item["venue"],
            "version": item["version"],
            "methods": sorted(METHODS),
        }
    )
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line:
            close = getattr(route, "close_transports", None)
            if callable(close):
                try: close()
                except Exception: pass
            return 0
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            return 2
        request = json.loads(line)
        identity = request.get("id")
        method = request.get("method")
        started = time.monotonic()
        cpu_started, process_cpu_started = time.thread_time(), time.process_time()
        transport_records = []
        def measurements():
            return {"adapter_ms": round((time.monotonic()-started)*1000, 2),
                    "adapter_thread_cpu_ms": round((time.thread_time()-cpu_started)*1000, 2),
                    "adapter_process_cpu_ms": round((time.process_time()-process_cpu_started)*1000, 2),
                    "transport_records": transport_records}
        try:
            if type(identity) is not int or method not in METHODS:
                raise ValueError("invalid protocol request")
            if (
                method in ("place_limit_order", "cancel_order")
                and not options["trading_enabled"]
            ):
                raise ValueError("live trading disabled")
            arguments, keywords = request.get("args", []), request.get("kwargs", {})
            if not isinstance(arguments, list) or not isinstance(keywords, dict):
                raise ValueError("invalid protocol arguments")
            for key in ("quantity", "price"):
                if key in keywords:
                    keywords[key] = Decimal(keywords[key])
                    if not keywords[key].is_finite() or keywords[key] <= 0:
                        raise ValueError("invalid order decimal")
            if "side" in keywords:
                from cex_plugin.models import HedgeSide

                keywords["side"] = HedgeSide(keywords["side"])
            capture = getattr(route, "diagnostic_capture", None)
            progress_count = 0
            def progress(phase):
                nonlocal progress_count
                if progress_count >= 64:
                    return
                progress_count += 1
                reply({"id": identity, "diagnostic_phase": phase})
            with capture(progress=progress) if callable(capture) else nullcontext([]) as transport_records:
                result = getattr(client, method)(*arguments, **keywords)
            reply({"id": identity, "result": encode(result), **measurements()})
        except Exception as exc:
            # Never send raw remote errors, signed URLs or API credentials.
            code = getattr(exc, "payload", None)
            code = str(code.get("code", "")) if isinstance(code, dict) else ""
            if len(code) > 64 or any(ch not in "-0123456789" for ch in code):
                code = ""
            status = getattr(exc, "status", None)
            reply(
                {
                    "id": identity,
                    **measurements(),
                    "error": {
                        "status": status if type(status) is int else None,
                        "code": code,
                        "execution_unknown": bool(
                            getattr(
                                exc,
                                "execution_unknown",
                                method in ("place_limit_order", "cancel_order"),
                            )
                        ),
                        "retryable_timeout": "(timeout)" in str(exc),
                        # Fixed categories only; do not reflect arbitrary class
                        # names/messages from a downloaded adapter.
                        "failure_kind": (
                            "timeout" if "(timeout)" in str(exc) or isinstance(exc, TimeoutError)
                            else "http_rejected" if type(status) is int
                            else "invalid_data" if isinstance(exc, (ValueError, KeyError, TypeError))
                            else "connection_error" if isinstance(exc, OSError)
                            else "adapter_error"
                        ),
                    },
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
