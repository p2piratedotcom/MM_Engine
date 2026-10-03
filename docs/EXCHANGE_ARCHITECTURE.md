# Exchange boundary and read-only preview

## Responsibilities

The strategy engine consumes canonical Spot balances, symbol rules, order books,
orders and fills. Pricing, sizing, inventory reservation, hedging, journaling and
KDF reconciliation belong to the common engine.

`src/kdf_mm/exchanges/config/<venue>.json` contains exchange identity, adapter
identifier, default API URL, legacy key namespace, signed request window,
time synchronization budget and read timeout/error policies. It contains no
credentials and cannot execute code. Package/release recipes include these files.

`exchanges.create_client` translates the configuration into the corresponding
adapter. MEXC V3 and Gate V4 implement API signing, endpoints, exchange-native
symbols and response normalization. Different signing algorithms require code
in an adapter; a JSON file alone cannot implement an arbitrary exchange API.
A new adapter implements the Spot contract and is registered at this boundary,
with its configuration and contract tests, without changing strategy math.

Existing `Mexc*` names and ARRR fields in durable records remain compatibility
aliases: their removal would need a separate data migration. The current strategy
supports arbitrary configured KDF base/quote assets routed through Spot USDT
markets. This change does not claim derivatives or every possible quote asset.

## Preview fix

The GUI intentionally starts no live CEX worker in preview mode. Previously
sizing nevertheless demanded its live coverage lease, making preview impossible.
The strategy preview/capacity paths now use `PreviewBalances` when no fresh live
lease is available. It loads current credentials at request time (keys entered
after engine startup therefore work), performs only read requests, verifies
Spot account/authorized symbols, and caches balances for at most three seconds
from the start of the balance request. A balance request taking ten seconds is
rejected; completion cannot freshen an old sample.

The read-only snapshot is never accepted by the live CoverageGuard. Background
strategy publication still demands the signed live worker lease and all existing
hedge/reconciliation checks. Saving creates paused configuration only. Zero funds,
unsupported routes and API permission/network failures remain errors with actionable
messages and no reflected remote payloads or secrets.

Tor reads use bounded exchange-specific budgets rather than 1.7–2 second limits.
Wallet live leases are capped at 30 seconds; balances are dated at local request
start, not an exchange clock offset, and expiration remains fail closed.

The CEX worker can now operate with Gate credentials alone. All configured clients
are resolved through the same adapter factory; trading remains explicitly gated.

## Validation

Standalone tests exercise both venue adapters, read-only balance sizing, cache
age, failed/slow reads, secret redaction, and isolation from live publication.
Tests use fake exchanges and never place funded orders. End-to-end confirmation
against the user's actual MEXC account must be performed in preview mode after
installing the local candidate; automated checks do not prove account permissions.

API contract reference: https://mexcdevelop.github.io/apidocs/spot_v3_en/
(account information and authorized Spot symbols).

Verified local results: 16 standalone engine tests, 97 strategy tests and 6
hedging tests passed. The inherited coverage test file has the same four failures
against unchanged main and this candidate (stale single-argument `_read` fixtures);
these are recorded as preexisting failures, not successful checks.

## Local MEXC balance regression (2026-10-03)

Verified both credentials in the existing wallet Secret Service profile; no key
migration or re-entry was needed. Read-only MEXC requests through the wallet Tor
proxy succeeded. `selfSymbols` returned 1,822 entries, including symbols outside
the supported ASCII alphanumeric hedge namespace. Rejecting the complete list
prevented balance display before the account request was even made.

Balance display now reads the Spot account independently of hedge permissions
and accepts an account with read permission but without trading permission.
Preview filters unsupported symbol entries as the live coverage publisher does;
it does not authorize those entries. Live coverage still requires Spot trading
permission and fresh signed coverage. Regression tests cover unsupported pairs,
read-only accounts and empty balance lists. All 19 standalone tests passed;
a real read-only account query through Tor returned five asset rows.
