import json
import unittest
import sqlite3
import tempfile
from pathlib import Path
from kdf_mm.strategy_store import StrategyStore

class OrderNumberingTests(unittest.TestCase):
    def test_creation_number_is_stable_and_not_name_sorted(self):
        store = StrategyStore(':memory:')
        self.addCleanup(store.close)
        for name in ('z-last-name', 'a-first-name'):
            store.db.execute('INSERT INTO strategies(id,spec) VALUES (?,?)', (name, json.dumps({})))
        rows = store.rows()
        self.assertEqual([(r['id'],r['creation_number']) for r in rows], [('z-last-name',1),('a-first-name',2)])
        store.db.execute("UPDATE strategies SET state='DELETED' WHERE id='z-last-name'")
        self.assertEqual(store.rows()[0]['creation_number'], 2)
        store.db.execute('INSERT INTO strategies(id,spec) VALUES (?,?)', ('third', '{}'))
        self.assertEqual(store.rows()[-1]['creation_number'], 3)
        self.assertEqual(len(store.rows(include_deleted=True)), 3)

    def test_numbers_survive_reopen_and_vacuum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'strategies.sqlite3'
            store = StrategyStore(path)
            for name in ('z', 'a'):
                store.db.execute('INSERT INTO strategies(id,spec) VALUES (?,?)', (name, '{}'))
            store.db.execute("UPDATE strategies SET creation_number=10 WHERE id='a'")
            store.db.execute('VACUUM')
            store.close()
            store = StrategyStore(path)
            self.addCleanup(store.close)
            self.assertEqual([(r['id'],r['creation_number']) for r in store.rows()], [('z',1),('a',10)])
            store.db.execute("INSERT INTO strategies(id,spec) VALUES ('next','{}')")
            self.assertEqual(store.rows()[-1]['creation_number'], 11)

    def test_legacy_database_migration_retains_specs_and_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE strategies (id TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER DEFAULT 0, state TEXT DEFAULT 'PAUSED', detail TEXT DEFAULT '', last_write REAL DEFAULT 0, evidence TEXT DEFAULT 'null', confirmations INTEGER DEFAULT 0, preview TEXT DEFAULT '{}')")
            db.execute("INSERT INTO strategies(id,spec) VALUES ('z', ?)", (json.dumps({'marker': 'original'}),))
            db.execute("INSERT INTO strategies(id,spec) VALUES ('a','{}')")
            db.commit(); db.close()
            store = StrategyStore(path)
            self.addCleanup(store.close)
            self.assertEqual([(r['id'],r['creation_number']) for r in store.rows()], [('z',1),('a',2)])
            self.assertEqual(store.get('z')['spec'], {'marker': 'original'})
