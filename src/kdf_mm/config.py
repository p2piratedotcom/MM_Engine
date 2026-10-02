from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping


def _boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _markets(
    value: str | None, *, base_ticker: str = "ARRR", quote_ticker: str = "USDT-BEP20"
) -> tuple[str, ...]:
    if value is None:
        raw = (
            "ARRR-USDT-BEP20,ARRR-LTC"
            if base_ticker == "ARRR" and quote_ticker == "USDT-BEP20"
            else f"{base_ticker}-{quote_ticker}"
        )
    else:
        raw = value
    result = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    if not result or len(set(result)) != len(result):
        raise ValueError("KDF_MM_MARKETS must contain unique comma-separated markets")
    return result


def _symbol_map(value: str | None) -> dict[str, str]:
    """Parse optional KDF-ticker -> MEXC-symbol overrides."""
    if value is None or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("KDF_MM_MEXC_QUOTE_SYMBOLS must be a JSON object") from exc
    if not isinstance(loaded, dict):
        raise ValueError("KDF_MM_MEXC_QUOTE_SYMBOLS must be a JSON object")
    result: dict[str, str] = {}
    for ticker, symbol in loaded.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("MEXC quote-symbol tickers must be non-empty strings")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("MEXC quote symbols must be non-empty strings")
        result[ticker.strip().upper()] = symbol.strip().upper()
    return result


def _fraction(value: str | None, *, default: str, name: str) -> Decimal:
    parsed = Decimal(value if value is not None else default)
    if parsed < 0 or parsed > 1:
        raise ValueError(f"{name} must be in [0, 1]")
    return parsed


def _non_negative_decimal(value: str | None, *, default: str, name: str) -> Decimal:
    parsed = Decimal(value if value is not None else default)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    base_ticker: str = "ARRR"
    pair: str = "ARRRUSDT"
    mexc_base_asset: str = "ARRR"
    mexc_quote_asset: str = "USDT"
    kdf_quote_ticker: str = "USDT-BEP20"
    markets: tuple[str, ...] = ("ARRR-USDT-BEP20", "ARRR-LTC")
    mexc_quote_symbols: Mapping[str, str] = field(default_factory=dict)
    kdf_rpc_url: str = "http://127.0.0.1:7783"
    kdf_rpc_userpass: str = field(default="", repr=False)
    kdf_binary_manifest: str = (
        "vendor/kdf/official-v2.6.0-beta-475cdb49/manifest.json"
    )
    kdf_config_path: str = "runtime/MM2.json"
    kdf_coins_path: str = (
        "vendor/coins/519e6a345c1e99542b705dd2489364ef3d4f767f-arrr-b5e6ae1b/coins"
    )
    kdf_coins_manifest: str = "vendor/coins/full-manifest.json"
    coin_profile_path: str = "runtime/coin-profile.json"
    mexc_base_url: str = "https://api.mexc.com"
    mexc_api_key: str = field(default="", repr=False)
    mexc_api_secret: str = field(default="", repr=False)
    gate_base_url: str = "https://api.gateio.ws/api/v4"
    gate_api_key: str = field(default="", repr=False)
    gate_api_secret: str = field(default="", repr=False)
    live_trading: bool = False
    live_transfers: bool = False
    auto_hedge: bool = False
    mexc_coverage: bool = False
    coverage_lease_ttl_seconds: float = 10.0
    coverage_override_seconds: int = 300
    coverage_audit_db: str = "var/coverage-audit.sqlite3"
    auto_hedge_max_attempts: int = 3
    kdf_order_writes: bool = False
    agent_bind: str = "127.0.0.1"
    agent_port: int = 8765
    agent_token: str = field(default="", repr=False)
    snapshot_secret: str = field(default="", repr=False)
    event_secret: str = field(default="", repr=False)
    desktop_agent_url: str = "http://127.0.0.1:8765"
    desktop_journal_db: str = "var/desktop-agent.sqlite3"
    desktop_consumer_id: str = "zorin-desktop"
    desktop_poll_interval_seconds: float = 3.0
    market_data_max_age_ms: int = 10_000
    mexc_public_feed: bool = False
    mexc_feed_interval_seconds: float = 3.0
    mexc_depth_limit: int = 100
    state_db: str = "var/vps-agent.sqlite3"
    outbox_db: str = "var/vps-outbox.sqlite3"
    manage_kdf: bool = False
    kdf_supervisor_state: str = "var/kdf-supervisor.json"
    kdf_log_path: str = "var/kdf.log"
    kdf_start_timeout_seconds: float = 30.0
    repricing_poll_interval_seconds: float = 3.0
    repricing_min_price_change: Decimal = Decimal("0.0025")
    repricing_min_update_interval_seconds: float = 15.0
    repricing_state_path: str = "var/repricing-state.json"
    reconciliation_interval_seconds: float = 2.0
    reconciliation_recent_swap_limit: int = 100
    kdf_event_stream: bool = False
    kdf_event_stream_client_id: int = 4107
    premium: Decimal = Decimal("0.02")
    cex_taker_fee: Decimal = Decimal("0.001")
    gate_taker_fee: Decimal = Decimal("0.001")
    risk_buffer: Decimal = Decimal("0.005")
    max_slippage: Decimal = Decimal("0.01")
    max_daily_volume_fraction: Decimal = Decimal("0.005")
    portfolio_kdf_arrr_target_fraction: Decimal = Decimal("0.5")
    portfolio_kdf_usdt_target_fraction: Decimal = Decimal("0.5")
    portfolio_rebalance_minimum_usdt: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if any(not fee.is_finite() or fee < 0 or fee >= 1
               for fee in (self.cex_taker_fee, self.gate_taker_fee)):
            raise ValueError("CEX taker fees must be finite and in [0, 1)")
        if (
            self.coverage_lease_ttl_seconds <= 0
            or self.coverage_lease_ttl_seconds > 30
        ):
            raise ValueError("coverage lease TTL must be in (0, 30] seconds")
        if (
            self.coverage_override_seconds <= 0
            or self.coverage_override_seconds > 300
        ):
            raise ValueError("coverage override must be in [1, 300] seconds")
        if (
            self.auto_hedge_max_attempts <= 0
            or self.auto_hedge_max_attempts > 10
        ):
            raise ValueError("automatic hedge attempts must be in [1, 10]")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        base_ticker = source.get("KDF_MM_BASE_TICKER", "ARRR").strip().upper()
        quote_ticker = source.get(
            "KDF_MM_KDF_QUOTE_TICKER", "USDT-BEP20"
        ).strip().upper()
        if not base_ticker or not quote_ticker or base_ticker == quote_ticker:
            raise ValueError("base and primary quote tickers must be different")
        default_mexc_base = base_ticker.split("-", 1)[0]
        pair = source.get("KDF_MM_PAIR", f"{default_mexc_base}USDT").strip().upper()
        mexc_base_asset = source.get(
            "KDF_MM_MEXC_BASE_ASSET", default_mexc_base
        ).strip().upper()
        mexc_quote_asset = source.get(
            "KDF_MM_MEXC_QUOTE_ASSET", "USDT"
        ).strip().upper()
        if not pair or not mexc_base_asset or not mexc_quote_asset:
            raise ValueError("MEXC symbol and assets must be non-empty")
        return cls(
            base_ticker=base_ticker,
            pair=pair,
            mexc_base_asset=mexc_base_asset,
            mexc_quote_asset=mexc_quote_asset,
            kdf_quote_ticker=quote_ticker,
            markets=_markets(
                source.get("KDF_MM_MARKETS"),
                base_ticker=base_ticker,
                quote_ticker=quote_ticker,
            ),
            mexc_quote_symbols=_symbol_map(
                source.get("KDF_MM_MEXC_QUOTE_SYMBOLS")
            ),
            kdf_rpc_url=source.get("KDF_RPC_URL", "http://127.0.0.1:7783"),
            kdf_rpc_userpass=source.get("KDF_RPC_USERPASS", ""),
            kdf_binary_manifest=source.get(
                "KDF_BINARY_MANIFEST",
                "vendor/kdf/official-v2.6.0-beta-475cdb49/manifest.json",
            ),
            kdf_config_path=source.get("KDF_CONFIG_PATH", "runtime/MM2.json"),
            kdf_coins_path=source.get(
                "KDF_COINS_PATH",
                "vendor/coins/519e6a345c1e99542b705dd2489364ef3d4f767f-arrr-b5e6ae1b/coins",
            ),
            kdf_coins_manifest=source.get(
                "KDF_COINS_MANIFEST", "vendor/coins/full-manifest.json"
            ),
            coin_profile_path=source.get(
                "KDF_MM_COIN_PROFILE", "runtime/coin-profile.json"
            ),
            mexc_base_url=source.get("MEXC_BASE_URL", "https://api.mexc.com"),
            mexc_api_key=source.get("MEXC_API_KEY", ""),
            mexc_api_secret=source.get("MEXC_API_SECRET", ""),
            gate_base_url=source.get("GATE_BASE_URL", "https://api.gateio.ws/api/v4"),
            gate_api_key=source.get("GATE_API_KEY", ""),
            gate_api_secret=source.get("GATE_API_SECRET", ""),
            live_trading=_boolean(source.get("KDF_MM_LIVE_TRADING")),
            live_transfers=_boolean(source.get("KDF_MM_LIVE_TRANSFERS")),
            auto_hedge=_boolean(source.get("KDF_MM_AUTO_HEDGE")),
            mexc_coverage=_boolean(source.get("KDF_MM_MEXC_COVERAGE")),
            coverage_lease_ttl_seconds=float(
                source.get("KDF_MM_COVERAGE_LEASE_TTL_SECONDS", "10")
            ),
            coverage_override_seconds=int(
                source.get("KDF_MM_COVERAGE_OVERRIDE_SECONDS", "300")
            ),
            coverage_audit_db=source.get(
                "KDF_MM_COVERAGE_AUDIT_DB", "var/coverage-audit.sqlite3"
            ),
            auto_hedge_max_attempts=int(
                source.get("KDF_MM_AUTO_HEDGE_MAX_ATTEMPTS", "3")
            ),
            kdf_order_writes=_boolean(source.get("KDF_MM_KDF_ORDER_WRITES")),
            agent_bind=source.get("KDF_MM_AGENT_BIND", "127.0.0.1"),
            agent_port=int(source.get("KDF_MM_AGENT_PORT", "8765")),
            agent_token=source.get("KDF_MM_AGENT_TOKEN", ""),
            snapshot_secret=source.get("KDF_MM_SNAPSHOT_SECRET", ""),
            event_secret=source.get("KDF_MM_EVENT_SECRET", ""),
            desktop_agent_url=source.get(
                "KDF_MM_DESKTOP_AGENT_URL", "http://127.0.0.1:8765"
            ),
            desktop_journal_db=source.get(
                "KDF_MM_DESKTOP_JOURNAL_DB", "var/desktop-agent.sqlite3"
            ),
            desktop_consumer_id=source.get(
                "KDF_MM_DESKTOP_CONSUMER_ID", "zorin-desktop"
            ),
            desktop_poll_interval_seconds=float(
                source.get("KDF_MM_DESKTOP_POLL_INTERVAL_SECONDS", "3")
            ),
            market_data_max_age_ms=int(
                source.get("KDF_MM_MARKET_DATA_MAX_AGE_MS", "10000")
            ),
            mexc_public_feed=_boolean(source.get("KDF_MM_MEXC_PUBLIC_FEED")),
            mexc_feed_interval_seconds=float(
                source.get("KDF_MM_MEXC_FEED_INTERVAL_SECONDS", "3")
            ),
            mexc_depth_limit=int(source.get("KDF_MM_MEXC_DEPTH_LIMIT", "100")),
            state_db=source.get("KDF_MM_STATE_DB", "var/vps-agent.sqlite3"),
            outbox_db=source.get("KDF_MM_OUTBOX_DB", "var/vps-outbox.sqlite3"),
            manage_kdf=_boolean(source.get("KDF_MM_MANAGE_KDF")),
            kdf_supervisor_state=source.get(
                "KDF_MM_KDF_SUPERVISOR_STATE", "var/kdf-supervisor.json"
            ),
            kdf_log_path=source.get("KDF_MM_KDF_LOG_PATH", "var/kdf.log"),
            kdf_start_timeout_seconds=float(
                source.get("KDF_MM_KDF_START_TIMEOUT_SECONDS", "30")
            ),
            repricing_poll_interval_seconds=float(
                source.get("KDF_MM_REPRICING_POLL_INTERVAL_SECONDS", "3")
            ),
            repricing_min_price_change=Decimal(
                source.get("KDF_MM_REPRICING_MIN_PRICE_CHANGE", "0.0025")
            ),
            repricing_min_update_interval_seconds=float(
                source.get("KDF_MM_REPRICING_MIN_UPDATE_INTERVAL_SECONDS", "15")
            ),
            repricing_state_path=source.get(
                "KDF_MM_REPRICING_STATE", "var/repricing-state.json"
            ),
            reconciliation_interval_seconds=float(
                source.get("KDF_MM_RECONCILIATION_INTERVAL_SECONDS", "2")
            ),
            reconciliation_recent_swap_limit=int(
                source.get("KDF_MM_RECONCILIATION_RECENT_SWAP_LIMIT", "100")
            ),
            kdf_event_stream=_boolean(source.get("KDF_MM_KDF_EVENT_STREAM")),
            kdf_event_stream_client_id=int(
                source.get("KDF_MM_KDF_EVENT_STREAM_CLIENT_ID", "4107")
            ),
            premium=Decimal(source.get("KDF_MM_PREMIUM", "0.02")),
            cex_taker_fee=Decimal(source.get("KDF_MM_CEX_TAKER_FEE", "0.001")),
            gate_taker_fee=Decimal(source.get("KDF_MM_GATE_TAKER_FEE", "0.001")),
            risk_buffer=Decimal(source.get("KDF_MM_RISK_BUFFER", "0.005")),
            max_slippage=Decimal(source.get("KDF_MM_MAX_SLIPPAGE", "0.01")),
            max_daily_volume_fraction=Decimal(
                source.get("KDF_MM_MAX_DAILY_VOLUME_FRACTION", "0.005")
            ),
            portfolio_kdf_arrr_target_fraction=_fraction(
                source.get(
                    "KDF_MM_PORTFOLIO_KDF_BASE_TARGET_FRACTION",
                    source.get("KDF_MM_PORTFOLIO_KDF_ARRR_TARGET_FRACTION"),
                ),
                default="0.5",
                name="KDF base-asset portfolio target",
            ),
            portfolio_kdf_usdt_target_fraction=_fraction(
                source.get(
                    "KDF_MM_PORTFOLIO_KDF_QUOTE_TARGET_FRACTION",
                    source.get("KDF_MM_PORTFOLIO_KDF_USDT_TARGET_FRACTION"),
                ),
                default="0.5",
                name="KDF quote-asset portfolio target",
            ),
            portfolio_rebalance_minimum_usdt=_non_negative_decimal(
                source.get("KDF_MM_PORTFOLIO_REBALANCE_MINIMUM_USDT"),
                default="5",
                name="portfolio rebalance minimum",
            ),
        )
