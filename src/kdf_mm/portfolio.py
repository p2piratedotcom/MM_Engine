from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Mapping


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    kdf_arrr_target_fraction: Decimal = Decimal("0.5")
    kdf_usdt_target_fraction: Decimal = Decimal("0.5")
    rebalance_minimum_usdt: Decimal = Decimal("5")
    cex_taker_fee: Decimal = Decimal("0.001")

    def __post_init__(self) -> None:
        for value, name in (
            (self.kdf_arrr_target_fraction, "KDF ARRR target"),
            (self.kdf_usdt_target_fraction, "KDF USDT target"),
        ):
            if value < ZERO or value > ONE:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.rebalance_minimum_usdt < ZERO:
            raise ValueError("rebalance minimum cannot be negative")
        if self.cex_taker_fee < ZERO or self.cex_taker_fee >= ONE:
            raise ValueError("CEX fee must be in [0, 1)")


def build_portfolio_snapshot(
    *,
    wallet: Mapping[str, Any],
    mexc: Mapping[str, Any],
    markets: Mapping[str, Any],
    policy: PortfolioPolicy,
    base_ticker: str = "ARRR",
    kdf_quote_ticker: str = "USDT-BEP20",
    cex_base_asset: str | None = None,
    cex_quote_asset: str = "USDT",
    primary_market_id: str | None = None,
    primary_symbol: str | None = None,
) -> dict[str, Any]:
    base_ticker = base_ticker.strip().upper()
    kdf_quote_ticker = kdf_quote_ticker.strip().upper()
    cex_base_asset = (cex_base_asset or base_ticker.split("-", 1)[0]).strip().upper()
    cex_quote_asset = cex_quote_asset.strip().upper()
    primary_market_id = primary_market_id or f"{base_ticker}-{kdf_quote_ticker}"
    primary_symbol = primary_symbol or f"{cex_base_asset}{cex_quote_asset}"
    result = _empty_payload(
        policy,
        base_ticker=base_ticker,
        kdf_quote_ticker=kdf_quote_ticker,
        cex_base_asset=cex_base_asset,
        cex_quote_asset=cex_quote_asset,
    )
    warnings: list[str] = []
    try:
        kdf = _kdf_balances(wallet, base_ticker, kdf_quote_ticker)
        cex = _mexc_balances(mexc, cex_base_asset, cex_quote_asset)
        bid, ask, midpoint, price_source = _base_price(
            markets,
            mexc,
            primary_market_id=primary_market_id,
            primary_symbol=primary_symbol,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        result["reason"] = f"dati di portafoglio non validi: {str(exc)[:240]}"
        result["warnings"] = [result["reason"]]
        return result

    result["price"] = {
        "available": midpoint is not None,
        "source": price_source,
        "best_bid": _text(bid),
        "best_ask": _text(ask),
        "midpoint": _text(midpoint),
    }
    result["venues"] = {
        "kdf": _valued_venue(kdf, midpoint),
        "mexc": _valued_venue(cex, midpoint),
    }

    if not kdf["available"]:
        warnings.append("portafoglio KDF non disponibile")
    if not cex["available"]:
        warnings.append("portafoglio MEXC Spot non disponibile")
    if midpoint is None or bid is None or ask is None:
        warnings.append(
            f"prezzo {cex_base_asset}/{cex_quote_asset} non disponibile"
        )
    if mexc.get("symbol_allowed") is False:
        warnings.append(f"la chiave MEXC non consente {primary_symbol}")

    if warnings:
        result["reason"] = "; ".join(warnings)
        result["warnings"] = warnings
        return result

    kdf_arrr = kdf["arrr"]
    kdf_usdt = kdf["usdt"]
    mexc_arrr = cex["arrr"]
    mexc_usdt = cex["usdt"]
    assert isinstance(kdf_arrr, Decimal)
    assert isinstance(kdf_usdt, Decimal)
    assert isinstance(mexc_arrr, Decimal)
    assert isinstance(mexc_usdt, Decimal)
    assert midpoint is not None and bid is not None and ask is not None

    total_arrr = kdf_arrr + mexc_arrr
    total_usdt = kdf_usdt + mexc_usdt
    arrr_value = total_arrr * midpoint
    total_value = total_usdt + arrr_value
    kdf_value = kdf_usdt + kdf_arrr * midpoint
    mexc_value = mexc_usdt + mexc_arrr * midpoint
    result["totals"] = {
        "arrr": _text(total_arrr),
        "usdt": _text(total_usdt),
        "arrr_value_usdt": _text(arrr_value),
        "estimated_value_usdt": _text(total_value),
        "kdf_value_usdt": _text(kdf_value),
        "mexc_value_usdt": _text(mexc_value),
        "kdf_value_fraction": _fraction(kdf_value, total_value),
        "arrr_value_fraction": _fraction(arrr_value, total_value),
    }

    fee_multiplier = ONE + policy.cex_taker_fee
    sell_capacity = min(kdf_arrr, mexc_usdt / (ask * fee_multiplier))
    buy_capacity = min(mexc_arrr, kdf_usdt / ask)
    result["coverage"] = {
        "sell_arrr": _text(max(ZERO, sell_capacity)),
        "buy_arrr": _text(max(ZERO, buy_capacity)),
        "basis": "saldi liberi e best ask; prima di riserve KDF e limiti di mercato",
    }

    target_kdf_arrr = total_arrr * policy.kdf_arrr_target_fraction
    target_kdf_usdt = total_usdt * policy.kdf_usdt_target_fraction
    arrr_delta = target_kdf_arrr - kdf_arrr
    usdt_delta = target_kdf_usdt - kdf_usdt
    suggestions = []
    arrr_delta_value = abs(arrr_delta) * midpoint
    if arrr_delta_value >= policy.rebalance_minimum_usdt and arrr_delta != ZERO:
        suggestions.append(
            _suggestion(
                asset=base_ticker,
                delta_to_kdf=arrr_delta,
                estimated_value_usdt=arrr_delta_value,
                reason=f"riallineamento {base_ticker} verso l'obiettivo KDF",
            )
        )
    if abs(usdt_delta) >= policy.rebalance_minimum_usdt and usdt_delta != ZERO:
        suggestions.append(
            _suggestion(
                asset=kdf_quote_ticker,
                delta_to_kdf=usdt_delta,
                estimated_value_usdt=abs(usdt_delta),
                reason=f"riallineamento {kdf_quote_ticker} verso l'obiettivo KDF",
            )
        )
    result["rebalance"] = {
        "target_kdf_arrr": _text(target_kdf_arrr),
        "target_kdf_usdt": _text(target_kdf_usdt),
        "arrr_delta_to_kdf": _text(arrr_delta),
        "usdt_delta_to_kdf": _text(usdt_delta),
        "balanced_within_threshold": not suggestions,
        "suggestions": suggestions,
        "executable": False,
        "notice": (
            "indicazione matematica: rete, fee, minimi, indirizzi e gas BNB "
            "devono essere verificati prima di qualsiasi trasferimento"
        ),
    }
    result["available"] = True
    result["reason"] = None
    return result


def failed_portfolio_snapshot(exc: Exception, policy: PortfolioPolicy) -> dict[str, Any]:
    result = _empty_payload(policy)
    result["reason"] = f"calcolo portafoglio non riuscito: {str(exc)[:240]}"
    result["warnings"] = [result["reason"]]
    return result


def _empty_payload(
    policy: PortfolioPolicy,
    *,
    base_ticker: str = "ARRR",
    kdf_quote_ticker: str = "USDT-BEP20",
    cex_base_asset: str = "ARRR",
    cex_quote_asset: str = "USDT",
) -> dict[str, Any]:
    return {
        "assets": {
            "base_ticker": base_ticker,
            "kdf_quote_ticker": kdf_quote_ticker,
            "cex_base_asset": cex_base_asset,
            "cex_quote_asset": cex_quote_asset,
        },
        "available": False,
        "reason": "dati KDF, MEXC e prezzo non ancora disponibili",
        "warnings": [],
        "price": {
            "available": False,
            "source": None,
            "best_bid": None,
            "best_ask": None,
            "midpoint": None,
        },
        "venues": {
            "kdf": _valued_venue(_zero_venue(False), None),
            "mexc": _valued_venue(_zero_venue(False), None),
        },
        "totals": {
            "arrr": None,
            "usdt": None,
            "arrr_value_usdt": None,
            "estimated_value_usdt": None,
            "kdf_value_usdt": None,
            "mexc_value_usdt": None,
            "kdf_value_fraction": None,
            "arrr_value_fraction": None,
        },
        "coverage": {
            "sell_arrr": None,
            "buy_arrr": None,
            "basis": None,
        },
        "policy": {
            "kdf_arrr_target_fraction": _text(policy.kdf_arrr_target_fraction),
            "kdf_usdt_target_fraction": _text(policy.kdf_usdt_target_fraction),
            "rebalance_minimum_usdt": _text(policy.rebalance_minimum_usdt),
        },
        "rebalance": {
            "target_kdf_arrr": None,
            "target_kdf_usdt": None,
            "arrr_delta_to_kdf": None,
            "usdt_delta_to_kdf": None,
            "balanced_within_threshold": False,
            "suggestions": [],
            "executable": False,
            "notice": "nessun trasferimento automatico",
        },
        "pnl": {
            "available": False,
            "reason": "costo storico e commissioni non ancora registrati",
        },
    }


def _kdf_balances(
    wallet: Mapping[str, Any], base_ticker: str, quote_ticker: str
) -> dict[str, Any]:
    balances = wallet.get("balances", {})
    if not isinstance(balances, Mapping):
        raise ValueError("saldi KDF non strutturati")
    arrr = balances.get(base_ticker, {})
    usdt = balances.get(quote_ticker, {})
    available = (
        isinstance(arrr, Mapping)
        and isinstance(usdt, Mapping)
        and arrr.get("available") is True
        and usdt.get("available") is True
    )
    if not available:
        return _zero_venue(False)
    return {
        "available": True,
        "arrr": _non_negative(arrr.get("balance", "0"), f"saldo KDF {base_ticker}"),
        "usdt": _non_negative(usdt.get("balance", "0"), f"saldo KDF {quote_ticker}"),
    }


def _mexc_balances(
    mexc: Mapping[str, Any], base_asset: str, quote_asset: str
) -> dict[str, Any]:
    if mexc.get("available") is not True:
        return _zero_venue(False)
    balances = mexc.get("balances", {})
    if not isinstance(balances, Mapping):
        raise ValueError("saldi MEXC non strutturati")
    arrr = balances.get(base_asset, {})
    usdt = balances.get(quote_asset, {})
    if not isinstance(arrr, Mapping) or not isinstance(usdt, Mapping):
        raise ValueError(f"saldi MEXC {base_asset}/{quote_asset} mancanti")
    return {
        "available": True,
        "arrr": _non_negative(arrr.get("free", "0"), f"saldo MEXC {base_asset}"),
        "usdt": _non_negative(usdt.get("free", "0"), f"saldo MEXC {quote_asset}"),
    }


def _base_price(
    markets: Mapping[str, Any],
    mexc: Mapping[str, Any],
    *,
    primary_market_id: str,
    primary_symbol: str,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None]:
    market = mexc.get("market", {})
    if isinstance(market, Mapping) and market.get("available") is True:
        bid = _positive(market.get("best_bid"), "MEXC best bid")
        ask = _positive(market.get("best_ask"), "MEXC best ask")
        return bid, ask, (bid + ask) / Decimal("2"), f"MEXC {primary_symbol}"
    items = markets.get("markets", {})
    if isinstance(items, Mapping):
        market = items.get(primary_market_id, {})
        if isinstance(market, Mapping) and market.get("best_bid") is not None:
            bid = _positive(market.get("best_bid"), "KDF feed best bid")
            ask = _positive(market.get("best_ask"), "KDF feed best ask")
            return bid, ask, (bid + ask) / Decimal("2"), f"VPS feed {primary_symbol}"
    return None, None, None, None


def _zero_venue(available: bool) -> dict[str, Any]:
    return {"available": available, "arrr": ZERO, "usdt": ZERO}


def _valued_venue(venue: Mapping[str, Any], midpoint: Decimal | None) -> dict[str, Any]:
    arrr = venue.get("arrr", ZERO)
    usdt = venue.get("usdt", ZERO)
    value = usdt + arrr * midpoint if midpoint is not None else None
    return {
        "available": bool(venue.get("available")),
        "arrr": _text(arrr),
        "usdt": _text(usdt),
        "arrr_value_usdt": _text(arrr * midpoint) if midpoint is not None else None,
        "estimated_value_usdt": _text(value),
    }


def _suggestion(
    *,
    asset: str,
    delta_to_kdf: Decimal,
    estimated_value_usdt: Decimal,
    reason: str,
) -> dict[str, Any]:
    return {
        "asset": asset,
        "direction": "MEXC_TO_KDF" if delta_to_kdf > ZERO else "KDF_TO_MEXC",
        "amount": _text(abs(delta_to_kdf)),
        "estimated_value_usdt": _text(estimated_value_usdt),
        "reason": reason,
        "executable": False,
    }


def _fraction(numerator: Decimal, denominator: Decimal) -> str | None:
    return _text(numerator / denominator) if denominator > ZERO else None


def _non_negative(value: Any, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed < ZERO:
        raise ValueError(f"{name} negativo")
    return parsed


def _positive(value: Any, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed <= ZERO:
        raise ValueError(f"{name} non positivo")
    return parsed


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} non valido") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} non valido")
    return parsed


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rounded = value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    rendered = format(rounded, "f").rstrip("0").rstrip(".")
    return rendered or "0"
