# Optional maker hedging and shared coverage

Source candidate, 2026-10-07. Not a claim of release installation, fixture or
funded acceptance. This change implements optional hedging and shared coverage;
custom rebalance snapshots and live rebalance execution remain deferred.

## Immutable creation choice

`StrategySpec.hedging_enabled` is a strict boolean, default true for historical
payloads. The GUI negotiates `optional_hedging: 1`; an older engine keeps the
previous hedged form. The choice is editable only for a new configuration.
Service/store validation and a SQLite strategy trigger reject changes even to
paused makers. To replace the choice, retire/archive the old configuration and
create a new ID. Modification does not reset consumed budgets.

QuotePlan and owned UUIDs persist hedging and reference requirements before
strategy binding. Ownership schema migrations default old UUIDs to hedged with
reference checks. Recovery restores those exact flags from the durable intent;
an ownership trigger rejects later changes. No network failure or absent field
can silently turn a hedged maker into an unhedged one.

With hedging off and fixed price, preview/sizing depend only on KDF capacity,
shared sold-coin reservations and maker/daily budgets. No CEX key, balance, book,
lot/minimum or compensating trade is required. Fixed means no automatic price
change; quantity remains a separate fixed/automatic choice. Native KDF fees,
minimums, activation, preflight, reconciliation and uncertain-write guards remain.

With hedging off and automatic price, only public executable bid/ask references
and premium determine the price; hedge costs are zero and quantity depends on
KDF, not CEX funding/depth. Signed fresh books still respect original age/future
limits. Price-only snapshots can explicitly mark missing volume (`volume_known`
false), without inventing an observation time. Hedge consumers always reject
unknown/expired volume via strict current(); price-only consumers request its
book-only view. Shared feeds may supply fresh books to price-only makers while
hedged peers remain blocked until their own volume requirements recover.

DEX-only UUIDs emit no hedge/outcome event to the CEX consumer. Swap identity,
coins and ownership are validated, budgets tracked, and successful terminal
swaps acknowledged locally, including paused makers. Failed swaps retain the
manual review gate; this change grants no automatic failed-swap resumption.
Wallet inventory remains exposed to price movements when there is no hedge.

The engine can remain available when CEX credentials are unavailable; hedged
makers remain fail-closed WAITING, and all makers still need explicit KDF write
permission. Normal KDF/Tor ownership and wallet-mode transfer restrictions stay.

## Shared reservations

`shared_coverage.py` maintains a private observational reservation snapshot in
ownership.sqlite3, keyed by owner ID and CEX-qualified asset. The financial
source of truth remains durable UUIDs/intents, verified swap/hedge evidence and
fresh signed balance leases, not stored snapshot rows. The current protocol has
one configured account per venue per wallet profile; different venues never
share funds. Credential replacement retains existing idle/invalidation guards.

Reservations cover:
- Active maker UUIDs, including residual amounts after matching.
- Unresolved publications, including manually HELD unknown writes.
- Uncertain update high-water terms, excluding only the actual replaced UUID.
- Unacknowledged swaps until hedge proof; already executed spending is not
  subtracted again after a filled basket. Acquired inventory stays protected
  until its DEX outcome is acknowledged.

KDF capacity uses the same quote/intent high-water facts across every pair and
mode. Swap-locked wallet funds are already excluded by KDF max_maker_vol, so
synthetic swap CEX holds are not added again to the KDF advertised total.
Exchange free balances exclude remote locked funds. The existing worker stops
publishing fresh leases during unresolved hedges; unknown balances remain
unknown, never zero or synthetic free cash. Rebalance pending-intent locks and
idle requirements are unchanged.

Known fresh aggregate deficits preserve stable older UUIDs first; uncancelable
intent/swap holds are charged before makers. Automatic quotes can shrink only
through normal final guards and durable update/readback. If a valid reduction
is unavailable, retire required UUIDs selectively. Fixed amounts never silently
shrink. Unknown/stale funding or uncomputable obligations retain the conservative
hedged-order circuit breaker; DEX-only makers do not inherit CEX funding failures.
Shared book depth includes other same-venue/symbol/side obligations. Venue lot,
precision, impact, depth fraction, volume limits, cooldown and reentry margins
remain; small missing amounts are not dismissed with an epsilon.

GUI Shared hedge coverage shows free, committed, uncommitted and missing funds;
unknown/stale display balances are explicitly unavailable. Uncommitted funds are
not permission to execute a trade. Priority is deterministic older-order
retention, not a configurable profitability optimizer or promise every maker can
be funded. Rebalance candidates exclude DEX-only makers as compatibility, not
as a new live/custom rebalance feature.

## Scope and remaining verification

Python syntax and changed Dart files receive static review. Unit/integration,
concurrent-fill, timeout/restart and financed acceptance are separate checks and
must not be inferred from static validation. The running AppImage/profile remain
unchanged until explicit installation/restart. New unhedged strategies require an
updated engine: downgrade must not reinterpret their saved intent as hedged.
