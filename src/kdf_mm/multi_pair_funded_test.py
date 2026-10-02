from __future__ import annotations

import concurrent.futures
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .coin_registry import CoinRegistry
from .funded_swap_test import (
    FundedSwapTestError,
    _current_balances,
    _last_event,
    _load_private_config,
    _load_public_addresses,
    _monitor_swap,
    _runtime_config,
    _start_kdf,
    _stop_kdf,
    _wait_for_order,
    _wait_for_peer,
    _wait_for_rpc,
    _write_private_json,
    _write_state,
)
from .kdf import KdfError, KdfRpcClient
from .kdf_runtime import KdfBinaryManager
from .market_data import MarketDataStore
from .markets import default_market_specs
from .models import DexSide, HedgeSide, QuotePlan
from .ownership import OrderOwnershipStore, OwnedOrder, OwnedOrderStatus
from .reconciliation import KdfReconciler, KdfReconciliationError
from .vps_controller import VpsController
from .wallet_activation import activate_and_collect


@dataclass(frozen=True, slots=True)
class MultiPairFundedTestResult:
    run_dir: str
    selective_swap_uuid: str | None
    selective_cancelled_order_uuid: str | None
    selective_success: bool | None
    concurrent_swap_uuids: tuple[str, ...]
    concurrent_started_count: int
    concurrent_failures: Mapping[str, str]
    concurrent_outcome: str
    concurrent_test_passed: bool
    concurrent_funds_safe: bool
    dual_match_observed: bool
    concurrent_successful_pairs: tuple[str, ...]
    concurrent_safely_aborted_pairs: tuple[str, ...]
    balances_before: Mapping[str, Mapping[str, str]]
    balances_after: Mapping[str, Mapping[str, str]]
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ConcurrentRaceClassification:
    outcome: str
    test_passed: bool
    completed: bool
    funds_safe: bool
    dual_match_observed: bool
    successful_pairs: tuple[str, ...]
    safely_aborted_pairs: tuple[str, ...]
    unsafe_pairs: tuple[str, ...]


def run_multi_pair_funded_test(
    *,
    binary_manifest_path: str | Path,
    coins_manifest_path: str | Path,
    coins_path: str | Path,
    maker_source_config: str | Path,
    taker_source_config: str | Path,
    maker_expected_addresses: str | Path,
    taker_expected_addresses: str | Path,
    run_dir: str | Path,
    arrr_sync_height: int,
    volume: Decimal = Decimal("0.1"),
    usdt_price: Decimal = Decimal("0.25"),
    ltc_price: Decimal = Decimal("0.006"),
    activation_timeout: float = 1800.0,
    swap_timeout: float = 10800.0,
    poll_interval: float = 5.0,
    mode: str = "full",
) -> MultiPairFundedTestResult:
    """Funded shared-inventory test with a deterministic OCO and a race."""
    if any(value <= 0 for value in (volume, usdt_price, ltc_price)):
        raise ValueError("volume and prices must be positive")
    if any(value <= 0 for value in (activation_timeout, swap_timeout, poll_interval)):
        raise ValueError("timeouts and poll interval must be positive")
    if mode not in {"full", "race-only"}:
        raise ValueError("mode must be 'full' or 'race-only'")

    manager = KdfBinaryManager(binary_manifest_path)
    verification = manager.verify()
    manifest = manager.manifest
    release = manifest.get("release")
    checks = manifest.get("verification")
    required_checks = (
        "github_asset_digest_matches",
        "zip_integrity_ok",
        "detached_signature_valid",
        "signed_checksum_valid",
    )
    if manifest.get("project") != "GLEECBTC/komodo-defi-framework":
        raise FundedSwapTestError("funded test requires the official KDF project")
    if not isinstance(release, Mapping) or release.get("tag") != "v2.6.0-beta":
        raise FundedSwapTestError("funded test requires pinned KDF v2.6.0-beta")
    if not isinstance(checks, Mapping) or not all(
        checks.get(name) is True for name in required_checks
    ):
        raise FundedSwapTestError("official KDF provenance checks are incomplete")

    maker_source = _load_private_config(Path(maker_source_config))
    taker_source = _load_private_config(Path(taker_source_config))
    if maker_source["passphrase"] == taker_source["passphrase"]:
        raise FundedSwapTestError("maker and taker must use different wallet seeds")
    if maker_source.get("netid") != 8762 or taker_source.get("netid") != 8762:
        raise FundedSwapTestError("funded test requires netid 8762")

    target = Path(run_dir).resolve()
    if target.exists():
        raise FundedSwapTestError(f"run directory already exists: {target}")
    target.mkdir(mode=0o700, parents=True)
    maker_dir = target / "maker"
    taker_dir = target / "taker"
    maker_dir.mkdir(mode=0o700)
    taker_dir.mkdir(mode=0o700)
    maker_config = _runtime_config(maker_source, maker_dir, rpc_port=17803)
    taker_config = _runtime_config(taker_source, taker_dir, rpc_port=17804)
    maker_config.update(
        {
            "i_am_seed": True,
            "is_bootstrap_node": True,
            "disable_p2p": False,
            "myipaddr": "127.0.0.1",
        }
    )
    maker_config.pop("seednodes", None)
    taker_config.update(
        {
            "i_am_seed": False,
            "is_bootstrap_node": False,
            "disable_p2p": False,
            "seednodes": ["127.0.0.1"],
        }
    )
    taker_config.pop("myipaddr", None)
    maker_config_path = maker_dir / "MM2.json"
    taker_config_path = taker_dir / "MM2.json"
    _write_private_json(maker_config_path, maker_config)
    _write_private_json(taker_config_path, taker_config)

    expected_maker = _load_public_addresses(Path(maker_expected_addresses))
    expected_taker = _load_public_addresses(Path(taker_expected_addresses))
    registry = CoinRegistry.from_manifest(coins_manifest_path)
    pinned_coins = Path(coins_path).resolve()
    maker = KdfRpcClient(
        rpc_url="http://127.0.0.1:17803",
        userpass=str(maker_config["rpc_password"]),
        orders_enabled=True,
        timeout=30.0,
    )
    taker = KdfRpcClient(
        rpc_url="http://127.0.0.1:17804",
        userpass=str(taker_config["rpc_password"]),
        timeout=30.0,
    )
    processes: dict[str, Any] = {}
    store: OrderOwnershipStore | None = None
    reconciler: KdfReconciler | None = None
    started = time.monotonic()
    active_swap_uuids: set[str] = set()
    terminal_swap_uuids: set[str] = set()
    all_order_uuids: set[str] = set()

    _write_state(
        target,
        {
            "phase": "prepared",
            "binary_sha256": verification.sha256,
            "volume_arrr": str(volume),
            "usdt_price": str(usdt_price),
            "ltc_price": str(ltc_price),
            "maximum_arrr_exposure": str(
                volume * Decimal("3" if mode == "full" else "2")
            ),
            "arrr_sync_height": arrr_sync_height,
            "mode": mode,
        },
    )

    try:
        processes["maker"] = _start_kdf(
            manager.binary_path, maker_dir, maker_config_path, pinned_coins
        )
        _wait_for_rpc(maker, processes["maker"], maker_dir / "kdf.log")
        print("Maker KDF avviata", flush=True)
        processes["taker"] = _start_kdf(
            manager.binary_path, taker_dir, taker_config_path, pinned_coins
        )
        _wait_for_rpc(taker, processes["taker"], taker_dir / "kdf.log")
        print("Taker KDF avviata", flush=True)

        maker_wallet = activate_and_collect(
            client=maker,
            registry=registry,
            timeout=activation_timeout,
            poll_interval=2.0,
            progress=lambda message: print(f"Maker: {message}", flush=True),
            arrr_sync_height=arrr_sync_height,
            include_ltc=True,
        )
        taker_wallet = activate_and_collect(
            client=taker,
            registry=registry,
            timeout=activation_timeout,
            poll_interval=2.0,
            progress=lambda message: print(f"Taker: {message}", flush=True),
            arrr_sync_height=arrr_sync_height,
            include_ltc=True,
        )
        _verify_addresses("maker", maker_wallet, expected_maker)
        _verify_addresses("taker", taker_wallet, expected_taker)
        before = {
            "maker": _wallet_balances(maker_wallet),
            "taker": _wallet_balances(taker_wallet),
        }
        _assert_funded(before, volume, usdt_price, ltc_price)
        print("Indirizzi e saldi delle quattro coin verificati", flush=True)
        _wait_for_peer(taker, timeout=60.0)
        print("Peer P2P locale verificato", flush=True)

        preimages = _preflight(
            maker=maker,
            taker=taker,
            volume=volume,
            usdt_price=usdt_price,
            ltc_price=ltc_price,
        )
        _write_state(target, {"phase": "preflight_passed", "balances_before": before,
                              "preimages": preimages})
        print("Preflight di entrambe le coppie completato", flush=True)

        store = OrderOwnershipStore(target / "agent.sqlite3")
        controller = _controller(maker, store)
        reconciler = KdfReconciler(
            kdf=maker,
            ownership=store,
            interval_seconds=1.0,
            pool_resolver=controller.inventory_pool,
            active_swap_callback=controller.cancel_sibling_orders,
        )
        reconciler.reconcile_once()

        selective_swap_uuid: str | None = None
        selective_cancelled_order_uuid: str | None = None
        selective_success: bool | None = None
        if mode == "full":
            print("Scenario 1: OCO selettivo tramite ARRR/LTC", flush=True)
            selective_orders = _publish_pair(
                maker, store, volume=volume, usdt_price=usdt_price, ltc_price=ltc_price
            )
            all_order_uuids.update(order.order_uuid for order in selective_orders.values())
            for rel, order in selective_orders.items():
                _wait_for_order(taker, order.order_uuid, timeout=90.0, rel=rel)
            selective_swap_uuid = _take(
                taker,
                rel="LTC",
                price=ltc_price,
                volume=volume,
                order_uuid=selective_orders["LTC"].order_uuid,
            )
            selective_cancelled_order_uuid = selective_orders["USDT-BEP20"].order_uuid
            active_swap_uuids.add(selective_swap_uuid)
            _wait_for_selective_oco(
                reconciler,
                store,
                matched_uuid=selective_orders["LTC"].order_uuid,
                sibling_uuid=selective_cancelled_order_uuid,
                timeout=60.0,
            )
            print("OCO: ordine ARRR/USDT-BEP20 cancellato selettivamente", flush=True)
            maker_status, taker_status = _monitor_swap(
                maker=maker,
                taker=taker,
                processes=processes,
                swap_uuid=selective_swap_uuid,
                target=target,
                timeout=swap_timeout,
                poll_interval=poll_interval,
            )
            terminal_swap_uuids.add(selective_swap_uuid)
            active_swap_uuids.discard(selective_swap_uuid)
            selective_success = (
                maker_status.get("is_success") is True
                and taker_status.get("is_success") is True
            )
            if not selective_success:
                raise FundedSwapTestError("selective ARRR/LTC swap did not succeed")
            _wait_for_terminal_reconciliation(
                reconciler, store, selective_swap_uuid, timeout=60.0
            )
            reconciler.acknowledge_quote("ARRR-LTC", DexSide.SELL_ARRR)

        print("Scenario 2: gara concorrente sulle due coppie", flush=True)
        concurrent_orders = _publish_pair(
            maker, store, volume=volume, usdt_price=usdt_price, ltc_price=ltc_price
        )
        all_order_uuids.update(order.order_uuid for order in concurrent_orders.values())
        for rel, order in concurrent_orders.items():
            _wait_for_order(taker, order.order_uuid, timeout=90.0, rel=rel)
        reconciler.start_worker()
        concurrent_swaps, concurrent_failures = _take_concurrently(
            taker,
            volume=volume,
            usdt_price=usdt_price,
            ltc_price=ltc_price,
            orders=concurrent_orders,
        )
        if not concurrent_swaps:
            raise FundedSwapTestError("neither concurrent taker request started a swap")
        active_swap_uuids.update(concurrent_swaps.values())
        reconciliation_error = _reconcile_race(
            reconciler, timeout=30.0
        )
        reconciler.stop_worker()
        race_status = {
            rel: store.get(order.order_uuid).status.value
            for rel, order in concurrent_orders.items()
        }
        if len(concurrent_swaps) == 1:
            losing_rel = next(rel for rel in concurrent_orders if rel not in concurrent_swaps)
            if race_status[losing_rel] not in {
                OwnedOrderStatus.CANCELLED.value,
                OwnedOrderStatus.INSUFFICIENT_BALANCE.value,
            }:
                raise FundedSwapTestError(
                    f"concurrent losing order {losing_rel} was not cancelled"
                )
        _write_state(
            target,
            {
                "phase": "concurrent_swaps_started",
                "concurrent_swaps": concurrent_swaps,
                "concurrent_failures": concurrent_failures,
                "race_order_status": race_status,
                "race_reconciliation_error": reconciliation_error,
            },
        )
        print(
            f"Gara: {len(concurrent_swaps)} swap avviati; "
            f"{len(concurrent_failures)} richiesta respinta",
            flush=True,
        )

        concurrent_statuses: dict[str, Any] = {}
        concurrent_raw_statuses: dict[
            str, tuple[Mapping[str, Any], Mapping[str, Any]]
        ] = {}
        for rel, swap_uuid in concurrent_swaps.items():
            maker_result, taker_result = _monitor_swap(
                maker=maker,
                taker=taker,
                processes=processes,
                swap_uuid=swap_uuid,
                target=target,
                timeout=swap_timeout,
                poll_interval=poll_interval,
            )
            active_swap_uuids.discard(swap_uuid)
            terminal_swap_uuids.add(swap_uuid)
            concurrent_raw_statuses[rel] = (maker_result, taker_result)
            maker_events = _event_types(maker_result)
            taker_events = _event_types(taker_result)
            concurrent_statuses[rel] = {
                "swap_uuid": swap_uuid,
                "maker_finished": maker_result.get("is_finished") is True,
                "taker_finished": taker_result.get("is_finished") is True,
                "maker_success": maker_result.get("is_success") is True,
                "taker_success": taker_result.get("is_success") is True,
                "maker_events": list(maker_events),
                "taker_events": list(taker_events),
                "taker_payment_sent": "TakerPaymentSent" in taker_events,
                "maker_final_event": _last_event(maker_result),
                "taker_final_event": _last_event(taker_result),
            }

        race = _classify_concurrent_race(
            concurrent_raw_statuses,
            concurrent_failures,
            order_statuses=race_status,
        )
        _write_state(
            target,
            {
                "phase": "concurrent_swaps_classified",
                "concurrent_statuses": concurrent_statuses,
                "concurrent_classification": asdict(race),
            },
        )
        if not race.test_passed:
            raise FundedSwapTestError(
                f"unsafe or incomplete concurrent outcome: {race.outcome}"
            )

        for swap_uuid in concurrent_swaps.values():
            _wait_for_terminal_reconciliation(
                reconciler, store, swap_uuid, timeout=60.0
            )
        reconciler.acknowledge_quote("ARRR-LTC", DexSide.SELL_ARRR)

        _cancel_remaining_owned(controller, store)
        after = {
            "maker": _current_balances_for(maker),
            "taker": _current_balances_for(taker),
        }
        result = MultiPairFundedTestResult(
            run_dir=str(target),
            selective_swap_uuid=selective_swap_uuid,
            selective_cancelled_order_uuid=selective_cancelled_order_uuid,
            selective_success=selective_success,
            concurrent_swap_uuids=tuple(concurrent_swaps.values()),
            concurrent_started_count=len(concurrent_swaps),
            concurrent_failures=concurrent_failures,
            concurrent_outcome=race.outcome,
            concurrent_test_passed=race.test_passed,
            concurrent_funds_safe=race.funds_safe,
            dual_match_observed=race.dual_match_observed,
            concurrent_successful_pairs=race.successful_pairs,
            concurrent_safely_aborted_pairs=race.safely_aborted_pairs,
            balances_before=before,
            balances_after=after,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        _write_state(
            target,
            {
                "phase": "finished",
                "success": True,
                "result": asdict(result),
                "concurrent_statuses": concurrent_statuses,
                "terminal_swap_uuids": sorted(terminal_swap_uuids),
            },
        )
        return result
    except BaseException as exc:
        _write_state(
            target,
            {
                "phase": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "active_swap_uuids": sorted(active_swap_uuids),
                "terminal_swap_uuids": sorted(terminal_swap_uuids),
                "kdf_left_running_for_recovery": bool(active_swap_uuids),
            },
        )
        raise
    finally:
        if reconciler is not None:
            reconciler.stop_worker()
        if store is not None:
            store.close()
        if not active_swap_uuids:
            _stop_kdf(taker, processes.get("taker"))
            _stop_kdf(maker, processes.get("maker"))


def _classify_concurrent_race(
    statuses: Mapping[
        str, tuple[Mapping[str, Any], Mapping[str, Any]]
    ],
    request_failures: Mapping[str, str],
    *,
    order_statuses: Mapping[str, str] | None = None,
) -> ConcurrentRaceClassification:
    """Classify a two-pair race without treating every safe abort as a failure.

    A failed swap is considered contained only for the exact observed KDF path:
    both peers are terminal, the Maker payment transaction failed, the Taker
    rejected that payment, and the Taker never sent its principal payment.
    """
    expected_pairs = {"USDT-BEP20", "LTC"}
    reported_pairs = set(statuses) | set(request_failures)
    successful: list[str] = []
    safely_aborted: list[str] = []
    unsafe: list[str] = []
    known_order_statuses = order_statuses or {}

    for rel, (maker_status, taker_status) in statuses.items():
        maker_finished = maker_status.get("is_finished") is True
        taker_finished = taker_status.get("is_finished") is True
        maker_success = maker_status.get("is_success") is True
        taker_success = taker_status.get("is_success") is True
        maker_events = set(_event_types(maker_status))
        taker_events = set(_event_types(taker_status))

        if maker_finished and taker_finished and maker_success and taker_success:
            successful.append(rel)
            continue
        if (
            maker_finished
            and taker_finished
            and maker_status.get("is_success") is False
            and taker_status.get("is_success") is False
            and "MakerPaymentTransactionFailed" in maker_events
            and "MakerPaymentValidateFailed" in taker_events
            and "TakerPaymentSent" not in taker_events
        ):
            safely_aborted.append(rel)
            continue
        unsafe.append(rel)

    for rel in request_failures:
        if known_order_statuses.get(rel) not in {
            OwnedOrderStatus.CANCELLED.value,
            OwnedOrderStatus.INSUFFICIENT_BALANCE.value,
        }:
            unsafe.append(rel)

    complete = (
        reported_pairs == expected_pairs
        and not (set(statuses) & set(request_failures))
    )
    dual_match = len(statuses) == 2
    if not complete:
        outcome = "INCOMPLETE_RACE"
    elif unsafe:
        outcome = "UNSAFE_SWAP_FAILURE"
    elif len(successful) == 2:
        outcome = "DUAL_MATCH_BOTH_SETTLED"
    elif len(successful) == 1 and safely_aborted:
        outcome = "DUAL_MATCH_ONE_SETTLED_ONE_ABORTED_BEFORE_TAKER_PAYMENT"
    elif len(successful) == 1 and request_failures:
        outcome = "ONE_SWAP_SETTLED_OTHER_REQUEST_REJECTED"
    elif safely_aborted and not successful:
        outcome = "NO_SWAP_SETTLED"
    else:
        outcome = "UNCLASSIFIED_RACE"

    funds_safe = (
        complete
        and not unsafe
        and len(successful) == 1
        and len(successful) + len(safely_aborted) + len(request_failures) == 2
    )
    test_passed = funds_safe and outcome in {
        "DUAL_MATCH_ONE_SETTLED_ONE_ABORTED_BEFORE_TAKER_PAYMENT",
        "ONE_SWAP_SETTLED_OTHER_REQUEST_REJECTED",
    }
    return ConcurrentRaceClassification(
        outcome=outcome,
        test_passed=test_passed,
        completed=complete,
        funds_safe=funds_safe,
        dual_match_observed=dual_match,
        successful_pairs=tuple(sorted(successful)),
        safely_aborted_pairs=tuple(sorted(safely_aborted)),
        unsafe_pairs=tuple(sorted(set(unsafe))),
    )


def _event_types(status: Mapping[str, Any]) -> tuple[str, ...]:
    events = status.get("events")
    if not isinstance(events, list):
        return ()
    result: list[str] = []
    for item in events:
        if not isinstance(item, Mapping):
            continue
        event = item.get("event")
        if isinstance(event, Mapping) and isinstance(event.get("type"), str):
            result.append(str(event["type"]))
    return tuple(result)


def _verify_addresses(
    label: str, wallet: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    for ticker in ("ARRR", "BNB", "USDT-BEP20", "LTC"):
        actual = str(wallet.get(ticker, {}).get("address", ""))
        wanted = str(expected.get(ticker, {}).get("address", ""))
        if not actual or actual != wanted:
            raise FundedSwapTestError(
                f"{label} {ticker} address does not match expected funded address"
            )


def _wallet_balances(wallet: Mapping[str, Any]) -> dict[str, str]:
    return {
        ticker: str(wallet[ticker].get("balance", "0"))
        for ticker in ("ARRR", "BNB", "USDT-BEP20", "LTC")
    }


def _current_balances_for(client: KdfRpcClient) -> dict[str, str]:
    return {
        ticker: str(client.balance(ticker).get("balance", "0"))
        for ticker in ("ARRR", "BNB", "USDT-BEP20", "LTC")
    }


def _assert_funded(
    balances: Mapping[str, Mapping[str, str]],
    volume: Decimal,
    usdt_price: Decimal,
    ltc_price: Decimal,
) -> None:
    maker = {key: Decimal(value) for key, value in balances["maker"].items()}
    taker = {key: Decimal(value) for key, value in balances["taker"].items()}
    if maker["ARRR"] <= volume * Decimal("3"):
        raise FundedSwapTestError("maker ARRR balance is below maximum test exposure")
    if taker["USDT-BEP20"] <= volume * usdt_price + Decimal("0.001"):
        raise FundedSwapTestError("taker USDT-BEP20 balance is insufficient")
    if taker["LTC"] <= volume * ltc_price * Decimal("2") + Decimal("0.0001"):
        raise FundedSwapTestError("taker LTC balance is insufficient")
    if maker["BNB"] <= 0 or taker["BNB"] <= 0:
        raise FundedSwapTestError("both wallets require BNB for gas")


def _preflight(
    *, maker: KdfRpcClient, taker: KdfRpcClient, volume: Decimal,
    usdt_price: Decimal, ltc_price: Decimal
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for rel, price in (("USDT-BEP20", usdt_price), ("LTC", ltc_price)):
        result[f"maker_{rel}"] = maker.v2(
            "trade_preimage",
            {"base": "ARRR", "rel": rel, "price": str(price),
             "volume": str(volume), "swap_method": "setprice"},
        )
        result[f"taker_{rel}"] = taker.v2(
            "trade_preimage",
            {"base": "ARRR", "rel": rel, "price": str(price),
             "volume": str(volume), "swap_method": "buy"},
        )
    return result


def _plan(rel: str, price: Decimal, volume: Decimal) -> QuotePlan:
    return QuotePlan(
        dex_side=DexSide.SELL_ARRR,
        hedge_side=HedgeSide.BUY,
        arrr_quantity=volume,
        reference_vwap=price,
        cex_limit_price=price,
        human_price_usdt_per_arrr=price,
        kdf_base="ARRR",
        kdf_rel=rel,
        kdf_price=price,
        kdf_volume=volume,
        effective_edge=Decimal("0"),
        market_id=f"ARRR-{rel}",
        quote_currency=rel,
        inventory_pool="ARRR",
    )


def _publish_pair(
    maker: KdfRpcClient,
    store: OrderOwnershipStore,
    *,
    volume: Decimal,
    usdt_price: Decimal,
    ltc_price: Decimal,
) -> dict[str, OwnedOrder]:
    orders: dict[str, OwnedOrder] = {}
    for rel, price in (("USDT-BEP20", usdt_price), ("LTC", ltc_price)):
        plan = _plan(rel, price, volume)
        response = maker.set_price(
            base="ARRR", rel=rel, price=price, volume=volume,
            min_volume=volume, save_in_history=True,
        )
        if not isinstance(response, Mapping) or not response.get("uuid"):
            raise FundedSwapTestError(f"setprice {rel} omitted maker order UUID")
        orders[rel] = store.register(str(response["uuid"]), plan)
    return orders


def _take(
    taker: KdfRpcClient,
    *,
    rel: str,
    price: Decimal,
    volume: Decimal,
    order_uuid: str,
) -> str:
    response = taker.legacy(
        "buy",
        base="ARRR",
        rel=rel,
        price=str(price),
        volume=str(volume),
        order_type={"type": "FillOrKill"},
        match_by={"type": "Orders", "data": [order_uuid]},
        save_in_history=True,
    )
    if not isinstance(response, Mapping) or not response.get("uuid"):
        raise FundedSwapTestError(f"buy {rel} omitted swap UUID")
    return str(response["uuid"])


def _take_concurrently(
    taker: KdfRpcClient,
    *,
    volume: Decimal,
    usdt_price: Decimal,
    ltc_price: Decimal,
    orders: Mapping[str, OwnedOrder],
) -> tuple[dict[str, str], dict[str, str]]:
    prices = {"USDT-BEP20": usdt_price, "LTC": ltc_price}
    successes: dict[str, str] = {}
    failures: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(
                _take,
                taker,
                rel=rel,
                price=prices[rel],
                volume=volume,
                order_uuid=order.order_uuid,
            ): rel
            for rel, order in orders.items()
        }
        for future in concurrent.futures.as_completed(futures):
            rel = futures[future]
            try:
                successes[rel] = future.result()
            except (KdfError, FundedSwapTestError) as exc:
                failures[rel] = f"{type(exc).__name__}: {exc}"
    return successes, failures


def _wait_for_selective_oco(
    reconciler: KdfReconciler,
    store: OrderOwnershipStore,
    *,
    matched_uuid: str,
    sibling_uuid: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            reconciler.reconcile_once()
            sibling = store.get(sibling_uuid)
            swaps = store.swaps_for_order(matched_uuid)
            if (
                sibling is not None
                and sibling.status in {
                    OwnedOrderStatus.CANCELLED,
                    OwnedOrderStatus.INSUFFICIENT_BALANCE,
                }
                and any(swap.state.value == "ACTIVE" for swap in swaps)
            ):
                return
        except KdfReconciliationError as exc:
            last_error = exc
        time.sleep(0.25)
    raise FundedSwapTestError(f"selective OCO timeout: {last_error}")


def _reconcile_race(reconciler: KdfReconciler, *, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            reconciler.reconcile_once()
            return last_error
        except KdfReconciliationError as exc:
            last_error = str(exc)
            time.sleep(0.25)
    return last_error


def _wait_for_terminal_reconciliation(
    reconciler: KdfReconciler,
    store: OrderOwnershipStore,
    swap_uuid: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            reconciler.reconcile_once()
            swap = store.get_swap(swap_uuid)
            if swap is not None and swap.state.value != "ACTIVE":
                return
        except KdfReconciliationError as exc:
            last_error = exc
        time.sleep(0.5)
    raise FundedSwapTestError(
        f"swap {swap_uuid} remained active in reconciliation: {last_error}"
    )


def _controller(kdf: KdfRpcClient, store: OrderOwnershipStore) -> VpsController:
    stores = {
        symbol: MarketDataStore(
            symbol=symbol,
            secret="funded-test",
            max_age_ms=1000,
            clock_ms=lambda: time.time_ns() // 1_000_000,
        )
        for symbol in ("ARRRUSDT", "LTCUSDT")
    }
    return VpsController(
        kdf=kdf,
        market_data=stores["ARRRUSDT"],
        ownership=store,
        kdf_quote_ticker="USDT-BEP20",
        premium=Decimal("0"),
        cex_taker_fee=Decimal("0"),
        risk_buffer=Decimal("0"),
        max_slippage=Decimal("0.01"),
        max_daily_volume_fraction=Decimal("0.001"),
        markets=default_market_specs(),
        market_data_by_symbol=stores,
    )


def _cancel_remaining_owned(
    controller: VpsController, store: OrderOwnershipStore
) -> None:
    for order in tuple(store.active()):
        try:
            controller.cancel_owned_order(order.order_uuid)
        except Exception:
            try:
                controller.kdf.order_status(order.order_uuid)
            except Exception:
                raise
