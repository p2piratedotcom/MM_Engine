from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .coin_registry import CoinRegistry
from .kdf import KdfRpcClient
from .kdf_runtime import validate_runtime_config


class WalletActivationError(RuntimeError):
    pass


def activate_wallet_from_config(
    *,
    config_path: str | Path,
    manifest_path: str | Path,
    output_path: str | Path,
    timeout: float = 1800.0,
    poll_interval: float = 5.0,
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, str]]:
    config_file = Path(config_path).resolve()
    problems = validate_runtime_config(config_file)
    if problems:
        raise WalletActivationError("; ".join(problems))

    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WalletActivationError("impossibile leggere la configurazione KDF") from exc

    rpc_host = str(config.get("rpcip", "127.0.0.1"))
    if rpc_host == "::1":
        rpc_host = "[::1]"
    client = KdfRpcClient(
        rpc_url=f"http://{rpc_host}:{int(config.get('rpcport', 7783))}",
        userpass=str(config["rpc_password"]),
        timeout=30.0,
    )
    registry = CoinRegistry.from_manifest(manifest_path)
    result = activate_and_collect(
        client=client,
        registry=registry,
        timeout=timeout,
        poll_interval=poll_interval,
        progress=progress,
        include_ltc=True,
    )
    _write_private_json(Path(output_path).resolve(), result)
    return result


def activate_and_collect(
    *,
    client: KdfRpcClient,
    registry: CoinRegistry,
    timeout: float,
    poll_interval: float,
    progress: Callable[[str], None] | None = None,
    arrr_sync_height: int | None = None,
    include_ltc: bool = False,
) -> dict[str, dict[str, str]]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    report = progress or (lambda _message: None)
    enabled = _enabled_tickers(client.enabled_coins())
    tasks: dict[str, tuple[int, Callable[[int], Any]]] = {}

    if "ARRR" in enabled:
        report("ARRR è già attiva")
    else:
        arrr_params = (
            registry.arrr_activation_params()
            if arrr_sync_height is None
            else registry.arrr_activation_params(sync_height=arrr_sync_height)
        )
        response = client.enable_z_coin(
            ticker="ARRR",
            activation_params=arrr_params,
        )
        task_id = _task_id(response, "ARRR")
        tasks["ARRR"] = (task_id, client.enable_z_coin_status)
        report(f"Attivazione ARRR avviata (task {task_id})")

    quote_tickers = {"BNB", "USDT-BEP20"}
    if quote_tickers.issubset(enabled):
        report("BNB e USDT-BEP20 sono già attive")
    elif enabled.intersection(quote_tickers):
        missing = ", ".join(sorted(quote_tickers - enabled))
        raise WalletActivationError(
            f"attivazione EVM parziale: manca {missing}; riavvia KDF e riprova"
        )
    else:
        response = client.enable_evm_with_tokens(
            registry.evm_token_activation_params("USDT-BEP20")
        )
        task_id = _task_id(response, "BNB/USDT-BEP20")
        tasks["BNB/USDT-BEP20"] = (
            task_id,
            client.enable_evm_with_tokens_status,
        )
        report(f"Attivazione BNB e USDT-BEP20 avviata (task {task_id})")

    deadline = time.monotonic() + timeout
    last_status: dict[str, str] = {}
    while tasks:
        for label, (task_id, status_call) in tuple(tasks.items()):
            response = status_call(task_id)
            if not isinstance(response, Mapping):
                raise WalletActivationError(f"stato non valido per {label}")
            status = str(response.get("status", "Sconosciuto"))
            if last_status.get(label) != status:
                report(f"{label}: {status}")
                last_status[label] = status
            if status == "Ok":
                del tasks[label]
            elif status in {"Error", "Cancelled", "UserActionRequired"}:
                raise WalletActivationError(f"attivazione {label} terminata con {status}")
        if tasks:
            if time.monotonic() >= deadline:
                pending = ", ".join(tasks)
                raise WalletActivationError(f"timeout durante l'attivazione di {pending}")
            time.sleep(poll_interval)

    if include_ltc:
        if "LTC" in enabled:
            report("LTC è già attiva")
        else:
            response = client.electrum(**registry.utxo_activation_params("LTC"))
            if not isinstance(response, Mapping) or not response.get("address"):
                raise WalletActivationError("attivazione LTC non valida")
            report("LTC attivata")

    balances: dict[str, dict[str, str]] = {}
    tickers = ("ARRR", "BNB", "USDT-BEP20", "LTC") if include_ltc else (
        "ARRR",
        "BNB",
        "USDT-BEP20",
    )
    for ticker in tickers:
        response = client.balance(ticker)
        if not isinstance(response, Mapping) or not response.get("address"):
            raise WalletActivationError(f"KDF non ha restituito l'indirizzo {ticker}")
        balances[ticker] = {
            key: str(response[key])
            for key in ("address", "balance", "unspendable_balance")
            if response.get(key) is not None
        }
    return balances


def _enabled_tickers(response: Any) -> set[str]:
    if not isinstance(response, Mapping) or not isinstance(response.get("coins"), list):
        raise WalletActivationError("risposta get_enabled_coins non valida")
    return {
        str(item["ticker"])
        for item in response["coins"]
        if isinstance(item, Mapping) and item.get("ticker")
    }


def _task_id(response: Any, label: str) -> int:
    if not isinstance(response, Mapping) or response.get("task_id") is None:
        raise WalletActivationError(f"KDF non ha restituito il task_id per {label}")
    return int(response["task_id"])


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            os.chmod(temporary_name, 0o600)
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
