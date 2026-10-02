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

## Wallet API

The allowlist in `vps_agent.py` limits wallet mode to strategy previews and
management, market/coin/status reads, owned-order status and reconciliation.
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

Protocol major `1` is fixed for this first adapter. A release manifest must
name the engine version, source commit, platform, architecture, minimum wallet
protocol, supported KDF version range and SHA-256 for the asset. The wallet
must authenticate that manifest using a trusted key or other pinned release
identity before installing it. Install in a versioned per-user directory and
switch the active version only after verification. New internal strategy and
exchange logic can ship in MM_Engine without a wallet patch while this
contract remains compatible. New wallet controls may require a GUI update.

The existing TUI remains an operator client of the same service architecture.
Its historical `local-service` command and state paths are unchanged by this
adapter.
