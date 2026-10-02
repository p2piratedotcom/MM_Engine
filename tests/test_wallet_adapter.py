"""Wallet adapter contract tests; no KDF, CEX credentials or funded orders."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.request import Request

from kdf_mm.http import _open, configure_wallet_proxy
from kdf_mm.vps_agent import WALLET_GET_PATHS, WALLET_POST_PATHS
from kdf_mm.wallet_service import _settings, _state_secrets


class WalletAdapterTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(configure_wallet_proxy, None)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.coins = self.root / "coins.json"
        self.coins.write_text(json.dumps([{"coin": "ARRR"}]), encoding="utf-8")
        self.bootstrap = {
            "state_dir": str(self.state),
            "coin_registry_path": str(self.coins),
            "kdf_rpc_url": "http://127.0.0.1:7783",
            "kdf_rpc_userpass": "test-only-rpc-password",
            "agent_token": "x" * 48,
            "network_mode": "tor",
            "tor_http_proxy": "http://127.0.0.1:39999",
        }

    def test_preview_attaches_to_wallet_kdf_without_trading_or_transfers(self):
        settings, profile, with_cex = _settings(self.bootstrap)
        self.assertEqual(settings.kdf_rpc_url, "http://127.0.0.1:7783")
        self.assertFalse(settings.manage_kdf)
        self.assertFalse(settings.kdf_order_writes)
        self.assertFalse(settings.live_trading)
        self.assertFalse(settings.auto_hedge)
        self.assertFalse(settings.live_transfers)
        self.assertFalse(with_cex)
        self.assertEqual(profile, "default")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_live_flags_require_cex_worker_and_hedging(self):
        for flags in (
            {"kdf_order_writes": True},
            {"auto_hedge": True},
            {"cex_trading": True},
        ):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                _settings({**self.bootstrap, "live": flags})
        settings, _, with_cex = _settings({
            **self.bootstrap,
            "with_cex": True,
            "live": {
                "kdf_order_writes": True,
                "auto_hedge": True,
                "cex_trading": True,
            },
        })
        self.assertTrue(with_cex)
        self.assertTrue(settings.kdf_order_writes)
        self.assertFalse(settings.live_transfers)

    def test_remote_kdf_and_proxy_bypass_are_rejected(self):
        bad = (
            {"kdf_rpc_url": "http://192.0.2.1:7783"},
            {"kdf_rpc_url": "http://user:secret@127.0.0.1:7783"},
            {"network_mode": "tor", "tor_http_proxy": "http://192.0.2.1:8080"},
            {"network_mode": "direct", "tor_http_proxy": "http://127.0.0.1:39999"},
        )
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                _settings({**self.bootstrap, **change})

    def test_signing_secrets_are_private_and_persistent(self):
        _settings(self.bootstrap)
        first = _state_secrets(self.state)
        self.assertEqual(first, _state_secrets(self.state))
        secret_file = self.state / "service-secrets.json"
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        os.chmod(secret_file, 0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            _state_secrets(self.state)

    def test_wallet_api_excludes_fund_movements_and_process_controls(self):
        self.assertIn("/v1/strategies/preview", WALLET_POST_PATHS)
        self.assertIn("/v1/reconciliation", WALLET_GET_PATHS)
        self.assertIn("/v1/engine/shutdown", WALLET_POST_PATHS)
        self.assertNotIn("/v1/kdf/stop", WALLET_POST_PATHS)
        self.assertNotIn("/v1/orders/publish", WALLET_POST_PATHS)
        self.assertFalse(any(path.startswith("/v1/wallet/send/")
                             for path in WALLET_POST_PATHS))

    def test_tor_route_uses_proxy_without_direct_fallback(self):
        configure_wallet_proxy("http://127.0.0.1:39999")
        opener = Mock()
        opener.open.side_effect = OSError("proxy unavailable")
        with patch("kdf_mm.http.build_opener", return_value=opener) as build, \
                patch("kdf_mm.http.urlopen") as direct:
            with self.assertRaises(OSError):
                _open(Request("https://api.mexc.com/api/v3/time"), timeout=1)
            proxy_handler = build.call_args.args[0]
            self.assertEqual(proxy_handler.proxies["https"],
                             "http://127.0.0.1:39999")
            direct.assert_not_called()
