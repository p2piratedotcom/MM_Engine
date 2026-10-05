"""Cross-process exclusion between KDF publication and manual rebalance."""
from contextlib import contextmanager, ExitStack
import fcntl
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote


def assert_no_pending(journal):
    wallet = Path(str(journal) + '.wallet-send.sqlite3')
    if wallet.exists():
        with sqlite3.connect(wallet.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM sends WHERE state IN ('SUBMITTING','UNKNOWN') LIMIT 1").fetchone():
                raise ValueError('Invio wallet KDF da verificare: aprire Portafoglio → Invia → Storico e aggiornare il TXID. Trading sospeso per non spendere fondi di esito incerto.')
    path = Path(str(journal) + '.rebalance.sqlite3')
    if not path.exists():
        return
    from .rebalance import TERMINAL
    db = sqlite3.connect('file:' + quote(str(path.resolve()), safe='/') + '?mode=ro', uri=True)
    try:
        if any(row[0] not in TERMINAL for row in db.execute('SELECT state FROM orders')):
            raise ValueError('Rebalance CEX pendente: aprire Trading Engine → MY CEXs → Refresh trade status (oppure [8] nella TUI) prima di ripartire')
    finally:
        db.close()


@contextmanager
def _file_lock(path, *, exclusive, deadline, busy_message):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(busy_message or 'Rebalance CEX o pubblicazione KDF in corso: riprovare dopo il completamento') from None
                time.sleep(min(.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def rebalance_guard(path, *, exclusive=False, wait_seconds=0, busy_message=None):
    if not path:
        yield
        return
    deadline = time.monotonic() + max(0, wait_seconds)
    with ExitStack() as held:
        if exclusive:
            # Announce the writer before waiting for existing cycles to drain.
            # New cooperative cycles cannot continually overtake Execute.
            held.enter_context(_file_lock(str(path) + '.admission', exclusive=True,
                                          deadline=deadline, busy_message=busy_message))
        held.enter_context(_file_lock(path, exclusive=exclusive,
                                      deadline=deadline, busy_message=busy_message))
        if not exclusive:
            assert_no_pending(str(path).removesuffix('.rebalance.lock'))
        yield


@contextmanager
def worker_cycle_guard(path):
    """Admit one cycle, then let a pending writer drain its shared gate.

    Release admission BEFORE runtime: an already admitted cycle may complete
    its nested local RPC/ACK calls while Execute waits for the main gate.
    No operational work runs without the existing shared rebalance guard.
    """
    if not path:
        yield
        return
    with ExitStack() as held:
        with _file_lock(str(path) + '.admission', exclusive=False,
                        deadline=time.monotonic(),
                        busy_message='Operazione esclusiva richiesta: ciclo CEX rinviato'):
            held.enter_context(rebalance_guard(path))
        yield
