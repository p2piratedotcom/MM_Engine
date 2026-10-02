from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .coin_registry import CoinRegistry
from .kdf import KdfError, KdfRpcClient
from .kdf_runtime import KdfBinaryManager
from .wallet_activation import activate_and_collect


class FundedSwapTestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FundedSwapTestResult:
    run_dir: str
    scenario: str
    swap_uuid: str
    maker_order_uuid: str
    volume_arrr: str
    amount_usdt: str
    maker_success: bool
    taker_success: bool
    maker_final_event: str
    taker_final_event: str
    restart_count: int
    recovery_action: str | None
    recovery_coin: str | None
    recovery_tx_hash: str | None
    refund_tx_hash: str | None
    duration_seconds: float


def run_funded_swap_test(
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
    price: Decimal = Decimal("0.25"),
    activation_timeout: float = 900.0,
    swap_timeout: float = 10800.0,
    poll_interval: float = 10.0,
    scenario: str = "normal",
) -> FundedSwapTestResult:
    if volume <= 0 or price <= 0:
        raise ValueError("volume and price must be positive")
    if activation_timeout <= 0 or swap_timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeouts and poll interval must be positive")
    if scenario not in {
        "normal",
        "restart_maker",
        "recover_taker_db_loss",
        "refund_maker_payment",
    }:
        raise ValueError(
            "scenario must be 'normal', 'restart_maker' or "
            "'recover_taker_db_loss' or 'refund_maker_payment'"
        )

    manager = KdfBinaryManager(binary_manifest_path)
    verification = manager.verify()
    manifest = manager.manifest
    if manifest.get("project") != "GLEECBTC/komodo-defi-framework":
        raise FundedSwapTestError("funded test requires the official KDF project")
    release = manifest.get("release")
    if not isinstance(release, Mapping) or release.get("tag") != "v2.6.0-beta":
        raise FundedSwapTestError("funded test requires the pinned v2.6.0-beta release")
    checks = manifest.get("verification")
    required_checks = (
        "github_asset_digest_matches",
        "zip_integrity_ok",
        "detached_signature_valid",
        "signed_checksum_valid",
    )
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

    maker_config = _runtime_config(maker_source, maker_dir, rpc_port=17793)
    taker_config = _runtime_config(taker_source, taker_dir, rpc_port=17794)
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
    pinned_coins = Path(coins_path).resolve()
    registry = CoinRegistry.from_manifest(coins_manifest_path)

    maker = KdfRpcClient(
        rpc_url="http://127.0.0.1:17793",
        userpass=str(maker_config["rpc_password"]),
        timeout=30.0,
    )
    taker = KdfRpcClient(
        rpc_url="http://127.0.0.1:17794",
        userpass=str(taker_config["rpc_password"]),
        timeout=30.0,
    )
    started = time.monotonic()
    processes: dict[str, subprocess.Popen[bytes]] = {}
    maker_order_uuid: str | None = None
    swap_uuid: str | None = None
    terminal = False
    restart_count = 0
    recovery_result: dict[str, str] = {}
    refund_result: dict[str, str] = {}

    _write_state(
        target,
        {
            "phase": "prepared",
            "scenario": scenario,
            "binary_sha256": verification.sha256,
            "volume_arrr": str(volume),
            "price_usdt_per_arrr": str(price),
            "amount_usdt": str(volume * price),
            "arrr_sync_height": arrr_sync_height,
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
        )
        _verify_addresses("maker", maker_wallet, expected_maker)
        print("Maker: indirizzi e saldi verificati", flush=True)

        taker_wallet = activate_and_collect(
            client=taker,
            registry=registry,
            timeout=activation_timeout,
            poll_interval=2.0,
            progress=lambda message: print(f"Taker: {message}", flush=True),
            arrr_sync_height=arrr_sync_height,
        )
        _verify_addresses("taker", taker_wallet, expected_taker)
        print("Taker: indirizzi e saldi verificati", flush=True)

        maker_balances = _balances(maker_wallet)
        taker_balances = _balances(taker_wallet)
        if maker_balances["ARRR"] <= volume:
            raise FundedSwapTestError("maker ARRR balance is insufficient")
        if taker_balances["USDT-BEP20"] <= volume * price:
            raise FundedSwapTestError("taker USDT-BEP20 balance is insufficient")
        if maker_balances["BNB"] <= 0 or taker_balances["BNB"] <= 0:
            raise FundedSwapTestError("both wallets require BNB for gas")

        _wait_for_peer(taker, timeout=60.0)
        print("Peer P2P locale verificato", flush=True)

        maker_preimage = maker.v2(
            "trade_preimage",
            {
                "base": "ARRR",
                "rel": "USDT-BEP20",
                "price": str(price),
                "volume": str(volume),
                "swap_method": "setprice",
            },
        )
        taker_preimage = taker.v2(
            "trade_preimage",
            {
                "base": "ARRR",
                "rel": "USDT-BEP20",
                "price": str(price),
                "volume": str(volume),
                "swap_method": "buy",
            },
        )
        print("Preflight fee e disponibilita completato", flush=True)
        _write_state(
            target,
            {
                "phase": "preflight_passed",
                "balances_before": {
                    "maker": _string_balances(maker_wallet),
                    "taker": _string_balances(taker_wallet),
                },
                "maker_preimage": maker_preimage,
                "taker_preimage": taker_preimage,
            },
        )

        maker_order = maker.legacy(
            "setprice",
            base="ARRR",
            rel="USDT-BEP20",
            price=str(price),
            volume=str(volume),
            min_volume=str(volume),
            save_in_history=True,
        )
        if not isinstance(maker_order, Mapping) or not maker_order.get("uuid"):
            raise FundedSwapTestError("setprice did not return a maker order UUID")
        maker_order_uuid = str(maker_order["uuid"])
        print(f"Ordine maker creato: {maker_order_uuid}", flush=True)
        _wait_for_order(taker, maker_order_uuid, timeout=90.0)
        print("Ordine maker visibile dal taker", flush=True)

        taker_order = taker.legacy(
            "buy",
            base="ARRR",
            rel="USDT-BEP20",
            price=str(price),
            volume=str(volume),
            order_type={"type": "FillOrKill"},
            match_by={"type": "Orders", "data": [maker_order_uuid]},
            save_in_history=True,
        )
        if not isinstance(taker_order, Mapping) or not taker_order.get("uuid"):
            raise FundedSwapTestError("buy did not return a taker/swap UUID")
        swap_uuid = str(taker_order["uuid"])
        print(f"Swap avviato: {swap_uuid}", flush=True)
        _write_state(
            target,
            {
                "phase": "swap_started",
                "maker_order_uuid": maker_order_uuid,
                "swap_uuid": swap_uuid,
            },
        )

        def restart_maker_on_event(label: str, event: str) -> None:
            nonlocal restart_count
            if scenario == "restart_maker":
                if (
                    restart_count
                    or label != "maker"
                    or event != "MakerPaymentSent"
                ):
                    return
                _write_state(
                    target,
                    {
                        "phase": "maker_restart_started",
                        "restart_trigger_event": event,
                        "restart_count": 0,
                    },
                )
                print(
                    "Riavvio controllato del Maker dopo MakerPaymentSent",
                    flush=True,
                )
                _stop_kdf(maker, processes.get("maker"))
                processes["maker"] = _start_kdf(
                    manager.binary_path, maker_dir, maker_config_path, pinned_coins
                )
                _wait_for_rpc(maker, processes["maker"], maker_dir / "kdf.log")
                restarted_wallet = activate_and_collect(
                    client=maker,
                    registry=registry,
                    timeout=activation_timeout,
                    poll_interval=2.0,
                    progress=lambda message: print(
                        f"Maker riavviato: {message}", flush=True
                    ),
                    arrr_sync_height=arrr_sync_height,
                )
                _verify_addresses(
                    "maker riavviato", restarted_wallet, expected_maker
                )
                try:
                    _wait_for_peer(taker, timeout=20.0)
                except FundedSwapTestError:
                    print(
                        "Il peer locale non si e riconnesso: riavvio il Taker",
                        flush=True,
                    )
                    _stop_kdf(taker, processes.get("taker"))
                    processes["taker"] = _start_kdf(
                        manager.binary_path,
                        taker_dir,
                        taker_config_path,
                        pinned_coins,
                    )
                    _wait_for_rpc(
                        taker, processes["taker"], taker_dir / "kdf.log"
                    )
                    restarted_taker_wallet = activate_and_collect(
                        client=taker,
                        registry=registry,
                        timeout=activation_timeout,
                        poll_interval=2.0,
                        progress=lambda message: print(
                            f"Taker riavviato: {message}", flush=True
                        ),
                        arrr_sync_height=arrr_sync_height,
                    )
                    _verify_addresses(
                        "taker riavviato",
                        restarted_taker_wallet,
                        expected_taker,
                    )
                    _wait_for_peer(taker, timeout=120.0)
                    _write_state(
                        target,
                        {"taker_peer_recovery_restart_count": 1},
                    )
                restart_count = 1
                _write_state(
                    target,
                    {
                        "phase": "maker_restarted",
                        "restart_trigger_event": event,
                        "restart_count": restart_count,
                    },
                )
                print("Maker riavviato e wallet ripristinati", flush=True)
                return

            if scenario == "refund_maker_payment":
                if (
                    refund_result
                    or label != "taker"
                    or event != "MakerPaymentWaitConfirmStarted"
                ):
                    return
                original_taker = processes["taker"]
                _write_state(
                    target,
                    {
                        "phase": "taker_crash_before_payment",
                        "fault_trigger_event": event,
                        "taker_payment_was_sent": False,
                    },
                )
                print(
                    "Crash controllato del Taker prima del pagamento USDT",
                    flush=True,
                )
                original_taker.kill()
                original_taker.wait(timeout=10.0)
                maker_finished = _wait_for_finished_swap(
                    maker,
                    swap_uuid,
                    timeout=swap_timeout,
                    label="Maker in attesa del rimborso ARRR",
                )
                refund = _event_data(maker_finished, "MakerPaymentRefunded")
                tx_hash = str(refund.get("tx_hash", "")) if refund else ""
                refund_method = "automatic"
                if not tx_hash:
                    # ARRR can reject the first locktime transaction as
                    # ``non-final`` while the chain median time still trails
                    # the transaction locktime.  KDF records the swap as
                    # finished after MakerPaymentRefundFailed, so resume it
                    # with the documented recovery RPC once the conservative
                    # wait_until timestamp has passed.
                    wait_until = _latest_wait_until(maker_finished)
                    if wait_until is not None:
                        _wait_until_timestamp(
                            wait_until,
                            label="Maker prima del recupero manuale ARRR",
                        )
                    recovered = _retry_recover_funds(
                        maker,
                        swap_uuid,
                        timeout=max(900.0, min(swap_timeout, 7200.0)),
                    )
                    action = str(recovered.get("action", ""))
                    coin = str(recovered.get("coin", ""))
                    tx_hash = str(recovered.get("tx_hash", ""))
                    if action != "RefundedMyPayment" or coin != "ARRR" or not tx_hash:
                        raise FundedSwapTestError(
                            "manual recovery did not return the expected ARRR refund"
                        )
                    refund_method = "recover_funds_of_swap"
                refund_result.update(
                    {
                        "action": "RefundedMyPayment",
                        "tx_hash": tx_hash,
                        "method": refund_method,
                    }
                )
                _write_state(
                    target,
                    {
                        "phase": "maker_payment_refunded",
                        "refund_action": "RefundedMyPayment",
                        "refund_coin": "ARRR",
                        "refund_tx_hash": tx_hash,
                        "refund_method": refund_method,
                    },
                )
                print(f"Rimborso ARRR trasmesso: {tx_hash}", flush=True)

                processes["taker"] = _start_kdf(
                    manager.binary_path,
                    taker_dir,
                    taker_config_path,
                    pinned_coins,
                )
                _wait_for_rpc(
                    taker, processes["taker"], taker_dir / "kdf.log"
                )
                restarted_taker_wallet = activate_and_collect(
                    client=taker,
                    registry=registry,
                    timeout=activation_timeout,
                    poll_interval=2.0,
                    progress=lambda message: print(
                        f"Taker dopo rimborso: {message}", flush=True
                    ),
                    arrr_sync_height=arrr_sync_height,
                )
                _verify_addresses(
                    "taker dopo rimborso",
                    restarted_taker_wallet,
                    expected_taker,
                )
                _wait_for_finished_swap(
                    taker,
                    swap_uuid,
                    timeout=1800.0,
                    label="Taker dopo rimborso Maker",
                )
                return

            if (
                scenario != "recover_taker_db_loss"
                or recovery_result
                or label != "taker"
                or event != "TakerPaymentSent"
            ):
                return
            original_taker = processes["taker"]
            _write_state(
                target,
                {
                    "phase": "taker_crash_injected",
                    "fault_trigger_event": event,
                    "original_taker_db_preserved": True,
                },
            )
            print(
                "Crash controllato del Taker dopo TakerPaymentSent",
                flush=True,
            )
            original_taker.kill()
            original_taker.wait(timeout=10.0)

            maker_finished = _wait_for_finished_swap(
                maker,
                swap_uuid,
                timeout=900.0,
                label="Maker rimasto online",
            )
            recovery_dir = target / "taker-recovery"
            recovery_dir.mkdir(mode=0o700)
            recovery_config = _runtime_config(
                taker_source, recovery_dir, rpc_port=17794
            )
            recovery_config.update(
                {
                    "i_am_seed": False,
                    "is_bootstrap_node": False,
                    "disable_p2p": False,
                    "seednodes": ["127.0.0.1"],
                }
            )
            recovery_config.pop("myipaddr", None)
            recovery_config_path = recovery_dir / "MM2.json"
            _write_private_json(recovery_config_path, recovery_config)
            processes["taker"] = _start_kdf(
                manager.binary_path,
                recovery_dir,
                recovery_config_path,
                pinned_coins,
            )
            _wait_for_rpc(
                taker, processes["taker"], recovery_dir / "kdf.log"
            )
            recovery_wallet = activate_and_collect(
                client=taker,
                registry=registry,
                timeout=activation_timeout,
                poll_interval=2.0,
                progress=lambda message: print(
                    f"Taker recovery: {message}", flush=True
                ),
                arrr_sync_height=arrr_sync_height,
            )
            _verify_addresses("taker recovery", recovery_wallet, expected_taker)
            recreated = taker.v2(
                "recreate_swap_data", {"swap": maker_finished}
            )
            if not isinstance(recreated, Mapping):
                raise FundedSwapTestError(
                    "recreate_swap_data did not return reconstructed data"
                )
            reconstructed_swap = recreated.get("swap")
            if not isinstance(reconstructed_swap, Mapping):
                raise FundedSwapTestError(
                    "recreate_swap_data response has no swap object"
                )
            taker.legacy("import_swaps", swaps=[reconstructed_swap])
            reconstructed = _swap_status(taker, swap_uuid)
            if not reconstructed:
                raise FundedSwapTestError("reconstructed taker swap is missing")
            if reconstructed.get("is_finished") is not True:
                print(
                    "Riavvio del Taker ricostruito per avviare il kick-start",
                    flush=True,
                )
                _stop_kdf(taker, processes.get("taker"))
                processes["taker"] = _start_kdf(
                    manager.binary_path,
                    recovery_dir,
                    recovery_config_path,
                    pinned_coins,
                )
                _wait_for_rpc(
                    taker, processes["taker"], recovery_dir / "kdf.log"
                )
                recovery_wallet = activate_and_collect(
                    client=taker,
                    registry=registry,
                    timeout=activation_timeout,
                    poll_interval=2.0,
                    progress=lambda message: print(
                        f"Taker kick-start: {message}", flush=True
                    ),
                    arrr_sync_height=arrr_sync_height,
                )
                _verify_addresses(
                    "taker kick-start", recovery_wallet, expected_taker
                )
                reconstructed = _wait_for_finished_swap(
                    taker,
                    swap_uuid,
                    timeout=swap_timeout,
                    label="Taker ricostruito in attesa del locktime",
                )
            recovered = taker.legacy(
                "recover_funds_of_swap", params={"uuid": swap_uuid}
            )
            if not isinstance(recovered, Mapping):
                raise FundedSwapTestError(
                    "recover_funds_of_swap returned an invalid response"
                )
            action = str(recovered.get("action", ""))
            coin = str(recovered.get("coin", ""))
            tx_hash = str(recovered.get("tx_hash", ""))
            if action not in {"SpentOtherPayment", "RefundedMyPayment"}:
                raise FundedSwapTestError(
                    f"unexpected recovery action: {action or 'missing'}"
                )
            if coin != "ARRR" or not tx_hash:
                raise FundedSwapTestError(
                    "recovery did not return the expected ARRR transaction"
                )
            recovery_result.update(
                {"action": action, "coin": coin, "tx_hash": tx_hash}
            )
            _write_state(
                target,
                {
                    "phase": "funds_recovered",
                    "recovery_action": action,
                    "recovery_coin": coin,
                    "recovery_tx_hash": tx_hash,
                    "original_taker_db_preserved": True,
                },
            )
            print(
                f"Recupero manuale riuscito: {action} {coin} {tx_hash}",
                flush=True,
            )

        maker_status, taker_status = _monitor_swap(
            maker=maker,
            taker=taker,
            processes=processes,
            swap_uuid=swap_uuid,
            target=target,
            timeout=swap_timeout,
            poll_interval=poll_interval,
            event_hook=restart_maker_on_event,
        )
        terminal = True
        maker_success = maker_status.get("is_success") is True
        taker_success = taker_status.get("is_success") is True
        if scenario == "recover_taker_db_loss":
            success = bool(recovery_result)
        elif scenario == "refund_maker_payment":
            success = bool(refund_result)
        else:
            success = maker_success and taker_success
        final_wallets = {
            "maker": _current_balances(maker),
            "taker": _current_balances(taker),
        }
        _write_state(
            target,
            {
                "phase": "finished",
                "maker_order_uuid": maker_order_uuid,
                "swap_uuid": swap_uuid,
                "maker_status": maker_status,
                "taker_status": taker_status,
                "balances_after": final_wallets,
                "success": success,
                "restart_count": restart_count,
                "recovery_action": recovery_result.get("action"),
                "recovery_coin": recovery_result.get("coin"),
                "recovery_tx_hash": recovery_result.get("tx_hash"),
                "refund_action": refund_result.get("action"),
                "refund_coin": "ARRR" if refund_result else None,
                "refund_tx_hash": refund_result.get("tx_hash"),
                "refund_method": refund_result.get("method"),
            },
        )
        return FundedSwapTestResult(
            run_dir=str(target),
            scenario=scenario,
            swap_uuid=swap_uuid,
            maker_order_uuid=maker_order_uuid,
            volume_arrr=str(volume),
            amount_usdt=str(volume * price),
            maker_success=maker_success,
            taker_success=taker_success,
            maker_final_event=_last_event(maker_status),
            taker_final_event=_last_event(taker_status),
            restart_count=restart_count,
            recovery_action=recovery_result.get("action"),
            recovery_coin=recovery_result.get("coin"),
            recovery_tx_hash=recovery_result.get("tx_hash"),
            refund_tx_hash=refund_result.get("tx_hash"),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except BaseException as exc:
        _write_state(
            target,
            {
                "phase": "error",
                "scenario": scenario,
                "error": f"{type(exc).__name__}: {exc}",
                "maker_order_uuid": maker_order_uuid,
                "swap_uuid": swap_uuid,
                "kdf_left_running_for_recovery": bool(swap_uuid and not terminal),
                "restart_count": restart_count,
            },
        )
        if maker_order_uuid and not swap_uuid:
            try:
                maker.legacy("cancel_order", uuid=maker_order_uuid)
            except Exception:
                pass
        raise
    finally:
        if not swap_uuid or terminal:
            _stop_kdf(taker, processes.get("taker"))
            _stop_kdf(maker, processes.get("maker"))


def _runtime_config(
    source: Mapping[str, Any], directory: Path, *, rpc_port: int
) -> dict[str, Any]:
    result = dict(source)
    result.update(
        {
            "rpcip": "127.0.0.1",
            "rpcport": rpc_port,
            "rpc_local_only": True,
            "dbdir": str(directory / "db"),
            "userhome": str(directory),
        }
    )
    return result


def _load_private_config(path: Path) -> Mapping[str, Any]:
    if path.stat().st_mode & 0o077:
        raise FundedSwapTestError(f"private config permissions are too open: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(loaded, Mapping)
        or not loaded.get("passphrase")
        or not loaded.get("rpc_password")
    ):
        raise FundedSwapTestError(f"invalid private KDF config: {path}")
    return loaded


def _load_public_addresses(path: Path) -> Mapping[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise FundedSwapTestError(f"invalid address file: {path}")
    return loaded


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _write_state(target: Path, update: Mapping[str, Any]) -> None:
    path = target / "state.json"
    current: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            current.update(loaded)
    current.update(update)
    current["updated_at"] = int(time.time())
    _write_private_json(path, current)


def _start_kdf(
    binary: Path, directory: Path, config: Path, coins: Path
) -> subprocess.Popen[bytes]:
    log_path = directory / "kdf.log"
    log_stream = log_path.open("ab", buffering=0)
    os.chmod(log_path, 0o600)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "MM_CONF_PATH": str(config),
        "MM_COINS_PATH": str(coins),
        "RUST_LOG": "info",
    }
    return subprocess.Popen(
        [str(binary)],
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_for_rpc(
    client: KdfRpcClient, process: subprocess.Popen[bytes], log: Path
) -> None:
    deadline = time.monotonic() + 30.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise FundedSwapTestError(f"KDF exited with {process.returncode}: {tail}")
        try:
            client.legacy("version")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise FundedSwapTestError(f"KDF RPC startup timeout: {last_error}")


def _verify_addresses(
    label: str, wallet: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    for ticker in ("ARRR", "BNB", "USDT-BEP20"):
        actual = str(wallet.get(ticker, {}).get("address", ""))
        wanted = str(expected.get(ticker, {}).get("address", ""))
        if not actual or actual != wanted:
            raise FundedSwapTestError(
                f"{label} {ticker} address does not match funded address"
            )


def _balances(wallet: Mapping[str, Any]) -> dict[str, Decimal]:
    return {
        ticker: Decimal(str(data.get("balance", "0")))
        for ticker, data in wallet.items()
    }


def _string_balances(wallet: Mapping[str, Any]) -> dict[str, str]:
    return {
        ticker: str(data.get("balance", "0")) for ticker, data in wallet.items()
    }


def _current_balances(client: KdfRpcClient) -> dict[str, str]:
    return {
        ticker: str(client.balance(ticker).get("balance", "0"))
        for ticker in ("ARRR", "BNB", "USDT-BEP20")
    }


def _wait_for_peer(client: KdfRpcClient, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = client.legacy("get_directly_connected_peers")
            if result:
                return
        except KdfError:
            pass
        time.sleep(1.0)
    raise FundedSwapTestError("taker did not connect to the local maker peer")


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, needle) for item in value)
    return str(value) == needle


def _wait_for_order(
    client: KdfRpcClient,
    uuid: str,
    *,
    timeout: float,
    rel: str = "USDT-BEP20",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            book = client.v2(
                "orderbook", {"base": "ARRR", "rel": rel}
            )
            if _contains(book, uuid):
                return
        except KdfError:
            pass
        time.sleep(1.0)
    raise FundedSwapTestError("maker order did not propagate to taker")


def _swap_status(client: KdfRpcClient, uuid: str) -> Mapping[str, Any] | None:
    try:
        result = client.legacy("my_swap_status", params={"uuid": uuid})
    except KdfError:
        return None
    return result if isinstance(result, Mapping) else None


def _last_event(status: Mapping[str, Any] | None) -> str:
    if not status:
        return "NotFound"
    events = status.get("events")
    if not isinstance(events, list) or not events:
        return "NoEvents"
    event = events[-1].get("event", {}) if isinstance(events[-1], Mapping) else {}
    return str(event.get("type", "Unknown")) if isinstance(event, Mapping) else "Unknown"


def _event_data(status: Mapping[str, Any], event_type: str) -> Mapping[str, Any] | None:
    events = status.get("events")
    if not isinstance(events, list):
        return None
    for item in reversed(events):
        if not isinstance(item, Mapping):
            continue
        event = item.get("event")
        if not isinstance(event, Mapping) or event.get("type") != event_type:
            continue
        data = event.get("data")
        return data if isinstance(data, Mapping) else {}
    return None


def _latest_wait_until(status: Mapping[str, Any]) -> int | None:
    events = status.get("events")
    if not isinstance(events, list):
        return None
    for item in reversed(events):
        if not isinstance(item, Mapping):
            continue
        event = item.get("event")
        if not isinstance(event, Mapping):
            continue
        data = event.get("data")
        if not isinstance(data, Mapping) or data.get("wait_until") is None:
            continue
        try:
            return int(data["wait_until"])
        except (TypeError, ValueError):
            continue
    return None


def _wait_until_timestamp(timestamp: int, *, label: str) -> None:
    last_heartbeat = 0.0
    while int(time.time()) < timestamp:
        now = time.monotonic()
        if now - last_heartbeat >= 60.0:
            last_heartbeat = now
            print(
                f"{label}: secondi rimanenti {max(0, timestamp - int(time.time()))}",
                flush=True,
            )
        time.sleep(min(10.0, max(0.5, timestamp - time.time())))


def _retry_recover_funds(
    client: KdfRpcClient,
    uuid: str,
    *,
    timeout: float,
) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = client.legacy(
                "recover_funds_of_swap",
                params={"uuid": uuid},
            )
            if isinstance(result, Mapping):
                return result
            last_error = FundedSwapTestError(
                "recover_funds_of_swap returned an invalid response"
            )
        except KdfError as exc:
            last_error = exc
        time.sleep(10.0)
    raise FundedSwapTestError(
        f"manual refund recovery did not succeed before timeout: {last_error}"
    )


def _wait_for_finished_swap(
    client: KdfRpcClient,
    uuid: str,
    *,
    timeout: float,
    label: str,
) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    last_heartbeat = 0.0
    while time.monotonic() < deadline:
        status = _swap_status(client, uuid)
        event = _last_event(status)
        if event != last:
            print(f"{label}: {event}", flush=True)
            last = event
        if status and status.get("is_finished") is True:
            return status
        now = time.monotonic()
        if status and now - last_heartbeat >= 60.0:
            last_heartbeat = now
            wait_until: int | None = None
            events = status.get("events")
            if isinstance(events, list):
                for item in reversed(events):
                    if not isinstance(item, Mapping):
                        continue
                    event_value = item.get("event")
                    if not isinstance(event_value, Mapping):
                        continue
                    data = event_value.get("data")
                    if isinstance(data, Mapping) and data.get("wait_until"):
                        wait_until = int(data["wait_until"])
                        break
            remaining = (
                max(0, wait_until - int(time.time()))
                if wait_until is not None
                else None
            )
            print(
                f"{label}: attivo; secondi al locktime {remaining}",
                flush=True,
            )
        time.sleep(0.5)
    raise FundedSwapTestError(f"{label} did not finish before timeout")


def _monitor_swap(
    *,
    maker: KdfRpcClient,
    taker: KdfRpcClient,
    processes: Mapping[str, subprocess.Popen[bytes]],
    swap_uuid: str,
    target: Path,
    timeout: float,
    poll_interval: float,
    event_hook: Callable[[str, str], None] | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    latest: dict[str, Mapping[str, Any] | None] = {"maker": None, "taker": None}
    while time.monotonic() < deadline:
        for label, client in (("maker", maker), ("taker", taker)):
            process = processes[label]
            if process.poll() is not None:
                raise FundedSwapTestError(f"{label} KDF exited during active swap")
            status = _swap_status(client, swap_uuid)
            latest[label] = status
            event = _last_event(status)
            if last.get(label) != event:
                print(f"{label.capitalize()} swap: {event}", flush=True)
                last[label] = event
                _write_state(
                    target,
                    {
                        "phase": "swap_running",
                        "swap_uuid": swap_uuid,
                        "maker_last_event": _last_event(latest["maker"]),
                        "taker_last_event": _last_event(latest["taker"]),
                    },
                )
                if event_hook is not None:
                    event_hook(label, event)
        maker_status = latest["maker"]
        taker_status = latest["taker"]
        if (
            maker_status
            and taker_status
            and maker_status.get("is_finished") is True
            and taker_status.get("is_finished") is True
        ):
            return maker_status, taker_status
        time.sleep(poll_interval)
    raise FundedSwapTestError(
        "swap monitoring timeout; KDF instances left running for recovery"
    )


def _stop_kdf(
    client: KdfRpcClient, process: subprocess.Popen[bytes] | None
) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        client.legacy("stop")
    except Exception:
        pass
    try:
        process.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
