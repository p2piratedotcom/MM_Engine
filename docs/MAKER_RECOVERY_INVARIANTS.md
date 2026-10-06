# Maker inventory and recovery invariants

Reviewed 2026-10-06. Engine contracts, not funded acceptance claims.

- KDF inventory belongs to the sold coin inventory_pool. Pair, strategy ID and
  premium level cannot create copies of it. inventory_reservations.reserved_pool_volume
  is shared by preview and the final guard. Only a replaced UUID is excluded.
  Preview retains partial-swap residual logic; the final guard retains its
  conservative advertised-volume calculation. CEX funding remains separate.
- Bulk pause includes disabled rows with pending publication, cancellation or
  update intents. Individual pause holds publications before withdrawal. A late
  response cannot silently turn that manual pause into automatic resumption.
- Publication readback validates identity and complete terms. register_candidate
  restores the persisted minimum before strategy binding, including after a
  crash between ownership registration and binding. Recovery never resends setprice.
- Differing or unknown identities remain blocked. TTLs, budgets, coverage and
  journal retention are unchanged.

Offline tests/test_maker_intent_inventory.py exercises production methods with
in-memory stores and fake KDF: shared coin/different pairs, replaced UUID exclusion,
preview agreement, disabled uncertain publication paused in bulk, and recovery
before/after registration with a nonzero minimum. Protocol/worker fixtures remain
required. These tests establish no real-account or funded acceptance.
