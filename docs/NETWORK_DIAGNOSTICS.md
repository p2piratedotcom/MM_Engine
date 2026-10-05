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
