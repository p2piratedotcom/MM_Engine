# P2Pirate wallet integration contract (protocol 1)

## Process and ownership

`mm-engine wallet-service` is a separate Linux process started only after the
wallet has signed in and its KDF 2.7 instance is available. The wallet owns
the KDF and Tor processes. The engine connects to the existing loopback KDF
RPC and must never start, stop or restart it. One engine process owns a wallet
profile's state directory and maker orders. The legacy KDF market maker bot
must be stopped before wallet mode starts. Existing maker orders are not
silently adopted or canceled.

The wallet writes exactly one UTF-8 JSON line to the child's stdin and keeps
the pipe open. Do not pass the KDF RPC password, wallet password, seed, API
keys or bearer token on the command line or in environment variables. Example
with placeholder values:

```json
{"state_dir":"/absolute/private/profile/mm-engine","coin_registry_path":"/absolute/path/to/coins.json","kdf_rpc_url":"http://127.0.0.1:7783","kdf_rpc_userpass":"<secret>","agent_token":"<random-32+-characters>","network_mode":"tor","tor_http_proxy":"http://127.0.0.1:39999","with_cex":false}
```

`network_mode` defaults to `tor` and requires the wallet's local Tor HTTP
bridge. The explicit `direct` mode requires no proxy. A failed Tor route must
not fall back to a direct connection. The engine routes outbound MEXC/Gate
HTTP through this bridge; local KDF and engine RPC stay on loopback.

Optional `markets` is a non-empty list of KDF market IDs. Optional
`cex_profile` selects the Linux Secret Service profile. `with_cex` starts the
local CEX worker and requires locally stored MEXC credentials; Gate credentials
are optional. For preview, keep it false. All `live` flags default to false:
`kdf_order_writes`, `auto_hedge`, `cex_trading`. The wallet must expose these
through a separate, confirmed live-trading flow; enabling the feature alone
cannot turn them on. Transfers remain disabled in wallet mode.

After setup the child writes one line beginning `MM_ENGINE_READY ` followed by
JSON `{"protocol":1,"port":NNNN}`. The GUI must reject an unknown protocol.
The local server binds `127.0.0.1` on an ephemeral port and requires
`Authorization: Bearer <agent_token>` on all `/v1/*` calls. `GET /health`
reports only that the process is alive; it is not a readiness or trading
authorization signal. The process exits gracefully when stdin closes or after
an authenticated `POST /v1/engine/shutdown` with `{}`.

On a normal shutdown it writes `MM_ENGINE_STOPPED ` followed by JSON with
`orders_remaining` and `cancel_error`. The wallet must treat a missing report,
a nonzero count, or a non-null error as a failed shutdown and keep KDF and Tor
running so the operator can recover. Before shutdown, it must check
`/v1/reconciliation` for `active_owned_swaps` and block logout/close while a
swap is still in progress.

## Wallet API

The allowlist in `vps_agent.py` limits wallet mode to strategy previews and
management, market/coin/status reads, owned-order status and reconciliation.
The authenticated credential endpoints report whether MEXC and Gate secrets
exist in the Linux Secret Service and accept an explicit `STORE MEXC` or
`STORE GATE` confirmation. They never return or log the secret values. A
service restart is required after configuring credentials before the CEX
worker can use them.
It hides wallet-send, KDF lifecycle, coin activation, manual order publication,
legacy repricing controls and coverage overrides. Unrecognized endpoints
return 404 even when the caller has the session token. The `/v1/capabilities`
response declares `protocol: 1` and `kdf_owner: wallet`.

The engine's state directory contains ownership, strategy, outbox, coverage
and hedge journal SQLite files plus two persistent signing secrets. It must be
private to the OS user (0700); signing secrets must be 0600. A lock prevents a
second wallet service from using the same directory. The wallet must keep
this directory across application upgrades. It must not share a state
directory with the standalone TUI operator instance.

## Stop and recovery

Shutdown stops repricing and attempts to cancel the engine's owned orders.
If cancellation fails, the error needs to be shown to the user; a process
exit alone does not prove orders are withdrawn. Pending swaps and hedge journal
entries remain persisted for recovery and reconciliation on the next launch.
Until a background KDF/Tor/engine ownership model is implemented, wallet-mode
trading is foreground-only. The GUI must warn about unresolved swaps before
closing, and should not claim that an in-progress hedge is complete.

## Compatibility and release

### Wallet feed initialization

Wallet mode starts with an explicitly allowed empty feed group. Stored
strategies (including paused ones) register their own markets, venue-qualified
stores and feeds before the service becomes ready. With no strategies, no
exchange feed runs. The controller retains its signing/clock template but drops
the CLI's predefined markets/stores so active wallet coins cannot subscribe to
unregistered feeds, and MEXC strategies cannot reuse a store with no feed.
Standalone CLI mode still requires a nonempty initial feed group. This fixes
the v0.2.0 wallet startup failure `at least one public feed is required`.

Local diagnosis on 2026-10-04 reproduced that exception using the released
v0.2.0 Linux binary with disposable credentials and inaccessible loopback
RPC/proxy endpoints. The rebuilt candidate reached `MM_ENGINE_READY`, loaded
one copied saved strategy, reported all seven installed venues with
`live_enabled: false`, and shut down with exit code zero. This confirms startup
and saved configuration restoration, not exchange account access or live orders.

Protocol major `1` is fixed for this first adapter. A release includes
`compatibility.json` with the engine version, source commit, platform,
architecture, wallet protocol, KDF 2.7 requirement and SHA-256 of the binary.
The wallet pins this repository's numeric GitHub ID, accepts only an immutable
release, verifies the GitHub asset digests, third-party license notices and the compatibility manifest,
then installs into a versioned private directory. GitHub's immutable release
attestation binds the tag, source commit and assets. New internal strategy and
exchange logic can ship in MM_Engine without a wallet patch while this
contract remains compatible. New wallet controls may require a GUI update.

The existing TUI remains an operator client of the same service architecture.
Its historical `local-service` command and state paths are unchanged by this
adapter. Standalone operator use must supply its own KDF and coin registry;
the extracted repository does not bundle those runtime assets.

## Local live worker authentication

The wallet service gives the local CEX worker a separate, random token for each
process. Only that token can access `/v1/events`, `/v1/events/acknowledge`,
`/v1/coverage/lease` and `/v1/coverage/publication-hold`. The GUI bearer token
cannot acknowledge hedge events or renew coverage. KDF lifecycle, wallet-send
and operator publication endpoints remain unavailable in wallet mode. The
operator service keeps its existing authentication contract.

The worker must synchronize signed events successfully before renewing the
signed balance lease. A failed renewal continues to block new maker orders;
this change does not bypass coverage checks or enable a paused strategy.
Run `python -m unittest discover -s tests -p 'test_wallet*.py'` for the isolated
wallet API checks.

### Active swap initialization and maker reconciliation

KDF 2.7 may expose an active swap UUID before its first durable `Started` status.
The reconciler distinguishes the exact missing-file/matching missing-UUID legacy
errors from arbitrary RPC failures. Unknown UUIDs get at most 30 seconds of
initialization grace, with new publications and updates blocked. Known owned maker
swaps never use that grace. Existing maker quotes are held only after a prior
successful reconciliation and with independent fresh market, aggregate wallet
capacity, hedge depth and CEX coverage checks. Persistent/other failures keep the
normal withdrawal path. A durable wallet taker status needs no maker canonical
lookup. `initializing_swap_uuids` makes this bounded wait observable; diagnostics
include the failing UUID. No completed swap is inferred from missing data.

Wallet strategy routes may be registered dynamically from active coin IDs. KDF
IDs are preserved verbatim, including case-sensitive network suffixes; CEX asset
codes and routes remain separate, uppercase, and validated during preview.


### Public-reader isolation and maker uptime (4 October 2026)

Wallet-created market feeds use `create_public_reader`, which bypasses the
credentialed/pooled plugin client. Every market has its own read-only depth
reader and a separate read-only metadata reader. The stdio plugin protocol
serializes a client's requests; sharing one client across markets previously
queued depth behind another symbol's slow 24h-ticker request. Sampled ticker
latencies reached 12.1 s, while depth freshness remains limited to 10 s.

The metadata worker refreshes rolling volume ahead of the existing 15 s cache
expiry. Depth reads continue independently. Metadata failure does not renew its
cache timestamp; expired metadata cannot produce a fresh signed snapshot.
Order-book timestamps, the 10 s book limit, coverage leases, minimum hedge size,
confirmation requirements and safety cooldowns are unchanged. No CEX plugin
configuration or adapter-specific changes are required. These independent
public plugin processes increase memory use per subscribed market; measure that
cost and actual withdrawal frequency after an approved restart.

Automatic quantity already decreases immediately, bypassing normal update
interval/hysteresis, when a valid smaller protected quote can be calculated.
Fixed quantities do not silently shrink. Funds committed to every other order
remain reserved. If the residual funds are below the venue minimum hedge value,
no smaller valid order exists: keep WAITING until funds/limits change. Do not
count an update in place as a withdrawal, or call net uncommitted funds the
account's total balance. A hard safety breaker can still withdraw an uncovered
order before a resize is confirmed; do not leave that order exposed on the
assumption an RPC update will succeed.

Offline regression tests cover blocked metadata with continued fresh depth,
independent readers across symbols and venues, unchanged expiry on failed
refresh, public clients without credentials/trading permission, and thread
cleanup. Actual uptime improvement requires comparison of the existing order
UUID/state-change history after deployment; test success alone is not proof of
live Tor/CEX reliability.


### Uncertain cancellation recovery (5 October 2026)

Cancellation intent and provenance are persisted before the KDF write. A lost
reply enters RECOVERING and is checked every ten seconds using bounded reads.
For cancellation without swaps, recovery requires the exact owned UUID and
pair, its absence from live makers, explicit Cancelled history with empty
matches/started swaps, no active swaps, and clear reconciliation gates. A still
visible owned UUID may only receive another cancellation after thirty seconds;
publication and additive quantity updates are never replayed by this path.
Automatic strategies resume through normal freshness, coverage and cooldown
checks after proof. Manual pauses revoke that permission and remain paused.
Swap, update and publication recovery retain their own independent gates.

Coin activation reads use a shared background snapshot. Unavailable or malformed
RPC data is distinct from an empty set of active coins. Unknown data prevents
new writes; briefly held existing orders retain the independent exposure
guards, and an expired snapshot follows the safe withdrawal path. The GUI
receives recovery status and the next readback delay. KDF_RPC diagnostics expose
only method, UTC timestamp, duration and transport outcome, never payloads.

Local candidate verification: Linux AppImage and engine compiled; Flutter
static analysis passed; Impeller disabled. After login, both legacy cancellation
blocks recovered through KDF readback. Subsequent coverage withdrawals are
separate events. New unit tests were not run for the cancellation change; CI
results and a longer live observation window must be recorded separately.
