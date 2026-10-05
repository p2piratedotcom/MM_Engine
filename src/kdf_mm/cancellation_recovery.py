"""Durable cancellation provenance; a lost reply never authorizes a new order."""
import time


AUTOMATIC_SOURCES = frozenset({'strategy_safety', 'market_data', 'hedge_depth', 'coverage'})


class CancellationRecovery:
    def __init__(self, ownership):
        self.db, self.lock = ownership.connection, ownership._lock
        with self.lock:
            self.db.execute('''CREATE TABLE IF NOT EXISTS cancellation_intents (
                order_uuid TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                source TEXT NOT NULL, reason TEXT NOT NULL,
                requested_at REAL NOT NULL, state TEXT NOT NULL,
                resume_requested INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '')''')

    def begin(self, uid, sid, source, reason, *, resume_requested=False):
        with self.lock:
            old = self.get(uid)
            if old and old['state'] not in {'DONE', 'DELEGATED'}:
                self.db.execute('UPDATE cancellation_intents SET requested_at=? WHERE order_uuid=?', (time.time(), uid))
                if source not in AUTOMATIC_SOURCES:
                    self.hold(sid)
                return
            self.db.execute('''INSERT INTO cancellation_intents VALUES (?,?,?,?,?,'PENDING',?,'')
                ON CONFLICT(order_uuid) DO UPDATE SET strategy_id=excluded.strategy_id,
                source=excluded.source,reason=excluded.reason,requested_at=excluded.requested_at,
                state='PENDING',resume_requested=excluded.resume_requested,last_error='' ''',
                (uid, sid, source, reason, time.time(), int(resume_requested)))

    def get(self, uid):
        with self.lock:
            row = self.db.execute('SELECT * FROM cancellation_intents WHERE order_uuid=?', (uid,)).fetchone()
        return dict(row) if row else None

    def pending(self, sid):
        with self.lock:
            rows = self.db.execute("SELECT * FROM cancellation_intents WHERE strategy_id=? AND state IN ('PENDING','CONFIRMED','HELD') ORDER BY requested_at", (sid,)).fetchall()
        return [dict(row) for row in rows]

    def failed(self, uid, error):
        with self.lock:
            self.db.execute('UPDATE cancellation_intents SET last_error=? WHERE order_uuid=?', (error, uid))

    def confirmed(self, uid):
        with self.lock:
            self.db.execute("UPDATE cancellation_intents SET state=CASE WHEN state IN ('HELD','DELEGATED') THEN state WHEN last_error='' THEN 'DONE' ELSE 'CONFIRMED' END WHERE order_uuid=?", (uid,))

    def state(self, uid, state):
        if state not in {'DONE', 'DELEGATED', 'HELD'}:
            raise ValueError('invalid cancellation recovery state')
        with self.lock:
            self.db.execute('UPDATE cancellation_intents SET state=? WHERE order_uuid=?', (state, uid))

    def hold(self, sid):
        with self.lock:
            self.db.execute("UPDATE cancellation_intents SET resume_requested=0,state='HELD' WHERE strategy_id=? AND state IN ('PENDING','CONFIRMED','HELD')", (sid,))

    def import_legacy(self, uid, sid):
        """Only migrate a proven automatic failure with no later manual pause."""
        with self.lock:
            if self.get(uid):
                return
            failed = self.db.execute("SELECT * FROM order_events WHERE order_uuid=? AND strategy_id=? AND event='CANCEL_FAILED_OR_UNCERTAIN' ORDER BY id DESC LIMIT 1", (uid, sid)).fetchone()
            if not failed or failed['source'] not in AUTOMATIC_SOURCES:
                return
            later_manual = self.db.execute("SELECT 1 FROM order_events WHERE strategy_id=? AND id>? AND event='CANCEL_REQUESTED' AND source NOT IN ('strategy_safety','market_data','hedge_depth','coverage') LIMIT 1", (sid, failed['id'])).fetchone()
            if later_manual:
                return
            self.begin(uid, sid, failed['source'], failed['reason'], resume_requested=True)
            self.failed(uid, failed['detail'] or 'Legacy cancellation response uncertain')
