from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


class CoinRegistryError(ValueError):
    pass


class CoinRegistry:
    def __init__(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        self._entries = {str(ticker).strip().upper(): item for ticker, item in entries.items()}

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> "CoinRegistry":
        manifest_file = Path(manifest_path).resolve()
        manifest = _json_object(manifest_file, "coin registry manifest")
        relative = Path(
            str(manifest.get("registry_path") or manifest.get("subset_path", ""))
        )
        if not relative or relative.is_absolute():
            raise CoinRegistryError("coin registry path must be relative")
        registry_path = (manifest_file.parent / relative).resolve()
        try:
            registry_path.relative_to(manifest_file.parent)
        except ValueError as exc:
            raise CoinRegistryError("coin registry path escapes its directory") from exc
        expected_hash = str(
            manifest.get("registry_sha256") or manifest.get("subset_sha256", "")
        ).lower()
        if len(expected_hash) != 64 or _sha256_file(registry_path) != expected_hash:
            raise CoinRegistryError("coin registry SHA-256 mismatch")

        try:
            loaded = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CoinRegistryError("cannot read the pinned coin registry") from exc
        entries = _registry_entries(loaded)
        declared = manifest.get("coins")
        if declared is not None:
            if not isinstance(declared, list) or {
                str(ticker).strip().upper() for ticker in declared
            } != set(entries):
                raise CoinRegistryError("manifest coin list does not match the registry")
        declared_count = manifest.get("coin_count")
        if declared_count is not None and int(declared_count) != len(entries):
            raise CoinRegistryError("manifest coin count does not match the registry")
        return cls(entries)

    @classmethod
    def from_file(cls, coins_path: str | Path) -> "CoinRegistry":
        """Load a user-selected KDF coins file without a project allow-list.

        Production deployments should prefer ``from_manifest`` so the exact
        registry is checksum-pinned. This loader exists for an explicitly
        configured full KDF registry during local development.
        """
        try:
            loaded = json.loads(Path(coins_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CoinRegistryError("cannot read the KDF coin registry") from exc
        return cls(_registry_entries(loaded))

    def entry(self, ticker: str) -> Mapping[str, Any]:
        selected = ticker.strip().upper()
        try:
            return copy.deepcopy(self._entries[selected])
        except KeyError as exc:
            raise CoinRegistryError(
                f"coin is not present in the configured registry: {selected}"
            ) from exc

    def tickers(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def protocol_type(self, ticker: str) -> str:
        selected = ticker.strip().upper()
        protocol = self.entry(selected).get("protocol")
        if not isinstance(protocol, Mapping) or not protocol.get("type"):
            raise CoinRegistryError(f"{selected} has no protocol configuration")
        return str(protocol["type"]).upper()

    def require_automatic_activation(self, ticker: str) -> str:
        selected = ticker.strip().upper()
        coin = self.entry(selected)
        if coin.get("wallet_only") is True or int(coin.get("mm2", 0)) != 1:
            raise CoinRegistryError(f"{selected} is not atomic-swap-capable")
        protocol = self.protocol_type(selected)
        if protocol not in {"UTXO", "ZHTLC", "ETH", "ERC20"}:
            raise CoinRegistryError(
                f"automatic activation for KDF protocol {protocol} is not implemented"
            )
        return protocol

    def dependency_tickers(self, ticker: str) -> tuple[str, ...]:
        selected = ticker.strip().upper()
        coin = self.entry(selected)
        protocol = coin.get("protocol")
        if not isinstance(protocol, Mapping):
            raise CoinRegistryError(f"{selected} has no protocol configuration")
        protocol_data = protocol.get("protocol_data")
        if isinstance(protocol_data, Mapping) and protocol_data.get("platform"):
            return (str(protocol_data["platform"]).strip().upper(),)
        return ()

    def arrr_activation_params(self, *, sync_height: int | None = None) -> dict[str, Any]:
        return self.zhtlc_activation_params("ARRR", sync_height=sync_height)

    def zhtlc_activation_params(
        self, ticker: str, *, sync_height: int | None = None
    ) -> dict[str, Any]:
        selected = ticker.strip().upper()
        coin = self.entry(selected)
        protocol = coin.get("protocol")
        if not isinstance(protocol, Mapping) or protocol.get("type") != "ZHTLC":
            raise CoinRegistryError(f"{selected} is not ZHTLC")
        if coin.get("wallet_only") is True:
            raise CoinRegistryError(f"{selected} is wallet-only")
        electrum = coin.get("electrum")
        lightwalletd = coin.get("light_wallet_d_servers")
        if not isinstance(electrum, list) or not electrum:
            raise CoinRegistryError(f"{selected} has no Electrum servers")
        if not isinstance(lightwalletd, list) or not lightwalletd:
            raise CoinRegistryError(f"{selected} has no lightwalletd servers")
        rpc_data: dict[str, Any] = {
            "electrum_servers": copy.deepcopy(electrum),
            "light_wallet_d_servers": list(map(str, lightwalletd)),
        }
        if sync_height is not None:
            if sync_height <= int(coin.get("checkpoint_height", 0)):
                raise CoinRegistryError(
                    f"{selected} sync height must be above its checkpoint"
                )
            rpc_data["sync_params"] = {"height": sync_height}
        return {
            "mode": {
                "rpc": "Light",
                "rpc_data": rpc_data,
            },
            "scan_blocks_per_iteration": 1000,
            "scan_interval_ms": 100,
        }

    def evm_token_activation_params(self, token_ticker: str) -> dict[str, Any]:
        selected = token_ticker.strip().upper()
        token = self.entry(selected)
        token_protocol = token.get("protocol")
        if not isinstance(token_protocol, Mapping):
            raise CoinRegistryError(f"{selected} has no protocol configuration")
        if token_protocol.get("type") != "ERC20":
            raise CoinRegistryError(
                f"{selected} is not an atomic-swap-capable ERC20-family token"
            )
        if token.get("wallet_only") is True:
            raise CoinRegistryError(f"{selected} is wallet-only")
        protocol_data = token_protocol.get("protocol_data")
        if not isinstance(protocol_data, Mapping) or not protocol_data.get("platform"):
            raise CoinRegistryError(f"{selected} has no platform binding")
        platform_ticker = str(protocol_data["platform"]).strip().upper()
        return self.evm_platform_activation_params(
            platform_ticker, token_tickers=(selected,)
        )

    def evm_token_only_activation_params(self, token_ticker: str) -> dict[str, Any]:
        selected = token_ticker.strip().upper()
        token = self.entry(selected)
        if self.protocol_type(selected) != "ERC20":
            raise CoinRegistryError(f"{selected} is not an ERC20-family token")
        if token.get("wallet_only") is True:
            raise CoinRegistryError(f"{selected} is wallet-only")
        confirmations = int(token.get("required_confirmations", 0))
        if confirmations <= 0:
            raise CoinRegistryError(
                f"{selected} has invalid required confirmations"
            )
        return {
            "ticker": selected,
            "activation_params": {"required_confirmations": confirmations},
        }

    def evm_platform_activation_params(
        self, ticker: str, *, token_tickers: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        selected = ticker.strip().upper()
        platform = self.entry(selected)
        if self.protocol_type(selected) != "ETH":
            raise CoinRegistryError(f"{selected} is not an EVM platform coin")
        if platform.get("wallet_only") is True:
            raise CoinRegistryError(f"{selected} is wallet-only")
        nodes = platform.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise CoinRegistryError(f"{selected} has no RPC nodes")
        activation_nodes: list[dict[str, Any]] = []
        for node in nodes:
            url = node.get("url") if isinstance(node, Mapping) else None
            parsed = urlparse(str(url))
            if parsed.scheme != "https" or not parsed.netloc:
                raise CoinRegistryError(f"{selected} contains an invalid RPC URL")
            activation_node: dict[str, Any] = {"url": str(url)}
            if isinstance(node, Mapping):
                for key in ("ws_url", "komodo_proxy"):
                    if node.get(key) is not None:
                        activation_node[key] = node[key]
            activation_nodes.append(activation_node)
        swap_contract = str(platform.get("swap_contract_address", ""))
        confirmations = int(platform.get("required_confirmations", 0))
        if not swap_contract or confirmations <= 0:
            raise CoinRegistryError(f"{selected} has incomplete EVM swap settings")
        token_requests: list[dict[str, Any]] = []
        for token_ticker in dict.fromkeys(
            item.strip().upper() for item in token_tickers
        ):
            token = self.entry(token_ticker)
            if self.protocol_type(token_ticker) != "ERC20":
                raise CoinRegistryError(f"{token_ticker} is not an ERC20-family token")
            if self.dependency_tickers(token_ticker) != (selected,):
                raise CoinRegistryError(
                    f"{token_ticker} does not belong to EVM platform {selected}"
                )
            if token.get("wallet_only") is True or int(token.get("mm2", 0)) != 1:
                raise CoinRegistryError(f"{token_ticker} is not atomic-swap-capable")
            token_confirmations = int(token.get("required_confirmations", 0))
            if token_confirmations <= 0:
                raise CoinRegistryError(
                    f"{token_ticker} has invalid required confirmations"
                )
            token_swap_contract = str(token.get("swap_contract_address", ""))
            if token_swap_contract and token_swap_contract.lower() != swap_contract.lower():
                raise CoinRegistryError(
                    f"{token_ticker} and {selected} swap contracts do not match"
                )
            token_requests.append(
                {
                    "ticker": token_ticker,
                    "required_confirmations": token_confirmations,
                }
            )
        result: dict[str, Any] = {
            "ticker": selected,
            "nodes": activation_nodes,
            "erc20_tokens_requests": token_requests,
            "swap_contract_address": swap_contract,
            "required_confirmations": confirmations,
            "tx_history": False,
        }
        fallback = platform.get("fallback_swap_contract")
        if fallback:
            result["fallback_swap_contract"] = str(fallback)
        return result

    def utxo_activation_params(self, ticker: str) -> dict[str, Any]:
        selected = ticker.strip().upper()
        coin = self.entry(selected)
        protocol = coin.get("protocol")
        if not isinstance(protocol, Mapping) or protocol.get("type") != "UTXO":
            raise CoinRegistryError(f"{selected} is not a UTXO coin")
        if coin.get("wallet_only") is True or int(coin.get("mm2", 0)) != 1:
            raise CoinRegistryError(f"{selected} is not atomic-swap-capable")
        servers = coin.get("electrum")
        if not isinstance(servers, list) or not servers:
            raise CoinRegistryError(f"{selected} has no Electrum servers")
        selected_servers: list[dict[str, str]] = []
        for server in servers:
            if not isinstance(server, Mapping) or not server.get("url"):
                raise CoinRegistryError(f"{selected} contains an invalid Electrum server")
            protocol_name = str(server.get("protocol", "TCP")).upper()
            if protocol_name not in {"TCP", "SSL"}:
                continue
            selected_servers.append(
                {"url": str(server["url"]), "protocol": protocol_name}
            )
        if not selected_servers:
            raise CoinRegistryError(f"{selected} has no TCP/SSL Electrum servers")
        confirmations = int(coin.get("required_confirmations", 0))
        if confirmations <= 0:
            raise CoinRegistryError(f"{selected} has invalid required confirmations")
        return {
            "coin": selected,
            "servers": selected_servers,
            "required_confirmations": confirmations,
            "requires_notarization": bool(coin.get("requires_notarization", False)),
            "mm2": 1,
            "min_connected": 1,
            "max_connected": min(2, len(selected_servers)),
        }


def _json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoinRegistryError(f"cannot read {label}") from exc
    if not isinstance(loaded, Mapping):
        raise CoinRegistryError(f"{label} must be a JSON object")
    return loaded


def _registry_entries(loaded: Any) -> dict[str, Mapping[str, Any]]:
    if isinstance(loaded, Mapping):
        rows = loaded.items()
    elif isinstance(loaded, list):
        rows = ((None, item) for item in loaded)
    else:
        raise CoinRegistryError("coin registry must be a JSON array or object")
    entries: dict[str, Mapping[str, Any]] = {}
    for declared_ticker, item in rows:
        if not isinstance(item, Mapping) or not isinstance(item.get("coin"), str):
            raise CoinRegistryError("coin registry contains an invalid entry")
        ticker = str(item["coin"]).strip().upper()
        if not ticker:
            raise CoinRegistryError("coin registry contains an empty ticker")
        if declared_ticker is not None and str(declared_ticker).strip().upper() != ticker:
            raise CoinRegistryError("coin registry key does not match its ticker")
        if ticker in entries:
            raise CoinRegistryError(f"duplicate coin registry ticker: {ticker}")
        entries[ticker] = item
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CoinRegistryError("cannot read the pinned coin registry") from exc
    return digest.hexdigest()
