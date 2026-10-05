"""Loopback-only API regression checks; no wallet, credentials or CEX writes."""
import json
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from kdf_mm.vps_agent import handler_factory


class WalletWorkerRoutesTests(TestCase):
    def setUp(self):
        self.gui_token = 'g' * 48
        self.worker_token = 'w' * 48
        self.coverage = Mock()
        self.coverage.accept.return_value = {'state': 'OK'}
        self.coverage.hold_publications.return_value = {'state': 'BLOCKED'}
        self.outbox = Mock()
        self.outbox.events.return_value = ()
        self.outbox.acknowledge.return_value.envelope.return_value = {'event_id': 1}
        self.controller = Mock()
        self.controller.rebalance_lock_path = None
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(
            self.controller, self.gui_token, wallet_mode=True,
            wallet_worker_token=self.worker_token, coverage=self.coverage,
            outbox=self.outbox))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, token, body=None):
        req = Request('http://127.0.0.1:%s%s' % (self.server.server_port, path),
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': 'Bearer ' + token,
                               'Content-Type': 'application/json'})
        try:
            with build_opener(ProxyHandler({})).open(req, timeout=2) as r:
                return r.status, json.load(r)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_worker_can_sync_events_and_renew_coverage(self):
        for path, body in [('/v1/events?after_event_id=0', None),
                           ('/v1/events/acknowledge', {'event_id': 1, 'consumer_id': 'local'}),
                           ('/v1/coverage/lease', {'lease': 'test-only'}),
                           ('/v1/coverage/publication-hold', {'recovery_marker': 1})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, self.worker_token, body)[0], 200)
        self.coverage.accept.assert_called_once()
        self.coverage.hold_publications.assert_called_once()

    def test_gui_token_cannot_act_as_worker(self):
        for path, body in [('/v1/events', None),
                           ('/v1/events/acknowledge', {'event_id': 1}),
                           ('/v1/coverage/lease', {'lease': 'test-only'}),
                           ('/v1/coverage/publication-hold', {'recovery_marker': 1})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, self.gui_token, body)[0], 401)
        self.coverage.accept.assert_not_called()

    def test_worker_cannot_use_gui_or_operator_controls(self):
        self.assertEqual(self.request('/v1/strategies/pause-all', self.worker_token, {})[0], 401)
        for path in ['/v1/kdf/stop', '/v1/orders/publish', '/v1/wallet/send/create']:
            for token in [self.gui_token, self.worker_token]:
                self.assertEqual(self.request(path, token, {})[0], 404)

    def test_worker_token_must_be_separate(self):
        with self.assertRaises(ValueError):
            handler_factory(self.controller, self.gui_token, wallet_mode=True,
                            wallet_worker_token=self.gui_token)
