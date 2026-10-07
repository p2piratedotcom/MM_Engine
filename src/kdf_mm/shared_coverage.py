"""Deterministic reservations from durable UUIDs/intents, not synthetic cash."""
from decimal import Decimal
import json
import time


class SharedCoverageLedger:
    def __init__(self, ownership):
        self.db, self.lock = ownership.connection, ownership._lock
        self._last = None
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS shared_coverage_reservations (owner_id TEXT NOT NULL, owner_kind TEXT NOT NULL, asset TEXT NOT NULL, required TEXT NOT NULL, state TEXT NOT NULL, observed_ms INTEGER NOT NULL, PRIMARY KEY(owner_id,asset))")

    def observe(self, claims, funds, *, fresh, observed_ms):
        rows = [{'owner_id':owner,'owner_kind':kind,'asset':asset,'required':str(amount),
                 'state':'OBSERVED' if fresh else 'BALANCE_UNKNOWN'}
                for owner,kind,amounts in claims for asset,amount in amounts.items()]
        totals = {}
        for _,_,amounts in claims:
            for asset,amount in amounts.items(): totals[asset] = totals.get(asset,Decimal(0))+amount
        signature = json.dumps([rows,fresh],sort_keys=True)
        if signature != self._last:
            with self.lock:
                self.db.execute('SAVEPOINT shared_coverage_snapshot')
                try:
                    self.db.execute('DELETE FROM shared_coverage_reservations')
                    self.db.executemany('INSERT INTO shared_coverage_reservations VALUES (?,?,?,?,?,?)',
                        [(r['owner_id'],r['owner_kind'],r['asset'],r['required'],r['state'],observed_ms) for r in rows])
                    self.db.execute('RELEASE shared_coverage_snapshot')
                except Exception:
                    self.db.execute('ROLLBACK TO shared_coverage_snapshot')
                    self.db.execute('RELEASE shared_coverage_snapshot')
                    raise
                self._last = signature
        return {'schema':1,'policy':'preserve_existing_older_orders', 'lease_fresh':fresh,
                'observed_ms':observed_ms,'reservations':rows,
                'assets':[{'asset':asset,'free':str(funds.get(asset,0)) if fresh else None,
                           'required':str(amount),'uncommitted':str(max(Decimal(0),funds.get(asset,Decimal(0))-amount)) if fresh else None,
                           'missing':str(max(Decimal(0),amount-funds.get(asset,Decimal(0)))) if fresh else None}
                          for asset,amount in sorted(totals.items())],
                'notice':'Free balances already exclude exchange-locked funds. Uncertain operations stay reserved; no balance is treated as zero when unavailable.'}


def keep_funded_orders(claims, funds):
    """Uncancelable swap/intent holds first; then stable older UUIDs in order."""
    remaining = {k:Decimal(v) for k,v in funds.items()}
    for _,kind,amounts in claims:
        if kind != 'maker':
            for asset,q in amounts.items(): remaining[asset] = remaining.get(asset,Decimal(0))-q
    keep, retire = [], []
    for owner,kind,amounts in claims:
        if kind != 'maker': continue
        if all(remaining.get(asset,Decimal(0)) >= q for asset,q in amounts.items()):
            keep.append(owner)
            for asset,q in amounts.items(): remaining[asset] = remaining.get(asset,Decimal(0))-q
        else: retire.append(owner)
    return keep,retire
