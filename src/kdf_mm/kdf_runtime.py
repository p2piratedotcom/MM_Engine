from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from .kdf import KdfError, KdfRpcClient


class KdfBinaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KdfBinaryVerification:
    path: str
    size_bytes: int
    sha256: str
    architecture: str
    executable: bool


@dataclass(frozen=True, slots=True)
class KdfContractResult:
    version: str
    rpc_url: str
    enabled_coins: int
    maker_orders: int
    taker_orders: int
    isolated_p2p: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class KdfActivationSmokeResult:
    version: str
    enabled_coins: tuple[str, ...]
    arrr_address: str
    evm_address: str
    balances: Mapping[str, str]
    isolated_p2p: bool
    funds_used: bool
    orders_created: int
    duration_seconds: float


class KdfBinaryManager:
    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path).resolve()
        try:
            loaded = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KdfBinaryError("cannot read the KDF binary manifest") from exc
        if not isinstance(loaded, Mapping):
            raise KdfBinaryError("KDF binary manifest must be a JSON object")
        self.manifest: Mapping[str, Any] = loaded
        self.binary_path = self._resolve_binary_path()

    def _resolve_binary_path(self) -> Path:
        try:
            relative = Path(str(self.manifest["binary"]["path"]))
        except (KeyError, TypeError) as exc:
            raise KdfBinaryError("manifest does not contain binary.path") from exc
        if relative.is_absolute():
            raise KdfBinaryError("manifest binary.path must be relative")
        candidate = (self.manifest_path.parent / relative).resolve()
        try:
            candidate.relative_to(self.manifest_path.parent)
        except ValueError as exc:
            raise KdfBinaryError("manifest binary.path escapes its directory") from exc
        return candidate

    def verify(self) -> KdfBinaryVerification:
        try:
            expected_size = int(self.manifest["binary"]["size_bytes"])
            expected_hash = str(self.manifest["binary"]["sha256"]).lower()
            expected_arch = str(self.manifest["binary"]["architecture"])
        except (KeyError, TypeError, ValueError) as exc:
            raise KdfBinaryError("manifest binary verification fields are invalid") from exc
        if not self.binary_path.is_file():
            raise KdfBinaryError(f"KDF binary is missing: {self.binary_path}")
        size = self.binary_path.stat().st_size
        if size != expected_size:
            raise KdfBinaryError(
                f"KDF binary size mismatch: expected {expected_size}, got {size}"
            )
        digest = _sha256_file(self.binary_path)
        if digest != expected_hash:
            raise KdfBinaryError("KDF binary SHA-256 mismatch")
        architecture = _elf_architecture(self.binary_path)
        if architecture != expected_arch:
            raise KdfBinaryError(
                f"KDF architecture mismatch: expected {expected_arch}, got {architecture}"
            )
        executable = os.access(self.binary_path, os.X_OK)
        if not executable:
            raise KdfBinaryError("KDF binary is not executable")
        return KdfBinaryVerification(
            path=str(self.binary_path),
            size_bytes=size,
            sha256=digest,
            architecture=architecture,
            executable=executable,
        )

    def probe_banner(self, *, timeout: float = 5.0) -> str:
        self.verify()
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
        }
        try:
            result = subprocess.run(
                [str(self.binary_path), "help"],
                cwd=self.binary_path.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KdfBinaryError("KDF help probe failed") from exc
        first_line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        expected_name = str(self.manifest.get("binary", {}).get("reported_name", ""))
        if not expected_name or expected_name not in first_line:
            raise KdfBinaryError("KDF probe returned an unexpected banner")
        return first_line


def run_kdf_activation_smoke_test(
    *,
    manifest_path: str | Path,
    fixture_path: str | Path,
    coins_manifest_path: str | Path,
    coins_path: str | Path,
    timeout: float = 900.0,
    poll_interval: float = 2.0,
) -> KdfActivationSmokeResult:
    """Activate ARRR, BNB, USDT-BEP20 and LTC with the public test identity.

    The process, wallet database and ports are temporary. P2P stays in memory,
    no order RPC is enabled, and the runner refuses every non-public mnemonic.
    """

    from .coin_registry import CoinRegistry
    from .wallet_activation import activate_and_collect

    manager = KdfBinaryManager(manifest_path)
    manager.verify()
    fixture = _load_json_object(Path(fixture_path), "KDF activation fixture")
    known_test_seed = (
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )
    if fixture.get("passphrase") != known_test_seed:
        raise KdfBinaryError("activation test refuses any non-public wallet mnemonic")

    pinned_coins = Path(coins_path).resolve()
    if not pinned_coins.is_file():
        raise KdfBinaryError(f"pinned KDF coins file is missing: {pinned_coins}")
    registry = CoinRegistry.from_manifest(coins_manifest_path)

    rpc_port = _available_loopback_port()
    p2p_memory_port = _available_loopback_port()
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None

    with TemporaryDirectory(prefix="kdf-mm-activation-") as directory:
        runtime_dir = Path(directory)
        runtime_config = dict(fixture)
        runtime_config.update(
            {
                "rpcip": "127.0.0.1",
                "rpcport": rpc_port,
                "rpc_local_only": True,
                "myipaddr": "127.0.0.1",
                "i_am_seed": True,
                "p2p_in_memory": True,
                "p2p_in_memory_port": p2p_memory_port,
                "seednodes": [],
                "dbdir": str(runtime_dir / "db"),
                "userhome": str(runtime_dir),
            }
        )
        config_path = runtime_dir / "MM2.activation.json"
        config_path.write_text(json.dumps(runtime_config, indent=2), encoding="utf-8")
        log_path = runtime_dir / "kdf-activation.log"
        client = KdfRpcClient(
            rpc_url=f"http://127.0.0.1:{rpc_port}",
            userpass=str(runtime_config["rpc_password"]),
            timeout=30.0,
        )
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "MM_CONF_PATH": str(config_path),
            "MM_COINS_PATH": str(pinned_coins),
            "RUST_LOG": "warn",
        }

        try:
            with log_path.open("w+b") as log_stream:
                process = subprocess.Popen(
                    [str(manager.binary_path)],
                    cwd=runtime_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                )
                version = _wait_for_kdf(client, process, log_path, timeout=30.0)
                wallet = activate_and_collect(
                    client=client,
                    registry=registry,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    include_ltc=True,
                )
                enabled_response = client.enabled_coins()
                enabled = tuple(sorted(_coin_tickers(enabled_response)))
                expected = {"ARRR", "BNB", "USDT-BEP20", "LTC"}
                if not expected.issubset(enabled):
                    missing = ", ".join(sorted(expected - set(enabled)))
                    raise KdfBinaryError(f"activation test did not enable: {missing}")
                orders = client.my_orders()
                order_count = _mapping_count(orders, "maker_orders") + _mapping_count(
                    orders, "taker_orders"
                )
                if order_count:
                    raise KdfBinaryError("activation test created unexpected orders")

                return KdfActivationSmokeResult(
                    version=str(version),
                    enabled_coins=enabled,
                    arrr_address=str(wallet["ARRR"]["address"]),
                    evm_address=str(wallet["BNB"]["address"]),
                    balances={
                        ticker: values.get("balance", "0")
                        for ticker, values in wallet.items()
                    },
                    isolated_p2p=True,
                    funds_used=False,
                    orders_created=0,
                    duration_seconds=round(time.monotonic() - started, 3),
                )
        finally:
            if process is not None and process.poll() is None:
                try:
                    client.legacy("stop")
                except (KdfError, OSError, ValueError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


def run_kdf_contract_test(
    *,
    manifest_path: str | Path,
    fixture_path: str | Path,
    coins_path: str | Path,
    timeout: float = 15.0,
) -> KdfContractResult:
    """Start the pinned KDF build with a public, isolated test identity.

    This is deliberately not a generic KDF launcher. It accepts only the public
    contract-test mnemonic and replaces every runtime path and port, so it can
    never start a production wallet by mistake.
    """

    manager = KdfBinaryManager(manifest_path)
    manager.verify()
    fixture = _load_json_object(Path(fixture_path), "KDF contract fixture")
    known_test_seed = (
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )
    if fixture.get("passphrase") != known_test_seed:
        raise KdfBinaryError("contract test refuses any non-public wallet mnemonic")

    pinned_coins = Path(coins_path).resolve()
    if not pinned_coins.is_file():
        raise KdfBinaryError(f"pinned KDF coins file is missing: {pinned_coins}")

    rpc_port = _available_loopback_port()
    p2p_memory_port = _available_loopback_port()
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None

    with TemporaryDirectory(prefix="kdf-mm-contract-") as directory:
        runtime_dir = Path(directory)
        runtime_config = dict(fixture)
        runtime_config.update(
            {
                "rpcip": "127.0.0.1",
                "rpcport": rpc_port,
                "rpc_local_only": True,
                "myipaddr": "127.0.0.1",
                "i_am_seed": True,
                "p2p_in_memory": True,
                "p2p_in_memory_port": p2p_memory_port,
                "seednodes": [],
                "dbdir": str(runtime_dir / "db"),
                "userhome": str(runtime_dir),
            }
        )
        config_path = runtime_dir / "MM2.contract.json"
        config_path.write_text(json.dumps(runtime_config, indent=2), encoding="utf-8")
        log_path = runtime_dir / "kdf-contract.log"
        rpc_url = f"http://127.0.0.1:{rpc_port}"
        client = KdfRpcClient(
            rpc_url=rpc_url,
            userpass=str(runtime_config["rpc_password"]),
            timeout=0.5,
        )
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "MM_CONF_PATH": str(config_path),
            "MM_COINS_PATH": str(pinned_coins),
            "RUST_LOG": "warn",
        }

        try:
            with log_path.open("w+b") as log_stream:
                process = subprocess.Popen(
                    [str(manager.binary_path)],
                    cwd=runtime_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                )
                version = _wait_for_kdf(client, process, log_path, timeout=timeout)
                enabled = client.enabled_coins()
                orders = client.my_orders()

                enabled_coins = _mapping_count(enabled, "coins")
                maker_orders = _mapping_count(orders, "maker_orders")
                taker_orders = _mapping_count(orders, "taker_orders")
                if enabled_coins or maker_orders or taker_orders:
                    raise KdfBinaryError(
                        "isolated contract test started with unexpected coin or order state"
                    )

                return KdfContractResult(
                    version=str(version),
                    rpc_url=rpc_url,
                    enabled_coins=enabled_coins,
                    maker_orders=maker_orders,
                    taker_orders=taker_orders,
                    isolated_p2p=True,
                    duration_seconds=round(time.monotonic() - started, 3),
                )
        finally:
            if process is not None and process.poll() is None:
                try:
                    client.legacy("stop")
                except (KdfError, OSError, ValueError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


def validate_runtime_config(path: str | Path) -> tuple[str, ...]:
    config_path = Path(path)
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (f"cannot read valid JSON config: {exc}",)
    if not isinstance(loaded, Mapping):
        return ("KDF config must be a JSON object",)

    problems: list[str] = []
    passphrase = str(loaded.get("passphrase", ""))
    rpc_password = str(loaded.get("rpc_password", ""))
    known_test_seed = (
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )
    if passphrase == known_test_seed:
        problems.append("passphrase is the public contract-test mnemonic")
    elif not passphrase or "REPLACE" in passphrase.upper() or len(passphrase) < 24:
        problems.append("passphrase is missing, placeholder, or too short")
    if "password" in rpc_password.lower():
        problems.append("rpc_password contains the word 'password', rejected by KDF")
    elif (
        not rpc_password
        or "REPLACE" in rpc_password.upper()
        or len(rpc_password) < 24
        or len(rpc_password) > 32
    ):
        problems.append("rpc_password must contain 24 to 32 characters")
    elif not any(character.isdigit() for character in rpc_password):
        problems.append("rpc_password must contain a digit")
    elif not any("a" <= character <= "z" for character in rpc_password):
        problems.append("rpc_password must contain a lowercase character")
    elif not any("A" <= character <= "Z" for character in rpc_password):
        problems.append("rpc_password must contain an uppercase character")
    elif not any(not character.isalnum() for character in rpc_password):
        problems.append("rpc_password must contain a special character")
    elif any(
        rpc_password[index] == rpc_password[index + 1] == rpc_password[index + 2]
        for index in range(max(0, len(rpc_password) - 2))
    ):
        problems.append("rpc_password cannot repeat one character three times")
    if loaded.get("rpc_local_only") is not True:
        problems.append("rpc_local_only must be true")
    if str(loaded.get("rpcip", "127.0.0.1")) not in {"127.0.0.1", "::1", "localhost"}:
        problems.append("rpcip must be loopback")
    if loaded.get("netid") != 8762:
        problems.append("netid must be 8762 for the selected main network")
    if loaded.get("allow_weak_password") is True:
        problems.append("allow_weak_password must not be true")
    if loaded.get("p2p_in_memory") is True:
        problems.append("p2p_in_memory is for isolated tests, not production")
    if loaded.get("disable_p2p") is not False:
        problems.append("disable_p2p must be explicitly false for a trading node")
    rpc_port = loaded.get("rpcport", 7783)
    if not isinstance(rpc_port, int) or isinstance(rpc_port, bool) or not 1024 <= rpc_port <= 65535:
        problems.append("rpcport must be an integer from 1024 to 65535")
    seednodes = loaded.get("seednodes")
    has_seednodes = isinstance(seednodes, list) and bool(seednodes) and all(
        isinstance(value, str) and value.strip() for value in seednodes
    )
    if loaded.get("i_am_seed") is not True and not has_seednodes:
        problems.append("at least one explicit seednode is required")
    for name in ("dbdir", "userhome"):
        value = loaded.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute():
            problems.append(f"{name} must be an absolute path")
    return tuple(problems)


def _wait_for_kdf(
    client: KdfRpcClient,
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            return client.legacy("version")
        except (KdfError, OSError, ValueError) as exc:
            last_error = exc
            time.sleep(0.1)
    detail = _log_tail(log_path)
    message = "KDF did not expose its loopback RPC before the contract-test timeout"
    if process.poll() is not None:
        message = f"KDF exited during contract test with code {process.returncode}"
    if last_error is not None:
        message += f"; last RPC error: {last_error}"
    if detail:
        message += f"; log tail: {detail}"
    raise KdfBinaryError(message)


def _mapping_count(value: Any, key: str) -> int:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), (Mapping, list)):
        raise KdfBinaryError(f"KDF contract response is missing {key}")
    return len(value[key])


def _coin_tickers(value: Any) -> set[str]:
    if not isinstance(value, Mapping) or not isinstance(value.get("coins"), list):
        raise KdfBinaryError("KDF contract response is missing coins")
    return {
        str(item["ticker"])
        for item in value["coins"]
        if isinstance(item, Mapping) and item.get("ticker")
    }


def _load_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KdfBinaryError(f"cannot read {label}") from exc
    if not isinstance(loaded, Mapping):
        raise KdfBinaryError(f"{label} must be a JSON object")
    return loaded


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _log_tail(path: Path, *, limit: int = 1200) -> str:
    try:
        raw = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").replace("\n", " ").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elf_architecture(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        raise KdfBinaryError("KDF binary is not an ELF64 executable")
    byteorder = "little" if header[5] == 1 else "big" if header[5] == 2 else None
    if byteorder is None:
        raise KdfBinaryError("KDF ELF byte order is invalid")
    machine = int.from_bytes(header[18:20], byteorder=byteorder)
    architectures = {62: "x86_64", 183: "aarch64"}
    return architectures.get(machine, f"elf-machine-{machine}")
