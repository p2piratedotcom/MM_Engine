"""Durable, server-owned spending envelopes for selected Spot inventory."""
from __future__ import annotations
import json
import re
import time
import uuid
from decimal import Decimal as D
from .rebalance import TERMINAL, number


def percentages(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 24:
        raise ValueError('Selezionare da una a 24 coin CEX')
    result = {}
    for asset, percent in value.items():
        if (not isinstance(asset, str) or not re.fullmatch(r'[A-Z0-9]{1,24}', asset)
                or type(percent) is not int or not 0 <= percent <= 100 or percent % 5):
            raise ValueError('Percentuali CEX non valide: usare 0–100% a passi di 5%')
        if percent:
            result[asset] = percent
    if not result:
        raise ValueError('Selezionare almeno una coin con percentuale maggiore di zero')
    return dict(sorted(result.items()))


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS allocations '
               '(id TEXT PRIMARY KEY,venue TEXT NOT NULL,scope TEXT NOT NULL,'
               'percentages TEXT NOT NULL,caps TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL)')
    db.commit()


def resolve(service, balances):
    db = service._db()
    try:
        schema(db)
        policy = service.asset_percentages
        scope = sorted(service.strategy_ids)
        identity = service.allocation_id
        if identity is None:
            caps = {a: str(balances.get(a, {}).get('free', D(0)) * D(p) / 100)
                    for a, p in policy.items()}
            if not any(D(v) > 0 for v in caps.values()):
                raise ValueError('Le coin selezionate non hanno saldo Spot disponibile')
            identity = uuid.uuid4().hex
            # A new selection supersedes old budgets/proposals on this venue.
            # Pending order intents remain independently blocking and auditable.
            db.execute("UPDATE allocations SET state='SUPERSEDED' WHERE venue=? AND state='ACTIVE'",
                       (service.venue,))
            db.execute('INSERT INTO allocations VALUES (?,?,?,?,?,?,?)',
                       (identity, service.venue, json.dumps(scope), json.dumps(policy),
                        json.dumps(caps), time.time(), 'ACTIVE'))
            db.commit()
            service.allocation_id = identity
        row = db.execute('SELECT venue,scope,percentages,caps,created,state FROM allocations WHERE id=?',
                         (identity,)).fetchone()
        if (not row or row[0] != service.venue or json.loads(row[1]) != scope
                or json.loads(row[2]) != policy or row[5] != 'ACTIVE'
                or time.time() - row[4] > 86400):
            raise ValueError('Budget CEX scaduto o selezione cambiata: selezionare di nuovo e analizzare')
        caps = {a: number(q, 'budget coin', positive=False) for a, q in json.loads(row[3]).items()}
        remaining = dict(caps)
        # Pending rows reserve their worst-case debit. Only identity-checked
        # terminal results recorded by reconcile can release debit or credit USDT.
        for payload, state, result in db.execute('SELECT payload,state,result FROM orders'):
            item = json.loads(payload)
            if item.get('allocation_id') != identity:
                continue
            qty = number(item['quantity'], 'quantità')
            verified = json.loads(result) if state in TERMINAL else None
            filled = number(verified.get('executedQty'), 'eseguito', positive=False) if verified else qty
            if filled > qty:
                raise ValueError('Esito rebalance incoerente: budget bloccato')
            fee = number(item['budget_fee'], 'fee', positive=False)
            price = number(item['price'], 'prezzo')
            asset = 'USDT' if item['side'] == 'BUY' else item['asset']
            debit = filled * (price if asset == 'USDT' else 1) * (1 + fee)
            remaining[asset] = remaining.get(asset, D(0)) - debit
            if item['side'] == 'SELL' and verified:
                # A filled LIMIT sell cannot execute below its limit. Credit
                # only this conservative minimum after fees, never its ACK.
                remaining['USDT'] = remaining.get('USDT', D(0)) + filled * price * (1 - fee)
        return {'id': identity, 'strategy_ids': scope, 'percentages': policy,
                'caps': {a: str(q) for a, q in caps.items()},
                'remaining': {a: str(max(D(0), q)) for a, q in remaining.items()},
                'expires': row[4] + 86400}
    finally:
        db.close()


def latest(service):
    db = service._db()
    try:
        schema(db)
        row = db.execute("SELECT id,scope,percentages FROM allocations WHERE venue=? AND state='ACTIVE' "
                         'AND created>? ORDER BY created DESC LIMIT 1', (service.venue, time.time()-86400)).fetchone()
        if not row:
            return None
        service.allocation_id = row[0]
        service.strategy_ids = frozenset(json.loads(row[1]))
        service.asset_percentages = json.loads(row[2])
    finally:
        db.close()
    return resolve(service, {})


def invalidate(service):
    db = service._db()
    try:
        schema(db)
        db.execute("UPDATE allocations SET state='INVALIDATED' WHERE venue=?", (service.venue,))
        db.commit()
    finally:
        db.close()
