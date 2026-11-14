"""Offline tests for services.web_text_recovery (no DB/account/config imports)."""
import unittest

from curl_cffi.requests.exceptions import RequestException

from services.web_text_recovery import (
    WebTextNotDispatched, WebTextRecoveryFailed, curl_code_of, final_assistant_state,
    is_transport_error, recover_assistant_message, run_predispatch,
)


def conv(status="finished_successfully", end_turn=True, content_type="text", text="答え", role="assistant", recipient="all", finish=None):
    msg = {"author": {"role": role}, "recipient": recipient, "status": status, "end_turn": end_turn,
           "content": {"content_type": content_type, "parts": [text]},
           "metadata": {"finish_details": finish} if finish else {}}
    return {"current_node": "n2", "mapping": {"n1": {"message": {"author": {"role": "user"}}}, "n2": {"message": msg, "parent": "n1"}}}


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Curl92(RequestException):
    pass


class StateTests(unittest.TestCase):
    def test_done(self):
        self.assertEqual(final_assistant_state(conv())[0], "done")
        self.assertEqual(final_assistant_state(conv(end_turn=None, finish={"type": "stop"}))[0], "done")

    def test_pending_while_thinking_or_streaming(self):
        self.assertEqual(final_assistant_state(conv(content_type="thoughts"))[0], "pending")
        self.assertEqual(final_assistant_state(conv(status="in_progress", end_turn=None))[0], "pending")
        self.assertEqual(final_assistant_state(conv(role="user"))[0], "pending")
        self.assertEqual(final_assistant_state(conv(recipient="python"))[0], "pending")
        self.assertEqual(final_assistant_state({})[0], "pending")

    def test_failed_turns_are_never_returned_as_answers(self):
        self.assertEqual(final_assistant_state(conv(finish={"type": "max_tokens"}))[0], "failed")
        self.assertEqual(final_assistant_state(conv(status="finished_partial_completion"))[0], "failed")
        self.assertEqual(final_assistant_state(conv(text=""))[0], "failed")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.now = lambda: self.clock[0]
        self.sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)

    def run_with(self, replies, deadline=100):
        seq = iter(replies)

        def fetch(cid):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item
        return recover_assistant_message(fetch, "c1", deadline, now=self.now, sleep=self.sleep)

    def test_polls_until_finished_and_tolerates_transient_errors(self):
        msg = self.run_with([conv(status="in_progress", end_turn=None), Curl92("curl: (92)"), HttpError(502), conv()])
        self.assertEqual(msg["content"]["parts"], ["答え"])

    def test_refusal_and_missing_conversation_stop(self):
        with self.assertRaises(WebTextRecoveryFailed):
            self.run_with([HttpError(403)])
        with self.assertRaises(WebTextRecoveryFailed):
            self.run_with([HttpError(404)] * 3)

    def test_deadline(self):
        with self.assertRaises(WebTextRecoveryFailed):
            self.run_with([conv(status="in_progress", end_turn=None)] * 100, deadline=12)

    def test_truncated_turn_fails(self):
        with self.assertRaises(WebTextRecoveryFailed):
            self.run_with([conv(finish={"type": "max_tokens"})])


class PredispatchTests(unittest.TestCase):
    def test_retries_transport_errors_then_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RequestException("curl: (28) Operation timed out")
            return "ok"
        self.assertEqual(run_predispatch("bootstrap", fn, sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)

    def test_exhausted_becomes_not_dispatched_503(self):
        def fn():
            raise RequestException("curl: (28) Operation timed out")
        with self.assertRaises(WebTextNotDispatched) as ctx:
            run_predispatch("bootstrap", fn, sleep=lambda s: None)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.to_openai_error()["error"]["code"], "upstream_not_dispatched")

    def test_http_answers_are_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise HttpError(403)
        with self.assertRaises(HttpError):
            run_predispatch("bootstrap", fn, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_classification(self):
        self.assertEqual(curl_code_of(RequestException("Failed to perform, curl: (92) HTTP/2 stream 7")), 92)
        self.assertTrue(is_transport_error(RequestException("x")))
        self.assertTrue(is_transport_error(ConnectionError("x")))
        self.assertFalse(is_transport_error(HttpError(429)))
        self.assertFalse(is_transport_error(ValueError("x")))
        self.assertFalse(is_transport_error(WebTextNotDispatched("bootstrap")))


if __name__ == "__main__":
    unittest.main()
