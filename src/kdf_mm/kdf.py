from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping

from .http import JsonTransport, TransportError, UrllibJsonTransport


class KdfError(RuntimeError):
    def __init__(self, message: str, *, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class KdfOrdersDisabled(KdfError):
    pass


class KdfPreflightError(KdfError):
    """setprice was not sent; unlike a write timeout, retrying a fresh plan is safe."""


class KdfRpcClient:
    def __init__(
        self,
        *,
        rpc_url: str,
        userpass: str,
        transport: JsonTransport | None = None,
        orders_enabled: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not rpc_url:
            raise ValueError("rpc_url is required")
        if not userpass:
            raise ValueError("userpass is required")
        self.rpc_url = rpc_url
        self.userpass = userpass
        self.transport = transport or UrllibJsonTransport()
        self.orders_enabled = orders_enabled
        self.timeout = timeout

    def legacy(self, method: str, **params: object) -> Any:
        payload = {"userpass": self.userpass, "method": method, **params}
        return self._post(payload)

    def v2(self, method: str, params: Mapping[str, object], *, request_id: int = 0) -> Any:
        payload = {
            "mmrpc": "2.0",
            "userpass": self.userpass,
            "method": method,
            "params": dict(params),
            "id": request_id,
        }
        return self._post(payload)

    def enable_z_coin(self, *, ticker: str, activation_params: Mapping[str, object]) -> Any:
        return self.v2(
            "task::enable_z_coin::init",
            {"ticker": ticker, "activation_params": dict(activation_params)},
        )

    def enable_z_coin_status(self, task_id: int, *, forget_if_finished: bool = False) -> Any:
        return self.v2(
            "task::enable_z_coin::status",
            {"task_id": task_id, "forget_if_finished": forget_if_finished},
        )

    def cancel_enable_z_coin(self, task_id: int) -> Any:
        return self.v2("task::enable_z_coin::cancel", {"task_id": task_id})

    def enable_evm_with_tokens(self, activation_params: Mapping[str, object]) -> Any:
        return self.v2("task::enable_eth::init", activation_params)

    def enable_erc20(self, activation_params: Mapping[str, object]) -> Any:
        return self.v2("enable_erc20", activation_params)

    def enable_evm_with_tokens_status(
        self, task_id: int, *, forget_if_finished: bool = False
    ) -> Any:
        return self.v2(
            "task::enable_eth::status",
            {"task_id": task_id, "forget_if_finished": forget_if_finished},
        )

    def cancel_enable_evm_with_tokens(self, task_id: int) -> Any:
        return self.v2("task::enable_eth::cancel", {"task_id": task_id})

    def enabled_coins(self) -> Any:
        return self.v2("get_enabled_coins", {})

    def disable_coin(self, ticker: str) -> Any:
        selected = ticker.strip().upper()
        if not selected:
            raise ValueError("coin ticker is required")
        return self.legacy("disable_coin", coin=selected)

    def balance(self, coin: str) -> Any:
        return self.legacy("my_balance", coin=coin)

    def electrum(
        self,
        *,
        coin: str,
        servers: list[dict[str, str]],
        required_confirmations: int,
        requires_notarization: bool,
        mm2: int = 1,
        min_connected: int = 1,
        max_connected: int = 2,
    ) -> Any:
        return self._post(
            {
                "userpass": self.userpass,
                "method": "electrum",
                "coin": coin,
                "servers": servers,
                "required_confirmations": required_confirmations,
                "requires_notarization": requires_notarization,
                "mm2": mm2,
                "min_connected": min_connected,
                "max_connected": max_connected,
            },
            preserve_legacy_object=True,
        )

    def max_maker_volume(self, coin: str) -> Any:
        return self.v2("max_maker_vol", {"coin": coin})

    def trade_preimage(
        self,
        *,
        base: str,
        rel: str,
        price: Decimal,
        volume: Decimal | None = None,
        maximum: bool = False,
    ) -> Any:
        params: dict[str, object] = {
            "base": base,
            "rel": rel,
            "swap_method": "setprice",
            "price": str(price),
            "max": maximum,
        }
        if volume is not None:
            params["volume"] = str(volume)
        return self.v2("trade_preimage", params)

    def set_price(
        self,
        *,
        base: str,
        rel: str,
        price: Decimal,
        volume: Decimal,
        min_volume: Decimal | None = None,
        save_in_history: bool = True,
        allowed_existing_uuids: tuple[str, ...] = (),
        before_send=None,
    ) -> Any:
        self._require_orders_enabled()
        if price <= 0 or volume <= 0:
            raise ValueError("KDF price and volume must be positive")
        params: dict[str, object] = {
            "base": base,
            "rel": rel,
            "price": str(price),
            "volume": str(volume),
            "save_in_history": save_in_history,
        }
        if min_volume is not None:
            params["min_volume"] = str(min_volume)
        # A lost HTTP response is not evidence that the write failed. Snapshot
        # UUIDs before sending, and never retry setprice after an uncertain write.
        try:
            before = self._maker_order_snapshot()
            if any(o.get("base") == base and o.get("rel") == rel and uid not in allowed_existing_uuids
                   for uid, o in before.items()):
                raise KdfError("existing KDF order for this direction requires reconciliation")
            params["cancel_previous"] = False
            if before_send is not None:
                before_send(before)
        except Exception as exc:
            raise KdfPreflightError(f'KDF preflight: {exc}; setprice non inviato') from exc
        try:
            result = self.legacy("setprice", **params)
            if not isinstance(result, Mapping) or not result.get("uuid"):
                raise KdfError("KDF setprice response omitted UUID")
            return result
        except KdfError as original:
            try:
                after = self._maker_order_snapshot()
                candidates = [o for uuid, o in after.items() if uuid not in before
                              and o.get("base") == base and o.get("rel") == rel]
                if len(candidates) == 1:
                    order = candidates[0]
                    # Do not adopt an already matched/partially consumed order:
                    # it needs explicit reconciliation of swap ownership too.
                    if (Decimal(str(order.get("price"))) == price
                            and Decimal(str(order.get("max_base_vol"))) == volume
                            and Decimal(str(order.get("available_amount"))) == volume
                            and not order.get("matches") and not order.get("started_swaps")
                            and (min_volume is None or Decimal(str(order.get("min_base_vol"))) == min_volume)):
                        return order
            except Exception:
                pass  # Preserve uncertainty; the caller must remain fail-closed.
            raise original

    def _maker_order_snapshot(self, *, timeout: float | None = None) -> dict[str, Mapping[str, Any]]:
        payload = (self.my_orders() if timeout is None else
                   self._post({'userpass': self.userpass, 'method': 'my_orders'}, timeout=timeout))
        orders = payload.get("maker_orders") if isinstance(payload, Mapping) else None
        if not isinstance(orders, Mapping) or any(
            not isinstance(uuid, str) or not isinstance(order, Mapping)
            or order.get("uuid") != uuid for uuid, order in orders.items()
        ):
            raise KdfError("invalid KDF maker order snapshot")
        return dict(orders)

    def update_maker_order(
        self,
        *,
        order_uuid: str,
        new_price: Decimal | None = None,
        volume_delta: Decimal | None = None,
        min_volume: Decimal | None = None,
    ) -> Any:
        self._require_orders_enabled()
        params: dict[str, object] = {"uuid": order_uuid}
        if new_price is not None:
            if new_price <= 0:
                raise ValueError("new_price must be positive")
            params["new_price"] = str(new_price)
        if volume_delta is not None:
            params["volume_delta"] = str(volume_delta)
        if min_volume is not None:
            params["min_volume"] = str(min_volume)
        return self.legacy("update_maker_order", **params)

    def cancel_order(self, order_uuid: str) -> Any:
        self._require_orders_enabled()
        return self.legacy("cancel_order", uuid=order_uuid)

    def my_orders(self) -> Any:
        return self.legacy("my_orders")

    def order_status(self, order_uuid: str) -> Any:
        if not order_uuid:
            raise ValueError("order UUID is required")
        return self.legacy("order_status", uuid=order_uuid)

    def active_swaps(self, *, include_status: bool = True) -> Any:
        return self.v2("active_swaps", {"include_status": include_status})

    def recent_swaps(self, *, limit: int = 100) -> Any:
        return self.v2(
            "my_recent_swaps",
            {"limit": limit, "page_number": 1, "from_uuid": None},
        )

    def swap_status(self, swap_uuid: str) -> Any:
        if not swap_uuid:
            raise ValueError("swap UUID is required")
        return self.legacy("my_swap_status", params={"uuid": swap_uuid})

    def enable_order_status_stream(self, client_id: int) -> Any:
        return self.v2("stream::order_status::enable", {"client_id": client_id})

    def enable_swap_status_stream(self, client_id: int) -> Any:
        return self.v2("stream::swap_status::enable", {"client_id": client_id})

    def _require_orders_enabled(self) -> None:
        if not self.orders_enabled:
            raise KdfOrdersDisabled("KDF order mutations are disabled")

    def _post(
        self,
        payload: Mapping[str, object],
        *,
        preserve_legacy_object: bool = False,
        timeout: float | None = None,
    ) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            response = self.transport.request(
                method="POST",
                url=self.rpc_url,
                headers={"Content-Type": "application/json"},
                body=body,
                timeout=self.timeout if timeout is None else min(self.timeout, timeout),
            )
        except TransportError as exc:
            raise KdfError(f"KDF RPC {payload.get('method', '?')}: {exc}", payload=exc.payload) from exc

        if not isinstance(response, Mapping):
            raise KdfError("KDF returned a non-object response", payload=response)
        if response.get("error") is not None:
            error = response["error"]
            message = error.get("message", str(error)) if isinstance(error, Mapping) else str(error)
            raise KdfError(message, payload=response)
        # Electrum activation is a legacy exception: useful fields are top-level
        # beside the scalar `result: "success"` status.
        if preserve_legacy_object:
            return response
        return response.get("result", response)
