"""2026-11-12 (owner-authorized): receipts keep the transport facts the web layer proved.

Offline: a fake store records what the receipt engine writes; no network, no account.
"""
import threading
import time
import unittest

from services.studio_receipts import ReceiptEngine
from services.web_text_recovery import WebTextNoConversation, WebTextNotDispatched


class FakeStore:
    def __init__(self):
        self.updates = []
        self.released = 0

    def recover(self):
        pass

    def update(self, row, **values):
        self.updates.append(values)
        return True

    def release(self, row):
        self.released += 1

    def complete(self, *args):
        raise AssertionError('must not complete')


class Http502(Exception):
    status_code = 502


def run(adapter):
    store = FakeStore()
    engine = ReceiptEngine(store, adapter, heartbeat=0.01)
    try:
        state = {'text': '', 'cancel': threading.Event(), 'started': time.monotonic(), 'last_visible': None, 'frames': 0}
        engine._work({'owner': 'o', 'logical_id': 'x', 'fence': 'f'}, {}, state)
    finally:
        engine.stopped = True
    return store


def raising(exc):
    def adapter(body, cancel):
        raise exc
        yield  # pragma: no cover
    return adapter


class ReceiptClassificationTest(unittest.TestCase):
    def test_not_dispatched_is_a_verified_failure(self):
        store = run(raising(WebTextNotDispatched('prepare')))
        last = store.updates[-1]
        self.assertEqual((last['status'], last['phase'], last['error_type'], last['native_count']),
                         ('FAILED_VERIFIED', 'REJECTED_PRE_DISPATCH', 'VERIFIED_NO_NATIVE_SUBMIT', 0))
        self.assertEqual(store.released, 1)

    def test_no_conversation_keeps_its_code_despite_the_502_status(self):
        store = run(raising(WebTextNoConversation(ConnectionError('curl: (92) HTTP/2 stream 7 was not closed cleanly'))))
        last = store.updates[-1]
        self.assertEqual((last['status'], last['error_type'], last['frames']), ('UNKNOWN', 'UPSTREAM_OUTCOME_UNKNOWN_NO_CONVERSATION', 0))

    def test_other_502_stays_generic_unknown(self):
        store = run(raising(Http502('bad gateway')))
        self.assertEqual(store.updates[-1]['error_type'], 'UPSTREAM_HTTP_502')


if __name__ == '__main__':
    unittest.main()
