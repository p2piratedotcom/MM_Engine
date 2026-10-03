"""Read-only Spot snapshots for sizing. These NEVER grant live coverage."""
from __future__ import annotations

import threading
import time

from . import load_config, private_client
from ..desktop_coverage import DesktopCoveragePublisher
from ..venues import coverage_asset_key, market_data_key


class PreviewBalances:
    def __init__(self, *, keyring_factory, base_urls=None, client_factory=private_client,
                 clock=time.monotonic):
        self.keyring_factory = keyring_factory
        self.base_urls = base_urls or {}
        self.client_factory = client_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._snapshots = {}

    def read_account(self, venue):
        """Display balances without requiring permission to place hedge orders."""
        config = load_config(venue)
        stage = 'caricamento credenziali'
        try:
            client = self.client_factory(venue, self.keyring_factory(),
                base_url=self.base_urls.get(venue), trading_enabled=False)
            stage = 'sincronizzazione orario'
            client.synchronize_time(max_round_trip_ms=config.time_sync_budget_ms)
            stage = 'lettura account Spot'
            account = client.account(timeout=config.private_read_timeout,
                total_timeout=config.private_read_timeout)
            stage = 'verifica formato saldi'
            return DesktopCoveragePublisher._balances(account, venue=venue,
                require_trading=False)
        except Exception as exc:
            status = getattr(exc, 'status', None)
            detail = f'HTTP {status}' if isinstance(status, int) else type(exc).__name__
            raise ValueError(f'{venue}: {stage} fallita ({detail}). '
                'Verificare Spot Account Read e connessione Tor; riprovare.') from None

    def __call__(self, venue):
        with self._lock:
            previous = self._snapshots.get(venue)
            if previous is not None and self.clock() - previous[0] < 3:
                return previous[1]
            config = load_config(venue)
            try:
                client = self.client_factory(venue, self.keyring_factory(),
                    base_url=self.base_urls.get(venue), trading_enabled=False)
                client.synchronize_time(max_round_trip_ms=config.time_sync_budget_ms)
                symbols = client.self_symbols(timeout=config.private_read_timeout,
                    total_timeout=config.private_read_timeout).get('data')
                if (not isinstance(symbols, list)
                        or any(not isinstance(s, str) for s in symbols)):
                    raise ValueError('simboli Spot autorizzati non validi')
                # Match the live publisher: unsupported symbols do not invalidate
                # the permission set for unrelated, supported hedge pairs.
                symbols = [s for s in symbols if s.isascii() and s.isalnum()]
                observed = self.clock()
                account = client.account(timeout=config.private_read_timeout,
                    total_timeout=config.private_read_timeout)
                balances = DesktopCoveragePublisher._balances(account, venue=venue)
                if self.clock() - observed >= 10:
                    raise ValueError('lettura saldo troppo lenta: riprovare')
            except Exception as exc:
                # Never echo signed URLs, credentials or exchange payloads.
                status = getattr(exc, 'status', None)
                detail = f'HTTP {status}' if isinstance(status, int) else type(exc).__name__
                raise ValueError(f'{venue}: saldo Spot non disponibile ({detail}). '
                    'Controllare chiavi API, permesso Spot Account Read e connessione Tor; riprovare.') from None
            snapshot = {
                'lease_fresh': True, 'live_hedging_enabled': False,
                'source': 'read_only_preview', 'expires_monotonic': observed + 10,
                'free_balances': {coverage_asset_key(venue, asset): str(amount)
                                  for asset, amount in balances.items()},
                'hedge_symbols': [market_data_key(venue, symbol) for symbol in symbols],
            }
            self._snapshots[venue] = (observed, snapshot)
            return snapshot
