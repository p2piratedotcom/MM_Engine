from __future__ import annotations

from .exchanges import load_config, supported_venues


SUPPORTED_CEX = supported_venues()


def normalize_cex(value: object) -> str:
    venue = str(value or "MEXC").strip().upper()
    if venue not in SUPPORTED_CEX:
        raise ValueError(f"CEX non supportato: {venue or '(vuoto)'}")
    return venue


def market_data_key(cex: object, symbol: str) -> str:
    """Keep historical MEXC keys stable and namespace every other venue."""
    venue = normalize_cex(cex)
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("simbolo CEX richiesto")
    return normalized if load_config(venue).legacy_keys else f"{venue}:{normalized}"


def coverage_asset_key(cex: object, asset: str) -> str:
    venue = normalize_cex(cex)
    normalized = asset.strip().upper()
    if not normalized:
        raise ValueError("asset CEX richiesto")
    return normalized if load_config(venue).legacy_keys else f"{venue}:{normalized}"
