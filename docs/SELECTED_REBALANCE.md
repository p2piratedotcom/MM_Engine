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

The engine advertises `rebalance_selection: 1` and `rebalance_ideal: true`. Legacy engine/TUI rebalance
requests keep their previous policy. GUI selection controls require the new
capability; arbitrary prices, quantities and caps from the GUI are never used.

## Three-stage preview

1. `/v1/rebalance/ideal` reads local maker configurations, remaining/daily
   budgets and persisted confirmed maker prices only. It makes no CEX or KDF
   network request. Fixed quantities and explicit auto maxima are bounded by
   the remaining/daily budget. Replenishing auto makers without a maximum use
   the finite nominal initial maker budget as their funding reference, multiplied
   by the configured auto fraction. Current OPEN obligations remain a floor.
   This is a funding goal, not a claim that the wallet can publish that quantity.
   Missing local reference prices fail explicitly instead of inventing rates.
2. Analyze values those native hedge obligations at fresh CEX prices and adds
   fees plus 20% reserve. It shows actual free Spot holdings, reserves for other
   makers, full ideal requirements and deficits. Locked funds are excluded.
   Local USDT equivalents are indicative; current buy prices set the live value.
3. The preview shows current financial coverage and the common attainable
   financial fraction, clearly FULL or PARTIAL, with the conversion sequence.
   Hedge minimum, shared depth and 24h-volume limits are reported separately:
   enough funds does not imply enough liquidity or authorize publication.

The native reference is stored with the spending allocation. Repeated Analyze
after confirmed fills revalues the same quantities instead of moving the goal
with each order-book update. Changed maker settings/budgets/enablement refresh
the reference; increased OPEN obligations cannot be hidden by a cached ideal.
Existing allocations migrate without resetting their caps or fill accounting.

Funding sources and hedge destinations are distinct. An asset with zero Spot
balance can still be bought if the selected makers need it and authorized USDT
can fund it. For a wallet maker selling DASH for USDT, the eventual hedge is a
BUY of DASH: pre-fund USDT, not DASH. For a maker selling USDT for DASH, the
hedge sells DASH: rebalance may first acquire DASH even from zero inventory.

## Calculation

1. Keep the local native maker reference independent of CEX funds and depth.
   Value its hedge requirements at fresh prices, adding hedge fees and the
   existing 20% reserve. Do not shrink the ideal because it is not fully funded.
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
   Preview limits use a 0.5% margin, leaving room within the existing 1%
   execution impact bound. Depth is at most half the visible executable depth
   within the actual approved limit. Apply price/quantity steps,
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
- Maker hedge minima/depth/volume limits are separate diagnostics, not a reason
  to silently delete that maker from the ideal. Funding can be prepared while
  liquidity is inadequate; live publication still enforces its own checks.
  Exhausted/zero references do not pretend to be 100% covered.
- A protected failed-hedge inventory invalidates executable and projected
  conversions; no misleading partial-plan coverage is approved.
- Missing balances, deleted maker selections, expired budgets, credential
  replacement and material strategy/account/market changes require correction
  and reanalysis. No automatic order replay, transfer, withdrawal or maker resume.
- The ordinary balance list is display-only and may be stale; analysis always
  loads current account funds, rules, permissions and fees. Live execution has
  not been exercised with funded trades by the agent. User-reported fills do
  not verify all execution/recovery scenarios.

## Approved-step execution

Selected-wallet execution no longer requires byte-for-byte equality with a
new optimizer suggestion. It retains the exact user-approved LIMIT quantity
and price, then validates current permissions, steps/minimum/maximum notional,
crossing price within 1%, at most half executable depth, usefulness relative
to the remaining ideal deficit, real balances, protected inventory, remaining
debit authorization, freshness and all existing idle/uncertain-intent checks.
It never replaces the approved price or quantity. Excessive, unnecessary,
unfunded, stale or unexecutable trades fail before a durable order is submitted
with a specific reason. Legacy TUI proposals keep their previous comparison.

Venue constraints are grounded in the adapter's normalized rules and current
responses; see [MEXC Spot API](https://www.mexc.io/api-docs/spot-v3/introduction)
and [Binance Spot filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md).


## Shared display balance refresh

MY CEXs and CEX REBALANCE keep independent venue/collapse controls but subscribe
to one page-owned balance source. One timer schedules refreshes for the distinct
visible venues; concurrent requests for the same venue await the same future.
Rows, loading/error state, original receipt timestamp and next-refresh countdown
are shared, and failures retain the last received balances. Hidden sections do
not schedule reads unless another visible section needs that venue. Credential
or engine changes invalidate the shared generation, so old replies are ignored.
Rebalance status polling invalidates display balances only when trade history
changes; an empty/unchanged status does not restart balance reads.

The authenticated display endpoint also uses one persistent reader per wallet
handler with per-venue locks and a two-second burst cache. This includes failures
to prevent concurrent consumers immediately repeating a failed time sync.
Credential replacement invalidates that display cache before/after storing.
This display cache never grants trading coverage; Analyze/Execute/live-worker
account freshness and time-sync limits remain unchanged. Error messages separate
the public time-sync phase from credential/account permission checks. Metadata
from the adapter carries safe failure categories, not raw exceptions or URLs.
