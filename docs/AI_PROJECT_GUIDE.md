# MM_Engine: AI and contributor project guide

Read [AGENTS.md](../AGENTS.md) first. Facts were checked on 2026-10-06 against the
source revision there. This guide explains source and contracts; it is not a
funded operator runbook or proof of live acceptance.

## Purpose and system boundary

MM_Engine is the standalone Python market maker extracted from the KDF Market
Maker Bot project. It sizes/prices maker quotes, tracks ownership, reconciles
wallet swaps/orders, checks CEX funding, and journals hedge/rebalance outcomes.
It does not include a KDF binary, wallet assets, funded configuration or keys.

[Wallet](https://github.com/p2piratedotcom/P2Pirate-ALPHA/blob/cheetahdex/AGENTS.md)
owns its KDF/Tor and starts this engine as a foreground child.
[SDK](https://github.com/p2piratedotcom/komodo-defi-sdk-flutter/blob/cheetahdex/AGENTS.md)
provides the wallet's KDF lifecycle/client. Downloadable
[CEX adapters](https://github.com/p2piratedotcom/CEX_configs/blob/main/AGENTS.md)
own native Spot API behavior. [Assets](https://github.com/p2piratedotcom/Assets/blob/main/AGENTS.md)
provides public wallet data; Rust KDF itself is external.

The operator TUI/local service and wallet-service share engine components but
have different lifecycle/authorization surfaces. Wallet-service is attach-only;
operator modes can own/start KDF only under their explicit operator configuration.
Do not reuse a state directory between them.

## Module map

All Python modules below are under `src/kdf_mm/`:

| Concern | Source |
| --- | --- |
| CLI / entry point | `cli.py`, `__main__.py`, `scripts/mm_engine_entrypoint.py` at repo root |
| Wallet bootstrap and restricted API | `wallet_service.py`, `vps_agent.py` |
| KDF HTTP and reconciliation | `kdf.py`, `reconciliation.py`, `kdf_events.py` |
| Quote/strategy math and persistence | `strategy.py`, `strategy_service.py`, `strategy_store.py`, `quote_engine.py` |
| Owned orders and uncertain writes | `ownership.py`, `publication_recovery.py`, `cancellation_recovery.py` |
| Independent public feeds | `public_feed.py`, `market_data.py` |
| Funding/lease/worker | `coverage.py`, `desktop_coverage.py`, `local_worker.py`, `outbox.py` |
| Hedge intents/fills/unwind | `hedging.py`, `basket_hedge.py`, `journal.py`, `hedge_unwind.py` |
| Rebalance policy | `wallet_rebalance.py`, `rebalance.py`, `rebalance_ideal.py`, `rebalance_portfolio.py`, `rebalance_allocation.py`, `rebalance_validation.py`, `rebalance_guard.py` |
| Downloaded plugin protocol | `exchange_plugin_host.py`, `exchanges/plugin_catalog.py`, `exchanges/plugin_client.py` |
| Credentials and diagnostic metadata | `credentials.py`, `network_diagnostics.py` |
| Operator interfaces | `tui.py`, `strategy_tui.py`, `rebalance_tui.py`, optional `gui.py` |

## Wallet protocol and permissions

Detailed contract: [WALLET_INTEGRATION.md](WALLET_INTEGRATION.md). The wallet sends
one bounded UTF-8 JSON bootstrap through stdin, with private state directory,
coin registry, loopback KDF endpoint/password, session token, explicit routing
and opt-in live capabilities. Secrets never belong on the command line or in
copied environment dumps. Keep stdin open. The engine emits `MM_ENGINE_READY`
with protocol 1 and an ephemeral loopback HTTP port.

The GUI bearer token authorizes the restricted wallet `/v1/` API. A separate
worker token is required for signed event delivery/acknowledgment and coverage
lease operations; the GUI token cannot impersonate that worker. `/health` proves
only process life. Wallet mode excludes KDF lifecycle, wallet-send, operator
publication controls and coverage overrides. Its transfers remain disabled.

Only one engine owns a profile directory/KDF wallet. Private state and signing
secrets are persisted with restrictive permissions and process locks. Safe
shutdown reports `MM_ENGINE_STOPPED`, owned orders remaining and any cancel error.
A missing/non-success report is not proof of withdrawal. The wallet must keep
KDF/Tor available when recovery needs them. Source updates do not hot-change a
running executable, and stopping only a standalone TUI is not stopping its service.

## From strategy to maker and hedge

1. Validate activation, exact KDF IDs and explicit Spot mappings. Validate
   configured premium, auto/fixed price/quantity, budgets and venue rules.
2. Preview builds a finite plan from wallet capacity, fresh books/rolling volume,
   fees, hedge minimums/steps/depth and shared reservations. Saving does not grant
   permission to publish; manual start/live controls are separate.
3. Persist publication intent before mutating KDF. Bind the actual order UUID to
   its strategy ID/creation number. Pair alone is not unique: multiple levels can
   share it. Fixed orders do not silently shrink; auto orders remain bounded.
4. Reconciliation distinguishes owned maker swaps, wallet takers, transient swap
   initialization, terminal order history and ambiguous/unknown states.
5. Signed worker/event/lease handling precedes coverage authority. Shared KDF/CEX
   funds and depth are reserved across orders, markets and levels; balance totals
   are not net uncommitted coverage. Missing coverage blocks unsafe quoting.
6. Hedge intents and uncertain CEX writes remain durable. Reconcile the same
   client/native order identity; do not replay, delete the journal or release
   holds because a response was late. A finished flag alone is not successful
   settlement or a verified accounting result.

### Preview funding explanation

When missing CEX funds prevent a valid quantity, the preview explains each
hedge asset separately: free exchange balance, coverage reserved for established
maker UUIDs (with the contributing order count), other pending or unattributed
obligations, net available funds, required funds and the shortage. It names the
quantity used for the funds calculation: Auto below the minimum evaluates the
minimum hedge quantity, while Fixed evaluates the requested quantity. Exact
Decimal values remain available in the message; the diagnostic report adds
`funding_context` without changing sizing or financial authority.

Suggested remedies are funding the selected venue, choosing a supported venue
with the necessary markets, or pausing a contributing maker and repeating the
preview. Pause is not immediate release: withdrawal must be confirmed and swap/
uncertain-operation obligations must be resolved. Increasing quantity alone does
not cure a funding shortage. Minimums, depth, budgets and precision still apply.

### How to interpret state

| Observation | Interpretation |
| --- | --- |
| `PAUSED` / disabled | Not permission to republish; identify operator intent/source |
| `WAITING` | Missing valid conditions, minimum/capacity, feed, reconciliation or cooldown; not necessarily a process failure |
| `STABILIZING` | May retain the same OPEN UUID while awaiting distinct snapshots/interval; not automatically a withdrawal |
| `WRITING` | RPC work in progress; not proof of successful publication |
| `REVIEW_REQUIRED` / GUI `RECOVERING` | Determine durable hold vs automatic pending readback; never bypass ambiguity |
| `OPEN` owned UUID | Published/reconciled ownership evidence; independent freshness and coverage still matter |
| `CANCEL_TIMEOUT_RECOVERED` | Reconciliation result, not a new withdrawal |

A manual pause is not auto-resume authorization. `DELEGATED` recovery intents are
not classified as human pauses solely from a boolean; inspect their source and
owned-order terminal evidence. Count actual OPEN-to-terminal transitions and
republications by UUID/timestamp, not repeated countdown messages or repricing.

Bulk pause also holds disabled rows with pending publication/update intents.
Readback restores persisted minimum volume before binding an order UUID.
See [maker recovery invariants](MAKER_RECOVERY_INVARIANTS.md) for shared-pool
scope and crash/pause regression fixtures.

## Feed and timeout model

Depth and 24-hour metadata use separate read-only plugin readers. The bounded
stdio protocol serializes one client's calls; independent readers prevent slow
metadata/other symbols from blocking depth. They are intentional children, not
necessarily duplicate engines. Original book and volume observation times remain
separate; a slow response must not refresh old metadata's age.

At the reviewed source, book/volume validity in the wallet rebalance/feed path is
10/15 seconds, with a separately bounded analysis window. Do not confuse a
network deadline, market TTL, allocation lifetime, proposal expiry, lease or safety
cooldown. Extending TTLs to suppress withdrawals is not a performance fix.

KDF writes such as `setprice`/`update_maker_order` can time out with an unknown
result while read methods remain fast. A local HTTP timeout does not establish
MEXC failure, a global KDF stall or Tor failure. Reconcile the UUID/history/matches
before cancellation/resumption; do not widen a confirmed price or silently retry.

## Selected-inventory CEX rebalance

Read [SELECTED_REBALANCE.md](SELECTED_REBALANCE.md). The wallet selects maker
configurations, funding assets (including unrelated holdings) and percentages
0..100 in 5% increments. The engine first derives a finite local ideal reference,
then reads current CEX data and computes attainable common funding, respecting
fees/20% reserve, protected unselected makers, rules and market depth.

Zero-balance destination assets can be bought. Their lack of an initial balance
is not permission to omit their hedge requirement. Partial funding does not
change the ideal or authorize below-minimum makers. A debit percentage sets an
absolute durable spending envelope; repeated Analyze cannot reset it. Changing
selection/confirmed Reset is a new authorization. Proposed later buys require
confirmed sale proceeds and a new analysis. Execute submits only the first
approved funded LIMIT step, after idle/reconciliation and current utility/funds/
price/depth revalidation. Unknown/open CEX outcomes keep the hold.

## Source setup and offline verification

Python >=3.11; `pyproject.toml` defines aiohttp plus optional desktop/release extras.
A fresh virtual environment is separate from any operational instance:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m kdf_mm --help
.venv/bin/python -m kdf_mm plugin-capabilities
```

These help/capability commands do not establish trading readiness. Offline
contract references are:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_wallet*.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

The actual PR/main CI workflow runs the latter repository fixtures. Historical
operator docs can reference absent canary configs/vendor assets; do not create or
borrow a real funded profile to make them run. Fixture success does not certify
real-account reads, sandbox acceptance, live orders, swaps or recovery after loss.
Do not run funded CLI tools merely to inspect their help or code paths.

`bash scripts/build-linux.sh` builds a Linux x86-64 PyInstaller candidate and
installs release dependencies in the selected Python environment. It does not
publish it. The tag workflow packages `compatibility.json`, SHA-256 and dependency/
Python license notices into an immutable release. Wallet installers also enforce
expected repository identity, major protocol compatibility and release digests.
Other platforms need their own locks, keyrings, builds and acceptance. Source
license is Unlicense; bundled dependencies retain their own notices.

## Diagnostic access and current limitations

Use [NETWORK_DIAGNOSTICS.md](NETWORK_DIAGNOSTICS.md) and
[FEED_RPC_RECOVERY.md](FEED_RPC_RECOVERY.md). Only allowlisted method/PID/request ID,
timestamps, durations, stage/route, outcomes and statuses belong in network logs.
The optional HTTP phase/host lifecycle extension and sparse incident journals
are documented there with their compatibility and retained-history limits.
The companion universal CEX HTTP core (MEXC 0.1.3, other adapters 0.1.1) measures
resolver queue separately from OS resolution and reuses explicitly routed
sessions/connections. Passive `network_path_sample` (`network_path.py`) correlates
link counters, with explicit unavailable/causality limits.
No keys, tokens, URL/header/body dumps, account data or raw exceptions. Preserve
mode 0600, bounds/rotation and `dropped_records` accounting.

Correlate starts/ends by PID and request ID; unmatched starts can result from
in-flight work, rotation/drop or shutdown. Queue/bootstrap/send/reply/adapter
latencies describe different stages. Adapter duration does not identify Tor,
TCP/TLS or exchange processing individually. `market_book_samples` is persisted
at a sampled rate, not every feed update. Read-only observations must report
coverage gaps, window length, ownership evidence and limits; they cannot justify
changing safety controls.

Catalog presence/capability flags do not prove exchange/funded compatibility.
MEXC/Gate are the original implementation paths; additional adapters have explicit
experimental limits in CEX_configs. The historically named `LocalMexcWorker` now builds clients for configured supported
venues and requires at least one credentialed Spot client. Its old MEXC name/CLI
flag does not establish a MEXC-only key requirement. The selected venue still
needs its own valid credentials, permissions and live readiness. Verify those
through the intended operator flow, not by harvesting secrets from a live process.

## Further reading

- [Wallet contract](WALLET_INTEGRATION.md), [exchange boundary](EXCHANGE_ARCHITECTURE.md)
- [Shared inventory](MULTI_MARKET_SHARED_INVENTORY.md), [coverage/hedging](AUTOMATIC_HEDGE_AND_COVERAGE.md)
- [Selected rebalance](SELECTED_REBALANCE.md), [wallet rebalance](WALLET_REBALANCE.md)
- [Local operator strategies](LOCAL_STRATEGIES.md), [market configuration](MARKET_CONFIGURATION.md)
- [Economic ledger](ECONOMIC_LEDGER.md), [fill import](MEXC_FILL_IMPORT.md)

## A safe starting prompt for an AI contributor

```text
Read AGENTS.md and docs/AI_PROJECT_GUIDE.md at this checkout's revision.
My task is: [describe the requested change].
Identify the component boundary, relevant source/contracts, current limitations,
validation appropriate to this scope, and whether these guides need updating.
Use disposable fixtures; do not start a real wallet/service or submit funded
operations without the operator's explicit authorization.
Report facts separately from assumptions and checks performed from checks not run.
```

## Maintenance and PR handoff

Recheck this guide and `AGENTS.md` in the same PR when architecture, public
contracts, ownership, safety, persistence, routing, supported platforms,
dependencies, setup/tests, generated outputs, provenance or acceptance limits
change. Update linked specifications too when their contract changed. The PR
maintenance checklist requires either the corresponding edits or an explicit
no-update reason; a checkbox alone does not make an old statement true.

Keep version claims dated and tied to source/release evidence. Do not copy a local
runtime path, user account, balance, API credential or private monitoring result
into public guidance. Prefer links to manifests/constants over repeated moving
pins or exhaustive API copies. A cross-repository change needs companion PRs and
compatibility notes; do not assume that merging one repo deploys the whole system.

A useful AI handoff states: repository and commit, requested scope, relevant
modules/contracts, proposed change, risks, exact checks actually performed,
checks not run, companion repositories affected, and guide sections updated.
Implementation, fixture tests, a compatible release, installation, startup,
read-only account validation and funded acceptance are separate milestones.


## Optional hedging / shared funding candidate

See [OPTIONAL_HEDGING_SHARED_COVERAGE.md](OPTIONAL_HEDGING_SHARED_COVERAGE.md)
for immutable creation choices, migration defaults, public-price-only maker
flows, UUID/intent/swap reservations, selective safety reductions and attribution
of stale/unknown funds. `optional_hedging: 1` and `shared_coverage: 1` are additive
capabilities. Native KDF is unchanged. Rebalance snapshot/custom/live execution
is not implemented by this candidate; only eligibility compatibility is added.
