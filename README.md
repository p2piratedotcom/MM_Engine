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
The standalone operator commands inherited from the TUI need an external KDF
binary/configuration and coin registry supplied by the operator. The legacy
`vendor/` and funded runtime files are intentionally absent from this repo;
the wallet adapter instead attaches to the wallet's KDF 2.7 and passes its
current coin registry at launch.

The wallet adapter's offline contract tests run with
`python -m unittest discover -s tests -p test_wallet_adapter.py -v` and on
every pull request. The broader TUI test suite still relies on fixtures and
runtime assets from its original project; it is not a release gate here.

Trading writes, auto hedge, live CEX trading and transfers start disabled.
Review orders, swaps and the persistent hedge journal before any funded use.
Only one engine instance may own a given state directory and KDF wallet.

## Distribution

The Linux x86-64 release workflow builds a standalone binary with its Python
runtime and writes `compatibility.json` with the source revision, KDF 2.7 and
wallet protocol requirements and binary SHA-256. A `vX.Y.Z` tag publishes both
assets, plus third-party dependency and Python license notices, as an
immutable GitHub release. Repository release immutability must
remain enabled; otherwise the wallet refuses the download. The wallet checks
the repository identity, release immutability, both GitHub asset digests and
the manifest before making the binary executable. Other desktop platforms
will need their own build and compatibility assets.

This source checkout remains an operator tool until a compatible release is
published. The wallet cannot enable live trading merely by installing it.

## License

MM_Engine's own source is released under [The Unlicense](LICENSE).
Third-party dependencies and KDF retain their respective licenses.
