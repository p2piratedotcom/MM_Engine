"""Durable intent for setprice; recovery only reads, never resends a trade."""
from dataclasses import asdict
from decimal import Decimal
import json
import time
import uuid

from .models import QuotePlan, DexSide, HedgeSide


def decode_plan(raw):
    numeric = {'arrr_quantity', 'reference_vwap', 'cex_limit_price', 'human_price_usdt_per_arrr',
               'kdf_price', 'kdf_volume', 'effective_edge', 'configured_premium', 'cex_taker_fee', 'risk_buffer'}
    values = dict(raw)
    for key in numeric & values.keys():
        values[key] = Decimal(values[key])
    values['dex_side'] = DexSide(values['dex_side'])
    values['hedge_side'] = HedgeSide(values['hedge_side'])
    return QuotePlan(**values)


class PublicationRecovery:
    def __init__(self, ownership):
        self.ownership = ownership
        self.db, self.lock = ownership.connection, ownership._lock
        with self.lock:
            self.db.execute('''CREATE TABLE IF NOT EXISTS publication_intents (
                id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, plan TEXT NOT NULL,
                before_uuids TEXT NOT NULL, minimum TEXT, requested_at_ms INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING', order_uuid TEXT,
                detail TEXT NOT NULL DEFAULT '')''')

    def begin(self, plan, before, minimum):
        intent_id = str(uuid.uuid4())
        with self.lock:
            if self.db.execute("SELECT 1 FROM publication_intents WHERE strategy_id=? AND state IN ('PENDING','REGISTERED','HELD')",
                               (plan.strategy_id,)).fetchone():
                raise ValueError('pubblicazione precedente non risolta: nessun reinvio')
            self.db.execute('''INSERT INTO publication_intents
                (id,strategy_id,plan,before_uuids,minimum,requested_at_ms) VALUES (?,?,?,?,?,?)''',
                (intent_id, plan.strategy_id, json.dumps(asdict(plan), default=str), json.dumps(sorted(before)),
                 None if minimum is None else str(minimum), time.time_ns() // 1_000_000))
        return intent_id

    def pending(self, strategy_id):
        with self.lock:
            rows = self.db.execute("SELECT * FROM publication_intents WHERE strategy_id=? AND state IN ('PENDING','REGISTERED')",
                                   (strategy_id,)).fetchall()
        return [dict(row) for row in rows]

    def state(self, intent_id, state, *, order_uuid=None, detail=''):
        with self.lock:
            self.db.execute('UPDATE publication_intents SET state=?,order_uuid=COALESCE(?,order_uuid),detail=? WHERE id=?',
                            (state, order_uuid, detail, intent_id))

    def hold(self, strategy_id):
        with self.lock:
            self.db.execute("UPDATE publication_intents SET state='HELD',detail='Recupero automatico sospeso manualmente' WHERE strategy_id=? AND state IN ('PENDING','REGISTERED')",
                            (strategy_id,))

    def candidate(self, intent, snapshot):
        plan = decode_plan(json.loads(intent['plan']))
        before = set(json.loads(intent['before_uuids']))
        candidates = [(uid, o) for uid, o in snapshot.items() if uid not in before
                      and (o.get('base'), o.get('rel')) == (plan.kdf_base, plan.kdf_rel)]
        if len(candidates) != 1:
            raise ValueError('recupero: ordine assente o ambiguo; nessun reinvio')
        uid, order = candidates[0]
        try:
            created = int(order['created_at'])
            matches = (order['uuid'] == uid
                and intent['requested_at_ms'] - 5000 <= created <= intent['requested_at_ms'] + 120000
                and Decimal(str(order['price'])) == plan.kdf_price
                and Decimal(str(order['max_base_vol'])) == plan.kdf_volume
                and Decimal(str(order['available_amount'])) == plan.kdf_volume
                and not order['matches'] and not order['started_swaps']
                and (intent['minimum'] is None or Decimal(str(order['min_base_vol'])) == Decimal(intent['minimum'])))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            matches = False
        if not matches:
            raise ValueError('recupero: dati diversi, incompleti o ordine già abbinato; verifica manuale richiesta')
        existing = self.ownership.get(uid)
        if existing is not None and existing.status.value != 'OPEN':
            raise ValueError('recupero: UUID già terminale, non riattivabile')
        return uid, plan
