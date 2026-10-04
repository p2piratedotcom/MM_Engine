"""Attach-only P2Pirate launcher. Wallet secrets arrive once over stdin.

This Linux desktop adapter does not own KDF or the Tor process. The wallet
starts both, keeps stdin open, and receives a single READY line on stdout.
The operator CLI remains available separately.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .http import configure_wallet_proxy
from .vps_agent import serve


MAX_BOOTSTRAP_BYTES = 65536


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def _state_secrets(state_dir: Path) -> tuple[str, str]:
    path = state_dir / "service-secrets.json"
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"snapshot": secrets.token_urlsafe(48),
                           "event": secrets.token_urlsafe(48)}, output)
                output.flush()
                os.fsync(output.fileno())
    if path.stat().st_mode & 0o077:
        raise ValueError("service secrets must have mode 0600")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    snapshot, event = loaded["snapshot"], loaded["event"]
    if not all(isinstance(value, str) and len(value) >= 32
               for value in (snapshot, event)):
        raise ValueError("service secrets are invalid")
    return snapshot, event


def _settings(bootstrap: dict[str, object]) -> tuple[Settings, str, bool]:
    from .exchanges.plugin_catalog import configure_plugins
    state_dir = _absolute_path(bootstrap.get("state_dir"), "state_dir")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_dir.stat().st_mode & 0o077:
        raise ValueError("state_dir must have mode 0700")
    coins = _absolute_path(bootstrap.get("coin_registry_path"), "coin_registry_path")
    if not coins.is_file():
        raise ValueError("coin_registry_path is not a file")
    rpc_url = bootstrap.get("kdf_rpc_url")
    if not isinstance(rpc_url, str):
        raise ValueError("kdf_rpc_url is required")
    parsed = urlparse(rpc_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.path not in ("", "/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("KDF RPC must be local loopback HTTP")
    userpass = bootstrap.get("kdf_rpc_userpass")
    token = bootstrap.get("agent_token")
    if not isinstance(userpass, str) or not userpass:
        raise ValueError("kdf_rpc_userpass is required")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("agent_token must be at least 32 characters")
    mode = bootstrap.get("network_mode", "tor")
    if mode not in ("tor", "direct"):
        raise ValueError("network_mode must be tor or direct")
    proxy = bootstrap.get("tor_http_proxy")
    if mode == "tor" and not isinstance(proxy, str):
        raise ValueError("Tor proxy is required in Tor mode")
    if mode == "direct" and proxy is not None:
        raise ValueError("Tor proxy cannot be set in direct mode")
    configure_wallet_proxy(proxy if mode == "tor" else None)
    snapshot, event = _state_secrets(state_dir)
    requested_markets = bootstrap.get("markets")
    if requested_markets is None:
        markets = Settings().markets
    elif (isinstance(requested_markets, list) and requested_markets
          and all(isinstance(item, str) and item for item in requested_markets)):
        markets = tuple(requested_markets)
    else:
        raise ValueError("markets must be a non-empty string list")
    live = bootstrap.get("live", {})
    live_keys = {"kdf_order_writes", "auto_hedge", "cex_trading"}
    if (not isinstance(live, dict) or set(live) - live_keys or any(
        not isinstance(live.get(key, False), bool)
        for key in live_keys
    )):
        raise ValueError("live settings must contain booleans")
    with_cex = bootstrap.get("with_cex", False)
    if not isinstance(with_cex, bool):
        raise ValueError("with_cex must be boolean")
    if any(live.values()) and not with_cex:
        raise ValueError("live trading requires the local CEX worker")
    if live.get("auto_hedge") and not live.get("cex_trading"):
        raise ValueError("automatic hedging requires CEX trading permission")
    if live.get("kdf_order_writes") and not live.get("auto_hedge"):
        raise ValueError("KDF maker orders require automatic hedging")
    profile = bootstrap.get("cex_profile", "default")
    if not isinstance(profile, str) or not profile:
        raise ValueError("cex_profile is invalid")
    settings = replace(
        Settings(),
        markets=markets,
        kdf_rpc_url=rpc_url,
        kdf_rpc_userpass=userpass,
        kdf_coins_path=str(coins),
        kdf_coins_manifest="",
        manage_kdf=False,
        agent_bind="127.0.0.1",
        agent_port=0,
        agent_token=token,
        snapshot_secret=snapshot,
        event_secret=event,
        state_db=str(state_dir / "ownership.sqlite3"),
        outbox_db=str(state_dir / "hedge-outbox.sqlite3"),
        desktop_journal_db=str(state_dir / "hedge-journal.sqlite3"),
        coverage_audit_db=str(state_dir / "coverage.sqlite3"),
        repricing_state_path=str(state_dir / "repricing.json"),
        coin_profile_path=str(state_dir / "coin-profile.json"),
        live_trading=live.get("cex_trading", False),
        auto_hedge=live.get("auto_hedge", False),
        kdf_order_writes=live.get("kdf_order_writes", False),
        live_transfers=False,
        coverage_lease_ttl_seconds=30.0,
        mexc_public_feed=True,
    )
    configure_plugins(bootstrap.get("cex_plugin_directory"), state_dir=str(state_dir))
    return settings, profile, with_cex


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("wallet service is currently available on Linux")
    os.umask(0o077)
    line = sys.stdin.buffer.readline(MAX_BOOTSTRAP_BYTES + 1)
    if len(line) > MAX_BOOTSTRAP_BYTES or not line.endswith(b"\n"):
        raise ValueError("invalid wallet bootstrap size")
    bootstrap = json.loads(line)
    if not isinstance(bootstrap, dict):
        raise ValueError("wallet bootstrap must be a JSON object")
    settings, profile, with_cex = _settings(bootstrap)
    state_dir = Path(settings.state_db).parent
    with (state_dir / "engine.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("MM_Engine is already active for this profile") from exc

        def on_ready(port, server):
            print("MM_ENGINE_READY " + json.dumps({"protocol": 1, "port": port}),
                  flush=True)

            def stop_on_wallet_exit():
                # The wallet owns this foreground process. EOF triggers the
                # same graceful cancellation/reconciliation path as shutdown.
                while sys.stdin.buffer.read(4096):
                    pass
                server.shutdown()

            threading.Thread(target=stop_on_wallet_exit, daemon=True).start()

        report = serve(settings, with_mexc=with_cex, mexc_profile=profile,
                       wallet_mode=True, on_ready=on_ready)
        from .exchanges.plugin_client import close_plugins
        close_plugins()
        print("MM_ENGINE_STOPPED " + json.dumps(report), flush=True)
    return 0 if report["orders_remaining"] == 0 and report["cancel_error"] is None else 2
