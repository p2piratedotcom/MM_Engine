"""Exchange boundary: configuration plus adapters, never strategy policy."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Protocol, Mapping, Any


class SpotExchange(Protocol):
    def synchronize_time(self, *, max_round_trip_ms: int) -> Any: ...
    def account(self, *, timeout: float, total_timeout: float) -> Mapping[str, Any]: ...
    def self_symbols(self, *, timeout: float, total_timeout: float) -> Mapping[str, Any]: ...
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


def supported_venues():
    return tuple(sorted(path.name[:-5].upper() for path in
                        files(__package__).joinpath('config').iterdir()
                        if path.name.endswith('.json')))


@lru_cache(maxsize=32)
def load_config(venue: str) -> ExchangeConfig:
    name = str(venue).strip().upper()
    if name not in supported_venues():
        raise ValueError(f'CEX non supportato: {name}')
    data = json.loads(files(__package__).joinpath('config', name.lower() + '.json').read_text())
    config = ExchangeConfig(**{**data,
        'timestamp_error_codes': tuple(data['timestamp_error_codes']),
        'symbols_error_codes': tuple(data['symbols_error_codes'])})
    if (config.venue != name or config.adapter not in ('mexc_spot_v3', 'gate_spot_v4')
            or not config.base_url.startswith('https://')
            or not 0 < config.private_read_timeout <= 10
            or not 0 < config.time_sync_budget_ms <= 12000
            or not 0 < config.recv_window_ms <= 60000):
        raise ValueError('invalid exchange configuration')
    return config


def create_client(venue: str, *, base_url=None, **kwargs) -> SpotExchange:
    config = load_config(venue)
    if config.adapter == 'mexc_spot_v3':
        from ..mexc import MexcClient
        kwargs.setdefault('recv_window_ms', config.recv_window_ms)
        cls = MexcClient
    else:
        from ..gate import GateClient
        cls = GateClient
    return cls(base_url=base_url or config.base_url, **kwargs)


def private_client(venue: str, keyring, *, base_url=None, trading_enabled=False):
    # Credential storage and signing are adapter responsibilities, never JSON.
    credentials = getattr(keyring, 'load_' + venue.lower())()
    return create_client(venue, base_url=base_url,
        api_key=credentials.api_key, api_secret=credentials.api_secret,
        trading_enabled=trading_enabled)
