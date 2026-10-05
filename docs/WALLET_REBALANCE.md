# Spot rebalance in P2Pirate Desktop

The wallet uses the same `CexRebalanceService` policy as the TUI through the
common Spot plugin protocol. The wallet owns presentation and confirmation;
the engine owns targets, prices, quantities, credentials and execution.

## User flow

1. Open Trading Engine → MY CEXs and select the exchange. **Analyze** reads
   current Spot balances, permissions/fees and public depth. It considers open
   and enabled maker configurations on this CEX, including enabled makers
   waiting for funds. If all makers are paused, it considers the configured
   makers. Explicit `strategy_ids` preserve this selection across manual pause.
2. Review coverage targets with a 20% reserve, missing balances, proposed
   BUY/SELL trades and exclusions due to minimum sizes or insufficient depth.
   Existing advertised quantities are a floor: a smaller automatic candidate
   cannot release coverage already needed by an open order.
3. Pause the makers manually (the panel provides a confirmed **Pause maker
   orders** action for all CEXs). Wait for order reconciliation and all swaps
   and hedges to complete. Keep live mode enabled; do not stop/restart the
   engine merely to rebalance. Run **Analyze** again after pausing.
4. **Execute rebalance** displays the exact first LIMIT trade and requires
   confirmation. It authorizes one operation only. Fresh validation may reject
   a changed proposal and require another analysis/confirmation.
5. **Refresh trade status** queries its durable client order ID. After confirmed
   terminal execution, analyze again for the next step. A LIMIT trade can
   remain open or partially filled; it can be cancelled directly at the CEX,
   followed by a status refresh. Makers are never resumed automatically.

Only excess in strategy assets may be sold. Buy proposals do not spend proceeds
from unfilled sales. The planner preserves coverage reserves and does not sell
unrelated holdings. Insufficient total funds or book liquidity are reported;
rebalancing does not create capital or remove exchange minimums. No withdrawals
or network/address selection are part of this feature.

## Wallet endpoints

All routes require the GUI bearer token, not the separate worker token.
The operator-only raw rebalance context remains inaccessible in wallet mode.

| POST route | Body | Effect |
| --- | --- | --- |
| `/v1/rebalance/analyze` | `venue`, optional `strategy_ids` | Read-only CEX/KDF analysis, server-owned proposal ID, expiry, targets, trades, blockers |
| `/v1/rebalance/execute` | `venue`, `id`, `confirmation: "EXECUTE REBALANCE <id>"` | Consume stored proposal and submit its first validated LIMIT trade |
| `/v1/rebalance/status` | `venue` | Query pending trade IDs; reconcile local journal without new orders |

Capabilities expose `rebalance: true`; older engines display an update notice
instead of enabling an unavailable action. Proposal prices/quantities cannot be
supplied by the wallet. Proposals last 120 seconds, are single-use, are lost on
engine restart and are invalidated when that venue's credentials change.

## Exclusion and recovery

Execution holds the existing exclusive rebalance gate and cooperates with the
local hedge worker. All makers and repricing must be manually paused, with no
KDF orders, swaps, unresolved publication/cancellation or pending hedges. The
checks use the entire wallet, even when the proposal covers one CEX. Symbol
rules, permission, available funds, configured/actual fees and failed-hedge
inventory protection are revalidated. Final book age is at most ten seconds;
an API timeout never triggers a write retry.

The client order ID is persisted in `<hedge-journal>.rebalance.sqlite3` BEFORE
submission. A write ACK stays SUBMITTED until a read verifies client ID,
symbol, side, original quantity and executed quantity. An uncertain or open
trade blocks operational hedge cycles and new KDF publication. Safety pause,
shutdown and read-only status endpoints remain reachable.

A cooperative worker can initialize after restart while a rebalance is pending,
so its read-only recovery UI is reachable. Its operational cycles remain blocked
by `assert_no_pending` until verified reconciliation releases the hold. Missing
order IDs do not clear an ambiguous submission. A new analysis is never a retry
of that submission.

Live execution has not been exercised against a funded account as part of this
implementation. Experimental CEX adapters retain their existing status.
