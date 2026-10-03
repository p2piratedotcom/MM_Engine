"""Synthetic third exchange proves extensibility without registry edits or trading."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile

from kdf_mm.credentials import LinuxSecretService, MexcCredentials
from kdf_mm.exchanges import (
    configure_plugins,
    create_client,
    load_config,
    supported_venues,
)
from kdf_mm.exchanges.plugin_catalog import verify_catalog
from kdf_mm.exchanges.plugin_client import close_plugins
from kdf_mm.mexc import MexcError
from kdf_mm.venues import coverage_asset_key, normalize_cex

ADAPTER = """
from dataclasses import dataclass
@dataclass
class TimeSync:
    server_time_ms: int = 1
    local_midpoint_ms: int = 1
    offset_ms: int = 0
    round_trip_ms: int = 0
class Client:
    def __init__(self, options): self.options=options
    def __getattr__(self,name): return lambda *args,**kwargs: {}
    def account(self,**kwargs):
        if self.options.get('api_key')=='slow-fixture':
            import time
            time.sleep(4)
        if self.options.get('api_key') == 'error-fixture':
            raise ValueError('must never echo fixture-secret or signed URL')
        return {'accountType':'SPOT','canTrade':False,'balances':[{'asset':'DEMO','free':'2','locked':'0'}]}
    def synchronize_time(self,**kwargs): return TimeSync()
    def place_limit_order(self,**kwargs):
        import os
        if self.options.get('api_key')=='crash-fixture': os._exit(7)
        if self.options.get('api_key')=='encoding-fixture': return {'invalid':float('nan')}
        return {'clientOrderId':kwargs['client_order_id'],'status':'NEW'}
def create_client(config,*,state_dir=None,**kwargs): return Client(kwargs)
"""


def make_catalog(root):
    root = Path(root)
    directory = root / "plugins" / "demo"
    directory.mkdir(parents=True)
    config = dict(
        schema=1,
        protocol="p2pirate-spot-v1",
        version="0.1.0",
        venue="DEMO",
        display_name="Demo Spot",
        base_url="https://exchange.example/api",
        legacy_keys=False,
        private_read_timeout=8,
        time_sync_budget_ms=12000,
        recv_window_ms=20000,
        timestamp_error_codes=[],
        symbols_error_codes=[],
        taker_fee="0.001",
        credential_fields=["api_key", "api_secret"],
        settings={},
    )
    (directory / "config.json").write_text(json.dumps(config))
    with ZipFile(directory / "adapter.zip", "w") as bundle:
        bundle.writestr("cex_plugin/__init__.py", "")
        bundle.writestr("cex_plugin/adapter.py", ADAPTER)
        bundle.writestr(
            "cex_plugin/http.py", "def configure_wallet_proxy(proxy): pass\n"
        )
        bundle.writestr(
            "cex_plugin/models.py",
            'from enum import StrEnum\nclass HedgeSide(StrEnum):\n BUY="BUY"\n SELL="SELL"\n',
        )
    (root / "LICENSE").write_text("Unlicense test fixture")
    h = lambda path: hashlib.sha256((root / path).read_bytes()).hexdigest()
    manifest = dict(
        schema=1,
        protocol=1,
        license_sha256=h("LICENSE"),
        plugins=[
            dict(
                venue="DEMO",
                version="0.1.0",
                config="plugins/demo/config.json",
                adapter="plugins/demo/adapter.zip",
                config_sha256=h("plugins/demo/config.json"),
                adapter_sha256=h("plugins/demo/adapter.zip"),
            )
        ],
    )
    (root / "catalog.json").write_text(json.dumps(manifest))
    return manifest, config


class SpotPluginProtocolTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest, self.config = make_catalog(self.root)
        self.addCleanup(configure_plugins, None)
        self.addCleanup(close_plugins)

    def configure(self):
        configure_plugins(str(self.root))

    def test_unknown_exchange_is_loaded_without_changing_engine_registry(self):
        self.configure()
        self.assertEqual(supported_venues(), ("DEMO",))
        self.assertEqual(normalize_cex("demo"), "DEMO")
        self.assertEqual(coverage_asset_key("DEMO", "USDT"), "DEMO:USDT")
        self.assertEqual(load_config("DEMO").base_url, self.config["base_url"])
        client = create_client("DEMO")
        self.assertEqual(client.account()["balances"][0]["free"], "2")
        self.assertEqual(client.synchronize_time().offset_ms, 0)
        self.assertIsNotNone(
            client._process.poll()
            if client._process.poll() is not None
            else client._process.pid
        )

    def test_read_only_client_cannot_send_real_orders(self):
        self.configure()
        client = create_client("DEMO")
        with self.assertRaises(MexcError):
            client.place_limit_order(symbol="BTCUSDT", client_order_id="fixture")
        self.assertIsNone(client._process)

    def test_error_does_not_reflect_credentials_or_remote_payloads(self):
        self.configure()
        client = create_client(
            "DEMO", api_key="error-fixture", api_secret="fixture-secret"
        )
        with self.assertRaises(MexcError) as error:
            client.account()
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("URL", str(error.exception))

    def test_clients_are_reused_and_obsolete_credentials_are_closed(self):
        self.configure()
        first = create_client("DEMO", api_key="old")
        first.account()
        process = first._process
        self.assertIs(first, create_client("DEMO", api_key="old"))
        second = create_client("DEMO", api_key="new")
        self.assertIsNot(first, second)
        self.assertIsNotNone(process.poll())

    def test_shutdown_reaps_adapter_child(self):
        self.configure()
        client = create_client("DEMO")
        client.account()
        process = client._process
        close_plugins()
        self.assertIsNotNone(process.poll())

    def test_bad_checksum_protocol_or_path_never_falls_back_to_builtins(self):
        for key, value in [
            ("adapter_sha256", "0" * 64),
            ("config", "../../private.json"),
        ]:
            manifest = json.loads(json.dumps(self.manifest))
            manifest["plugins"][0][key] = value
            (self.root / "catalog.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                self.configure()
        manifest = dict(self.manifest, protocol=2)
        (self.root / "catalog.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.configure()

    def test_symlinks_are_rejected(self):
        path = self.root / "plugins/demo/config.json"
        path.unlink()
        path.symlink_to(self.root / "LICENSE")
        with self.assertRaises(ValueError):
            verify_catalog(self.root)

    def test_public_configuration_rejects_embedded_secrets(self):
        self.config["settings"] = {"api_secret": "fixture"}
        path = self.root / "plugins/demo/config.json"
        path.write_text(json.dumps(self.config))
        self.manifest["plugins"][0]["config_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        (self.root / "catalog.json").write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            self.configure()

    def test_keyring_namespaces_preserve_mexc_gate_and_accept_future_exchanges(self):
        calls = []
        from types import SimpleNamespace

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="fixture-value\n", stderr="")

        keys = LinuxSecretService(executable="/fixture/secret-tool", runner=runner)
        for venue, prefix in [("MEXC", ""), ("GATE", "gate-"), ("DEMO", "demo-")]:
            keys.store(venue, MexcCredentials("fixture-key", "fixture-secret"))
            keys.load(venue)
            self.assertEqual(calls[-2][-1], prefix + "api-key")
            self.assertEqual(calls[-1][-1], prefix + "api-secret")

    def test_ambiguous_write_never_replays_and_reaps_crashed_child(self):
        self.configure()
        for key in ("crash-fixture", "encoding-fixture"):
            client = create_client("DEMO", api_key=key, trading_enabled=True)
            with self.assertRaises(MexcError) as error:
                client.place_limit_order(symbol="DEMOUSDT", client_order_id="fixture")
            self.assertTrue(error.exception.execution_unknown)
            self.assertEqual(client._sequence, 1)  # No second submission.
            if key == "crash-fixture":
                self.assertIsNone(client._process)

    def test_invalid_state_directory_does_not_replace_valid_snapshot(self):
        self.configure()
        with self.assertRaises(ValueError):
            configure_plugins(str(self.root), state_dir="relative")
        self.assertEqual(supported_venues(), ("DEMO",))

    def test_private_adapter_state_is_wallet_scoped(self):
        from kdf_mm.exchanges.plugin_catalog import plugin_state_directory

        state = self.root / "wallet"
        state.mkdir(mode=0o700)
        configure_plugins(str(self.root), state_dir=str(state))
        path = Path(plugin_state_directory("DEMO"))
        self.assertEqual(path, state / "exchange-state/demo")
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(path.parent.stat().st_mode & 0o077, 0)

    def test_strategy_service_does_not_inject_mexc_into_other_catalog(self):
        from unittest.mock import MagicMock
        from kdf_mm.strategy_service import StrategyService

        self.configure()
        controller = MagicMock()
        store = MagicMock()
        store.rows.return_value = []
        client = create_client("DEMO")
        service = StrategyService(
            controller=controller,
            store=store,
            feed_group=None,
            public_clients={"DEMO": client},
            reconciliation=None,
            repricing=None,
        )
        self.assertEqual(service.public_clients, {"DEMO": client})
        self.assertIs(service.public_client, client)

    def test_read_timeout_is_bounded_and_reaps_plugin(self):
        import time

        self.configure()
        client = create_client("DEMO", api_key="slow-fixture", timeout=0.01)
        started = time.monotonic()
        with self.assertRaises(MexcError) as error:
            client.account()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(error.exception.execution_unknown)
        self.assertIsNone(client._process)

    def test_protocol_rejects_nonfinite_books_and_invalid_clock_types(self):
        from kdf_mm.exchanges.plugin_client import decode

        for value in [
            {
                "type": "OrderBook",
                "value": {
                    "bids": [
                        {
                            "type": "PriceLevel",
                            "value": {"price": "Infinity", "quantity": "1"},
                        }
                    ],
                    "asks": [],
                    "observed_at_ms": 1,
                },
            },
            {
                "type": "TimeSync",
                "value": {
                    "server_time_ms": 1,
                    "local_midpoint_ms": 1,
                    "offset_ms": 0,
                    "round_trip_ms": "0",
                },
            },
        ]:
            with self.assertRaises(ValueError):
                decode(value)
