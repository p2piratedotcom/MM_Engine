"""Immutable, verified Spot plugin snapshots; never hot-reload trading clients."""

from __future__ import annotations
import hashlib
import json
import re
import stat
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

PROTOCOL = 1
_snapshot: dict[str, dict] | None = None
_root: Path | None = None
_state_root: Path | None = None


def read_file(root: Path, name: str, digest: str, limit: int) -> bytes:
    if not re.fullmatch(
        r"(?:LICENSE|plugins/[a-z][a-z0-9_-]{0,31}/(?:config.json|adapter.zip))", name
    ):
        raise ValueError("invalid plugin path")
    path = root / name
    if any(p.is_symlink() for p in [root, path, *path.parents]):
        raise ValueError("plugin symlinks are forbidden")
    if not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("invalid plugin digest")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("plugin file missing or too large")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("plugin checksum mismatch")
    return data


def validate_config(data: dict, venue: str, version: str) -> dict:
    expected = {
        "schema",
        "protocol",
        "version",
        "venue",
        "display_name",
        "base_url",
        "legacy_keys",
        "private_read_timeout",
        "time_sync_budget_ms",
        "recv_window_ms",
        "timestamp_error_codes",
        "symbols_error_codes",
        "taker_fee",
        "credential_fields",
        "settings",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("invalid plugin configuration fields")
    if not isinstance(data["settings"], dict):
        raise ValueError("plugin settings must be an object")

    def public_only(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if any(
                    token in key.lower().replace("-", "_")
                    for token in (
                        "api_key",
                        "api_secret",
                        "password",
                        "passphrase",
                        "token",
                    )
                ):
                    raise ValueError(
                        "credentials do not belong in public configuration"
                    )
                public_only(item)
        elif isinstance(value, list):
            for item in value:
                public_only(item)

    public_only(data["settings"])
    url = urlparse(data["base_url"])
    fee = Decimal(data["taker_fee"])
    if (
        data["schema"] != 1
        or data["protocol"] != "p2pirate-spot-v1"
        or data["venue"] != venue
        or data["version"] != version
        or not re.fullmatch("[A-Z][A-Z0-9_]{0,31}", venue)
        or not isinstance(data["display_name"], str)
        or not 1 <= len(data["display_name"]) <= 64
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.hostname in ("localhost", "127.0.0.1", "::1")
        or type(data["legacy_keys"]) is not bool
        or data["legacy_keys"] != (venue == "MEXC")
        or type(data["private_read_timeout"]) not in (int, float)
        or not 0 < data["private_read_timeout"] <= 10
        or type(data["time_sync_budget_ms"]) is not int
        or not 0 < data["time_sync_budget_ms"] <= 12000
        or type(data["recv_window_ms"]) is not int
        or not 0 < data["recv_window_ms"] <= 60000
        or not fee.is_finite()
        or not 0 <= fee < 1
        or data["credential_fields"] != ["api_key", "api_secret"]
        or any(
            not isinstance(data[key], list)
            or len(data[key]) > 32
            or any(not isinstance(v, str) or len(v) > 64 for v in data[key])
            for key in ("timestamp_error_codes", "symbols_error_codes")
        )
    ):
        raise ValueError("incompatible Spot plugin configuration")
    return data


def verify_catalog(root: Path) -> dict[str, dict]:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("plugin directory must be absolute and not a symlink")
    manifest = root / "catalog.json"
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or manifest.stat().st_size > 65536
    ):
        raise ValueError("plugin catalog missing")
    data = json.loads(manifest.read_text())
    if (
        set(data) != {"schema", "protocol", "license_sha256", "plugins"}
        or data["schema"] != 1
        or data["protocol"] != PROTOCOL
    ):
        raise ValueError("incompatible plugin protocol")
    entries = data["plugins"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("empty or oversized plugin catalog")
    read_file(root, "LICENSE", data["license_sha256"], 65536)
    result = {}
    for item in entries:
        if set(item) != {
            "venue",
            "version",
            "config",
            "adapter",
            "config_sha256",
            "adapter_sha256",
        }:
            raise ValueError("invalid plugin manifest fields")
        venue, version = item["venue"], item["version"]
        if (
            venue in result
            or not re.fullmatch("[A-Z][A-Z0-9_]{0,31}", venue)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        ):
            raise ValueError("duplicate or invalid plugin identity")
        slug = venue.lower().replace("_", "-")
        if (
            item["config"] != f"plugins/{slug}/config.json"
            or item["adapter"] != f"plugins/{slug}/adapter.zip"
        ):
            raise ValueError("plugin path does not match venue")
        config = validate_config(
            json.loads(read_file(root, item["config"], item["config_sha256"], 16384)),
            venue,
            version,
        )
        read_file(root, item["adapter"], item["adapter_sha256"], 1024 * 1024)
        with ZipFile(root / item["adapter"]) as bundle:
            members = bundle.infolist()
            if (
                len(members) > 64
                or sum(m.file_size for m in members) > 3 * 1024 * 1024
                or len({m.filename for m in members}) != len(members)
                or "cex_plugin/adapter.py" not in {m.filename for m in members}
                or any(
                    not re.fullmatch(r"cex_plugin/[a-z][a-z0-9_]*\.py", m.filename)
                    and m.filename != "cex_plugin/__init__.py"
                    for m in members
                )
                or any(stat.S_ISLNK(m.external_attr >> 16) for m in members)
            ):
                raise ValueError("invalid plugin source bundle")
        result[venue] = {**item, "configuration": config}
    return result


def configure_plugins(directory: str | None, *, state_dir: str | None = None):
    global _snapshot, _root, _state_root
    if directory is None:
        _snapshot, _root, _state_root = None, None, None
        return
    root = Path(directory)
    entries = verify_catalog(root)  # No fallback on an invalid external catalog.
    state = Path(state_dir) if state_dir is not None else None
    if state is not None and not state.is_absolute():
        raise ValueError("plugin state directory must be absolute")
    _snapshot, _root, _state_root = entries, root, state


def installed_plugins():
    return _snapshot


def plugin_directory():
    return _root


def plugin_state_directory(venue):
    if _state_root is None:
        return None
    path = _state_root / "exchange-state" / venue.lower()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("plugin state symlinks are forbidden")
    # The wallet owns the parent; create both plugin directories privately.
    path.parent.mkdir(exist_ok=True, mode=0o700)
    path.mkdir(exist_ok=True, mode=0o700)
    if any(p.stat().st_mode & 0o077 for p in (path.parent, path)):
        raise ValueError("plugin state directory must be private")
    return str(path)
