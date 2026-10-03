"""Exchange boundary: configuration plus adapters, never strategy policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Protocol, Mapping, Any
from .plugin_catalog import installed_plugins, configure_plugins


class SpotExchange(Protocol):
    def synchronize_time(self, *, max_round_trip_ms: int) -> Any: ...
    def account(self, *, timeout: float, total_timeout: float) -> Mapping[str, Any]: ...
    def self_symbols(
        self, *, timeout: float, total_timeout: float
    ) -> Mapping[str, Any]: ...
    def symbol_rules(self, symbol: str, **kwargs) -> Any: ...
    def order_book(self, symbol: str, **kwargs) -> Any: ...


@dataclass(frozen=True)
class ExchangeConfig:
    venue: str
    adapter: str
    base_url: str
    legacy_keys: bool
    private_read_timeout: float
    time_sync_budget_ms: int
    recv_window_ms: int
    timestamp_error_codes: tuple[str, ...]
    symbols_error_codes: tuple[str, ...]
    taker_fee: str = "0.001"


def supported_venues():
    if installed_plugins() is not None:
        return tuple(sorted(installed_plugins()))
    return tuple(
        sorted(
            path.name[:-5].upper()
            for path in files(__package__).joinpath("config").iterdir()
            if path.name.endswith(".json")
        )
    )


def load_config(venue: str) -> ExchangeConfig:
    name = str(venue).strip().upper()
    if name not in supported_venues():
        raise ValueError(f"CEX non supportato: {name}")
    if installed_plugins() is not None:
        data = installed_plugins()[name]["configuration"]
        return ExchangeConfig(
            venue=name,
            adapter="p2pirate-spot-v1",
            base_url=data["base_url"],
            legacy_keys=data["legacy_keys"],
            private_read_timeout=data["private_read_timeout"],
            time_sync_budget_ms=data["time_sync_budget_ms"],
            recv_window_ms=data["recv_window_ms"],
            timestamp_error_codes=tuple(data["timestamp_error_codes"]),
            symbols_error_codes=tuple(data["symbols_error_codes"]),
            taker_fee=data["taker_fee"],
        )
    data = json.loads(
        files(__package__).joinpath("config", name.lower() + ".json").read_text()
    )
    config = ExchangeConfig(
        **{
            **data,
            "timestamp_error_codes": tuple(data["timestamp_error_codes"]),
            "symbols_error_codes": tuple(data["symbols_error_codes"]),
        }
    )
    if (
        config.venue != name
        or config.adapter not in ("mexc_spot_v3", "gate_spot_v4")
        or not config.base_url.startswith("https://")
        or not 0 < config.private_read_timeout <= 10
        or not 0 < config.time_sync_budget_ms <= 12000
        or not 0 < config.recv_window_ms <= 60000
    ):
        raise ValueError("invalid exchange configuration")
    return config


def create_client(venue: str, *, base_url=None, **kwargs) -> SpotExchange:
    config = load_config(venue)
    if installed_plugins() is not None:
        from .plugin_client import client_for

        if set(kwargs) - {"api_key", "api_secret", "trading_enabled", "timeout"}:
            raise ValueError("unsupported plugin client option")
        # External plugins own API endpoints. Legacy Settings overrides do not apply.
        return client_for(installed_plugins()[config.venue], **kwargs)
    if config.adapter == "mexc_spot_v3":
        from ..mexc import MexcClient

        kwargs.setdefault("recv_window_ms", config.recv_window_ms)
        cls = MexcClient
    else:
        from ..gate import GateClient

        cls = GateClient
    return cls(base_url=base_url or config.base_url, **kwargs)


def private_client(venue: str, keyring, *, base_url=None, trading_enabled=False):
    # Credentials stay in the wallet keyring; the adapter owns signing.
    credentials = keyring.load(venue)
    return create_client(
        venue,
        base_url=base_url,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        trading_enabled=trading_enabled,
    )
