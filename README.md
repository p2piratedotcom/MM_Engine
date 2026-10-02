# MM_Engine

Standalone P2Pirate market maker engine extracted from the KDF Market Maker
Bot project. It creates KDF maker orders using MEXC or Gate spot data, adjusts
quotes and available amounts, and records CEX hedge intents and outcomes. The
wallet remains a separate client; KDF remains a separate executable.

## Components

| Component | Location | Role |
| --- | --- | --- |
| Trading and risk engine | `src/kdf_mm/` | Strategies, quote sizing, ownership, reconciliation, coverage and hedge journal. |
| Local service | `src/kdf_mm/vps_agent.py` | Authenticated local API and workers. |
| TUI | `src/kdf_mm/tui.py`, `strategy_tui.py` | Existing operator client. |
| Wallet adapter | `src/kdf_mm/wallet_service.py` | Attach-only entry point for P2Pirate GUI. |
| Reference docs | `docs/` | Strategy semantics and operational safeguards. |

The package does **not** contain KDF, wallet assets, a funded profile or CEX
credentials. KDF and the wallet have their own repositories and releases.

## Current operator workflow

Python 3.11 or newer is required for source operation. Install in a virtual
environment with `python -m pip install -e .`. The existing TUI workflow is
documented in [local strategies](docs/LOCAL_STRATEGIES.md). The wallet adapter
protocol is in [the integration contract](docs/WALLET_INTEGRATION.md).

Trading writes, auto hedge, live CEX trading and transfers start disabled.
Review orders, swaps and the persistent hedge journal before any funded use.
Only one engine instance may own a given state directory and KDF wallet.

## Distribution

Desktop release archives should include the Python runtime and dependencies,
be built separately for each supported platform, and be published with source
revision, dependency notices, hashes and a signed compatibility manifest.
Until those releases and the wallet integration are available, this source
checkout is an operator tool, not an automatic wallet download.

## License

MM_Engine's own source is released under [The Unlicense](LICENSE).
Third-party dependencies and KDF retain their respective licenses.
