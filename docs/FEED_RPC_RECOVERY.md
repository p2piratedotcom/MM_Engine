# Feed freshness, KDF RPC and dashboard recovery

The 5 October 2026 investigation found brief `setprice` timeouts recovered by
read-only UUID attribution, while the wallet continued displaying an old
`REVIEW_REQUIRED` snapshot. ARRR and DASH also experienced actual withdrawals
for expired market references and chain-backed KDF reads.

## Changes

- Poll the wallet dashboard every five seconds, one request batch at a time.
  Order and strategy states come from the same authenticated local snapshot.
  Busy engine locks return a timestamped previous snapshot, or 503 before the
  first snapshot; no HTTP handler queues behind a chain-backed write.
- Expose automatic pending publication checks as `RECOVERING` in read-only
  status. Durable internal review states and manual hold controls are preserved.
  No write is replayed after timeout.
- Omit zero `volume_delta` for price-only updates. In KDF 2.7,
  `update_maker_order` checks chain balances and fees whenever the field is
  present, including zero. Nonzero changes still get the original validation.
- Reuse valid `max_maker_vol` reads for at most two seconds from request start.
  Mutations invalidate before and after sending, including uncertain responses;
  errors, invalid data and slow reads are never cached. KDF remains the final
  validator for volume-changing writes. This is not a fallback on read failure.
- Recheck live market and hedge coverage after update preflight readback.
- Retry public depth and rolling-volume reads promptly after transient errors,
  with bounded exponential backoff during outages. Readers remain independent.
- Sign the rolling-volume observation timestamp separately from book time and
  enforce its original 15-second lifetime. A newer book or slow response cannot
  extend metadata validity. Older signed snapshots without the optional field
  remain compatible with the existing book freshness check.

## Private diagnostics

Slow (at least 1 second) and failed KDF requests are recorded in
`kdf-rpc-diagnostics.jsonl` alongside the ownership database. There are at most
three 1 MiB files, with mode 0600. Records contain only UTC time, an allowlisted
method, elapsed milliseconds, and transport outcome. No URL, payload, response,
exception message or credentials are retained. Logging does not authorize any
operation and never changes an RPC result.

The exact underlying chain/network cause of the earlier timeouts cannot be
reconstructed: the GUI discarded the engine stderr. The new diagnostics allow
later monitoring to measure which methods stall without reading credentials.

Safety withdrawals, market age limits, minimums, hedge leases, budgets and
cooldowns remain enforced. These changes reduce avoidable work; a persistent
network failure must still stop unsafe quoting. Linux Impeller remains disabled.
