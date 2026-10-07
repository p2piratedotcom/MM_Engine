# Market freshness and KDF timeout diagnostics

A stale market snapshot means the engine lacks a sufficiently recent **validated**
book (configured TTL, normally 10 s) or 24-hour volume observation (15 s).
It does not establish that MEXC is disconnected. Failures, slow requests through
Tor, plugin queue/startup delays, metadata refresh gaps and time spent in KDF
publication can all exhaust freshness. Clock skew is logged separately as
`book_future` / `volume_future`. Rejected timestamps are never extended.

A KDF transport timeout means the local RPC did not deliver the complete response
within the effective socket timeout (normally 15 s for `setprice`). urllib's
socket timeout is not a total request deadline. A write timeout does **not** prove
that publication failed: reconcile its UUID before submitting another write.
KDF can itself wait for remote chain services, but the loopback timeout does not
prove which internal operation was delayed.

## Files and records

Next engine startup enables `engine-network-diagnostics.jsonl` beside
`ownership.sqlite3`. A standalone local worker uses
`worker-network-diagnostics.jsonl` beside its hedge journal; an embedded worker
shares the process's configured writer. The existing `kdf-rpc-diagnostics.jsonl`
format remains compatible.

Each new journal is private (0600, no symlink follow on open), rotates at 16 MiB
with two backups (48 MiB maximum per writer). A bounded queue of 2048 records
keeps filesystem writes off request threads; saturation drops diagnostics and
records the cumulative `dropped_records` count. Last buffered records may be
lost on exit. Coverage of a two-hour window must be checked from the oldest
retained timestamp; rotation or drops can leave gaps. Diagnostic failures never
retry or change trading operations.

- `kdf_request_start/end`: correlation ID, method, start UTC, elapsed monotonic
  and wall time, actual timeout, result classification (`received`, `rpc_error`,
  `invalid_response`, `transport_error`, `client_error`), mutation/uncertainty,
  HTTP status and coarse transport phase. `connect_or_headers` includes
  connection and header wait; it cannot distinguish DNS/TCP/TLS or KDF work.
  `response_body` means headers arrived but reading the body failed.
- `plugin_request_start/end`: venue, public symbol where applicable, public vs
  private lane, configured route (Tor/direct), queue, bootstrap, send, response
  wait and adapter execution time, budget, HTTP status and failure classification.
  A plugin reply timeout differs from an adapter-reported timeout. `adapter_ms`
  includes the adapter's whole method, not just the network. It cannot by itself
  distinguish Tor congestion from a slow exchange endpoint. Optional response
  timing metadata works with existing downloaded adapters.
- `feed_stage_start/end`, `feed_cycle_start/end`, `metadata_request_start`:
  book/volume original observation times and ages, validity limits, stage and
  cycle durations and queue wait. Cached metadata reads are identified by their
  stage; only `metadata_refresh` performs the periodic ticker request.
- `feed_wakeup`, `metadata_wakeup`: delay relative to the scheduled wake-up.
- `feed_retry`, `metadata_retry`: failures and next delay. Metadata errors are
  retained even though the refresh loop continues using its unchanged TTL.
- `market_rejected`: exact book/volume age and expiry/future cause, source
  (`ingest` or `current`), throttled to one record per cause/store/source/5 s.
- `rebalance_read_start/end`: analysis correlation ID, public symbol, method,
  duration and outcome of the bounded ticker/book reads. Rule and commission
  checks precede these reads; plugin diagnostics retain their individual times.
- `rebalance_market_check`: age and validity limit for each analyzed symbol,
  checked after all readers close and again before returning the plan. It
  distinguishes `stale_book` from `stale_volume` without including balances.

No credentials, URLs, headers, request/response bodies, account balances or raw
exception messages are accepted. Methods, internal labels, timestamps, durations
and status numbers use an explicit field allowlist.

## Interpreting the next observation

1. Match `market_rejected` time/symbol to feed stage and plugin records. Large
   queue/bootstrap time suggests a local delay; slow adapter time or reported
   network errors suggest the remote path, without proving Tor or MEXC separately.
2. Compare rejection age to the originating timestamp. Persisted
   `market_book_samples` are sampled only every 15 s; gaps in that table are **not**
   a measurement of every refresh and cannot prove the feed polling interval.
3. Match KDF request starts/ends by ID and PID. Check fast read calls too:
   simultaneous slow reads and writes suggest a broader KDF stall; only slow
   publication suggests a method-specific path. An unmatched start alone is not
   proof of a stall (rotation, queue drops and shutdown can produce it).
4. Confirm that the instrumented build is actually running before interpreting
   absent journals. Changing source files does not enable logging in an already
   running binary. Inspect CPU/memory and retained-window coverage alongside logs.

The private read-only monitoring collector accepts both journal formats, keeps
only strict metadata, aggregate counts/maxima, recent errors and unmatched starts.
This instrumentation leaves TTLs, retries and order safety checks unchanged.


## Phase diagnostics extension (source candidate, 2026-10-07)

This extension requires a rebuilt engine. Fine MEXC HTTP phases additionally
require the companion CEX_configs MEXC 0.1.1 adapter; old v1 adapters remain
compatible and simply lack those measurements. Changing source/catalog files
never hot-updates an already running process.

- `http_request_end` shares the enclosing KDF/plugin request ID. `http_sequence`
  distinguishes up to eight HTTP calls within a single adapter method. Only
  bounded fixed phase labels, monotonic durations, numeric HTTP status/errno and
  fixed failure categories cross this boundary. No endpoint/hostnames, headers,
  response size, payloads, account amounts or order identifiers are recorded.
- aiohttp phases identify DNS, connection acquisition, connection establishment
  including TLS/proxy negotiation, request send, headers wait, body and decoding.
  `connection_total_ms` includes DNS; do not sum it with `dns_ms`. The connection
  phase does not split TCP, TLS and a Tor proxy's upstream work. Headers wait can
  include request-body upload, network latency and remote processing; it is not
  a measurement of exchange compute time. A proxy route may resolve the remote
  hostname upstream, so absence of a local DNS phase is not a DNS failure.
- urllib (including current KDF loopback calls) retains its existing socket
  timeout and can measure only `connect_or_headers` and `response_body`.
  Neither transport changes routing, redirects, deadlines or retry behavior.
- Optional host `diagnostic_phase` frames contain only protocol sequence ID and
  fixed phase. They share the **original** reply deadline and cannot extend it.
  The last received phase survives in `plugin_request_end` when the final reply
  times out; it is the last *observed* phase, not proof of the precise blocking
  point. Old adapters/hosts emit no frames. At most 64 frames per method.
- `adapter_thread_cpu_ms` / `adapter_process_cpu_ms` help distinguish CPU work
  from elapsed waiting/scheduling. Low CPU cannot distinguish network waiting
  from process suspension. `plugin_pid`, `protocol_sequence` and
  `plugin_lifecycle` (spawn/close/exit, numeric return code) identify host churn
  without reading process command lines or environment.
- `coverage_stage_start/end` use a correlation ID for each renewal stage.
  `coverage_publication_hold` with reason `renewal_failed` records the trigger
  requiring fail-closed recovery, not successful delivery of the hold. Existing
  lease/coverage policies remain the authority; no balances are added to logs.

Each writer also keeps `engine-network-incidents.jsonl` (or worker equivalent),
8 MiB plus two backups, private 0600. It mirrors errors, HTTP failures, rejected
markets, lifecycle and operations lasting at least 5 seconds. A heartbeat at
most once per minute **while records are being written** shows retained logging
activity; absence may mean idle, exit or writer trouble, not service death.
`diagnostic_write_failures` and `dropped_records` report losses when writing
recovers. Logging remains asynchronous, bounded and never retries a trade.
The sparse incident history does not restore a missing complete request history.

Monitoring deduplicates mirrored records, reports coverage per journal and
preserves strict metadata allowlists. Check coverage of full and sparse logs
separately before drawing conclusions. Offline/live acceptance of this source
candidate is separate from syntax review and packaging.


## Resolver/local-path metadata (source candidate, 2026-10-07)

MEXC 0.1.2 plus the companion engine adds session/connection/cache reuse flags,
`resolver_queue_ms`, `resolver_call_ms` and `resolver_inflight` to existing
correlated HTTP records. This distinguishes local executor contention from time
inside OS getaddrinfo. The latter can include NSS, local stub/cache and upstream
DNS; it is not provider-server compute time. A pending/shared DNS resolution
can produce a cache signal while still waiting; use durations and in-flight
flags as well. No read/write retry, DNS override, market TTL change or additional
network query is used by these measurements.

`network_path_sample` is produced asynchronously every 30 seconds, with no
probe packets. On Linux it samples the IPv4 default-route interface's numeric
index, carrier changes and RX/TX error/drop totals/deltas; when exposed by the
driver, legacy wireless quality/signal and retry-discard/missed-beacon counters.
Missing driver counters remain absent with `wifi_stats_available=false`.
Counter reset or interface change invalidates deltas (`link_sample_reset`).
These counters cover all host traffic, not just the wallet; zero counters do not
prove absence of Wi-Fi interference. The IPv4 default route may differ from
an individual IPv6/VPN/proxied request's actual path; no IPv4 default route is
reported as unavailable, not offline.

Resolver configuration records only fixed categories: `local_stub`, `gateway`,
`private`, `public`, `mixed`, `unknown`. A gateway-configured DNS suggests using
the router as a resolver/proxy, but does not prove it caused the delay. A public
DNS address does not identify ownership by the ISP. Read-only resolvectl queries
collect configured DNS class and recognized numeric aggregate statistics when
available. Permission denial, absent service/tool or unknown JSON schema means
`resolver_stats_available=false`; never substitute zero, request privileges,
flush caches or change settings. The bounded subprocess reads run on a separate
sampler thread with one-second timeouts. No hostnames, IP/MAC addresses, SSIDs,
resolver cache contents, packet capture, keys or financial data are persisted.

Correlate HTTP timestamps with path samples: long local queue suggests executor
contention; low queue with long OS call points to the system-resolution path;
carrier/drop changes or Wi-Fi counters provide separate link evidence. Samples
are hints, not automatic causal verdicts. Distinguishing router, Wi-Fi loss,
provider DNS and remote endpoint conclusively needs controlled independent
measurements/telemetry; this passive instrumentation explicitly leaves those
cases unresolved. An unavailable resolver counter is not a network failure.


### Universal adapter coverage

The common CEX_configs HTTP-core candidate now extends the preceding MEXC-only
session/resolver notes to every generated Spot adapter. Venue names are supplied
by the engine host, not hard-coded in diagnostics. Native signing/decoding and
experimental venue limits remain unchanged. Passive host-path samples remain
whole-host context, not proof that a particular venue/router/provider is slow.
Runtime deployment must confirm the new engine and companion immutable catalog;
source edits alone do not enable these metrics.
