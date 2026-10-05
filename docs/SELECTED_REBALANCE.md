# Selected maker coverage and CEX spending envelopes

## Wallet flow

CEX REBALANCE is an independent collapsible card below MY CEXs, with its own
venue and read-only balance refresh. Select one or more maker configurations
on that venue, then select funding assets and percentages 0..100 in 5% steps.
The funding assets may be unrelated to the maker assets. One **Analyze** button
always uses these explicit selections; there is no second analysis action.

A percentage limits **debits**, including the conservative configured fee.
Existing inventory can continue covering a maker without being liquidated.
Balances locked in exchange orders are excluded. Assets belonging to active or
OPEN unselected maker configurations are reserved separately. Paused makers
have no active liability; all makers must be manually paused before execution.

A new selection or explicit confirmed **Reset spending limits** establishes a
new budget from fresh balances. Repeated Analyze and trade-status refresh keep
the original absolute budget, including after navigation/restart. A reset is
an additional spending authorization, clearly explained before resetting.
Budgets expire after 24 hours; individual proposals expire after 120 seconds.
Changing a selection supersedes prior budgets/proposals on that venue.
Changing API credentials invalidates stored proposals and allocation budgets.

The engine advertises `rebalance_selection: 1`. Legacy engine/TUI rebalance
requests keep their previous policy. GUI selection controls require the new
capability; arbitrary prices, quantities and caps from the GUI are never used.

## Calculation

1. Compute each selected maker's current hedge requirements from its actual
   published quantity, or its finite spendable KDF amount and strategy limits.
   Add hedge fees and the existing 20% reserve. Do not substitute unlimited KDF
   funds, overcount replacements or bypass per-pair depth/daily limits.
2. Initial source debit cap is `fresh_free_balance * percentage / 100`.
   Non-selected assets have zero debit authorization. USDT received from a
   confirmed rebalance sale becomes additional authorized USDT, never from an
   ACK, an uncertain submission or a hypothetical sale.
3. Compute a common target fraction `lambda` between 0 and 1. At a candidate
   fraction each hedge asset requires `lambda * full_target`. Preserve outside
   active liabilities before computing selected availability. Source sales
   cannot consume inventory needed by these selected targets, and cannot debit
   more than the remaining source envelope.
4. For BUY, acquire deficits after conservative receipt fees. For SELL, cap
   quantity by remaining source budget and surplus, both divided by `1+fee`.
   Price limits use the existing 1% impact bound; depth is at most half the
   visible executable depth within that bound. Apply price/quantity steps,
   minimum notional, venue maximum notional, supported sides and LIMIT type.
   Tiny purchases may round up to a minimum lot only if actually affordable.
5. A monotone 64-step Decimal bisection maximizes the common fraction. It is a
   max-min coverage policy, rather than favoring the first maker. Deterministic
   source selection prefers lower bid/ask spread, then asset ticker. Coverage
   is a conservative estimate within the common USDT route contract, not a
   global optimizer over all possible venue pairs or a guarantee of fills.
6. Show full targets, achievable targets, common percentage, exclusions,
   protected inventory and projected conversion sequence. Later buys remain
   indicative until sales finish. Only the first currently funded LIMIT trade
   is executable per explicit confirmation, followed by verified status and a
   fresh Analyze. No maker quantity/state is changed by rebalance.

Example: choosing LTC 85% and USDT 50% permits LTC surplus sales up to the
initial LTC envelope and USDT purchases up to the initial USDT envelope plus
confirmed sale proceeds. It does not consume 85% of the remaining LTC each time
Analyze is pressed. Selecting several makers seeks the largest common fraction
of their combined hedge requirements. If full coverage cannot be reached,
fixed makers may remain unpublishable and live coverage checks still apply.

## Durable budget accounting

Allocations are server-owned rows in the private 0600 rebalance journal; order
intents include their allocation identity and fee allowance BEFORE the remote
write. Pending/unknown orders reserve the full worst-case debit. Only an
identity/price/quantity checked terminal order observation releases unfilled
amounts. SELL credit uses filled quantity times the submitted limit price minus
fee: a conservative floor for a verified LIMIT sale. BUY debit uses filled
quantity times the limit price plus fee: a conservative ceiling. These avoid
claiming ACKs are fills and do not need exchange-specific commission payloads.
All old uncertain-send/publication/hedge locks remain in force.

## Limits and failure handling

- Current Spot adapters normalize supported routes as ASSET/USDT. An unrelated
  asset without a supported, permitted USDT route or verified fee is excluded
  with a reason; required hedge routes failing validation block the analysis.
- At most 24 selected funding assets and 16 required market snapshots per
  analysis. Public reads remain bounded with original book/volume observation
  times (10/15 seconds) and a 30-second read window. Slow analyses fail closed.
- Selected fixed makers outside current hedge minima/depth, and auto makers
  unable to meet KDF/market hedge minima, block the plan
  with their reason; exhausted/zero targets do not pretend to be 100% covered.
- A protected failed-hedge inventory invalidates executable and projected
  conversions; no misleading partial-plan coverage is approved.
- Missing balances, deleted maker selections, expired budgets, credential
  replacement and changed market/strategy/account data require correction and
  reanalysis. No automatic order replay, transfer, withdrawal or maker resume.
- The ordinary balance list is display-only and may be stale; analysis always
  loads current account funds, rules, permissions and fees. Live execution has
  not been validated with funded trades in this implementation.

Venue constraints are grounded in the adapter's normalized rules and current
responses; see [MEXC Spot API](https://www.mexc.io/api-docs/spot-v3/introduction)
and [Binance Spot filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md).
