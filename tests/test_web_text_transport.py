"""No DB/account/config imports. Actual Web method and SSE helper are AST loaded.

The loopback curl test is synthetic transport evidence, NOT an authenticated
ChatGPT Web qualification or a model quality score.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

from curl_cffi import CurlHttpVersion, CurlInfo, requests
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END
from services.web_text_transport import (
    MODE_ENV, PROFILE_ENV, WebTextOutcomeUnknown, WebTextStreamAudit,
    checked_payloads, isolated_http1_enabled,
    text_transport_request_id,
    safe_transport_fields,
)

ROOT = Path(__file__).resolve().parents[1]


def load_method():
    tree = ast.parse((ROOT / "services/openai_backend_api.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OpenAIBackendAPI")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_stream_web_text_f_conversation")
    namespace = dict(Iterator=object, Dict=dict, Any=object, ChatRequirements=object)
    namespace.update(CurlHttpVersion=CurlHttpVersion, CurlInfo=CurlInfo, requests=requests, WebTextStreamAudit=WebTextStreamAudit,
                     WebTextOutcomeUnknown=WebTextOutcomeUnknown, checked_payloads=checked_payloads,
                     normalize_thinking_effort=lambda s: s, logger=Mock(),
                     image_failure=lambda code: types.SimpleNamespace(code=code, retryable=False),
                     ensure_ok=lambda response, path: None, iter_sse_payloads=lambda response, **kwargs: iter(response.payloads))
    code = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])), "actual_web_text_method", "exec")
    exec(code, namespace)
    return namespace[method.name], namespace


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-http1-test-")
        self.root = Path(self.temp.name) / "isolated"
        self.root.mkdir()
        self.production = Path(self.temp.name) / "production"
        self.profile = self.root / "profile.json"
        self.manifest = dict(version=1, purpose="isolated-web-text-transport-v1", sourceRoot=str(self.root), productionRoot=str(self.production))
        self.profile.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.env = {MODE_ENV: "http1_v1", PROFILE_ENV: str(self.profile), "DATABASE_URL": "sqlite:///" + (self.root / "data/chatgpt2api.db").as_posix()}

    def tearDown(self):
        self.temp.cleanup()

    def test_default_unchanged_without_manifest_or_database(self):
        self.assertFalse(isolated_http1_enabled(ROOT, {}))

    def test_valid_contained_profile(self):
        self.assertTrue(isolated_http1_enabled(self.root, self.env))

    def test_invalid_mode_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "INVALID_MODE"):
            isolated_http1_enabled(ROOT, {MODE_ENV: "http2-guess"})

    def test_reject_production_root(self):
        with self.assertRaisesRegex(ValueError, "ISOLATED_PROFILE"):
            isolated_http1_enabled(ROOT, self.env)

    def test_reject_shared_external_relative_or_network_database(self):
        for dsn in ["", "sqlite:///data/shared.db", "postgresql://secret@host/db", "sqlite:///" + (self.production / "data/chatgpt2api.db").as_posix()]:
            with self.subTest(dsn_hash=hashlib.sha256(dsn.encode()).hexdigest()[:8]):
                with self.assertRaisesRegex(ValueError, "ISOLATED_PROFILE"):
                    isolated_http1_enabled(self.root, {**self.env, "DATABASE_URL": dsn})

    def test_reject_nested_production(self):
        self.manifest["productionRoot"] = str(self.root.parent)
        self.profile.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            isolated_http1_enabled(self.root, self.env)

    def test_import_guards_precede_any_repository_import(self):
        for file in ["services/config.py", "services/application_database.py"]:
            script = "import runpy; runpy.run_path(" + repr(str(ROOT / file)) + ")"
            result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env={**os.environ, **self.env}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REQUIRES_ISOLATED_PROFILE", result.stderr)
            self.assertNotIn("sqlalchemy", result.stderr)
        self.assertFalse((self.root / "data").exists())


class StreamTests(unittest.TestCase):
    def test_ledger_projection_never_persists_arbitrary_fields_or_native_claim(self):
        raw = WebTextStreamAudit("request").fields()
        raw.update(cookie="private", account="private", nativeFinishVerified=True, headersMs="credential", outcome="private")
        safe = safe_transport_fields(raw)
        self.assertNotIn("private", json.dumps(safe))
        self.assertNotIn("credential", json.dumps(safe))
        self.assertFalse(safe["nativeFinishVerified"])
        self.assertIsNone(safe["headersMs"])
        self.assertIsNone(safe_transport_fields({"version": 1, "protocolRequested": "http1_v1", "requestFingerprint": "private"}))

    def test_actual_ledger_stream_records_safe_telemetry_and_strips_internal_key(self):
        tree = ast.parse((ROOT / "services/log_service.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LoggedCall")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "stream")
        strip = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_strip_internal_response_fields")
        ns = dict(safe_transport_fields=safe_transport_fields, INTERNAL_RESPONSE_KEYS={"_web_text_transport"},
                  _collect_urls=lambda item: [], _collect_account_emails=lambda item: [], _collect_conversation_ids=lambda item: [],
                  collect_image_attempts=lambda item: [])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[strip, method], type_ignores=[])), "actual_ledger_stream", "exec"), ns)
        call = types.SimpleNamespace(_is_image_request=lambda: False, log=Mock())
        raw = WebTextStreamAudit("request").fields()
        raw["secret"] = "never-log"
        output = list(ns["stream"](call, iter([{"choices": [], "_web_text_transport": raw}])))
        self.assertEqual(output, [{"choices": []}])
        safe = call.log.call_args.kwargs["extra"]["web_text_transport"]
        self.assertEqual(safe["requestFingerprint"], hashlib.sha256(b"request").hexdigest())
        self.assertNotIn("never-log", json.dumps(safe))

    def test_request_fingerprint_matches_studio_without_raw_identity(self):
        identity = text_transport_request_id({"metadata": {"studio_request_id": "studio-test-id", "secret": "never-log"}})
        audit = WebTextStreamAudit(identity)
        self.assertEqual(audit.fields()["requestFingerprint"], hashlib.sha256(b"studio-test-id").hexdigest())
        self.assertNotIn("studio-test-id", json.dumps(audit.fields()))
        self.assertEqual(text_transport_request_id({"metadata": {"studio_request_id": {}}, "_call_id": "internal"}), "internal")
        self.assertEqual(text_transport_request_id({"metadata": {"studio_request_id": "a" * 161}}), "")

    def test_typed_error_envelope_never_implies_retry_or_success(self):
        error = WebTextOutcomeUnknown("HTTP2_STREAM_INTERRUPTED")
        self.assertFalse(error.retryable)
        self.assertEqual(error.to_openai_error()["error"]["code"], "http2_stream_interrupted")

    def test_done_is_observed_and_does_not_invent_native_finish(self):
        audit = WebTextStreamAudit("safe-request")
        values = ['{"v":"private content"}', "[DONE]"]
        self.assertEqual(list(checked_payloads(iter(values), audit)), values)
        self.assertEqual(audit.outcome, "transport_complete")
        self.assertFalse(audit.fields()["nativeFinishVerified"])
        self.assertNotIn("private", json.dumps(audit.fields()))

    def test_empty_and_partial_eof_never_manufacture_done(self):
        for values in [[], ['{"v":"partial"}']]:
            audit = WebTextStreamAudit("request")
            with self.assertRaises(WebTextOutcomeUnknown):
                list(checked_payloads(iter(values), audit))
            self.assertFalse(audit.done_seen)
            self.assertEqual(audit.outcome, "unknown")

    def test_curl92_after_payload_is_not_retried(self):
        def failed():
            yield '{"v":"partial"}'
            raise RequestException("sensitive proxy info", code=92)
        audit = WebTextStreamAudit("request")
        with self.assertRaises(RequestException):
            list(checked_payloads(failed(), audit))
        self.assertEqual(audit.fields()["failureCode"], "HTTP2_STREAM_INTERRUPTED")
        self.assertEqual(audit.data_frames, 1)
        self.assertNotIn("sensitive", json.dumps(audit.fields()))

    def backend(self, http1=True):
        method, ns = load_method()
        backend = types.SimpleNamespace(web_text_http1=http1, transport_request_id="req", base_url="https://not-called.invalid",
            _web_text_message=lambda x: dict(author={"role": "user"}, content={"parts": [x["content"]]}),
            _web_text_model_slug=lambda x: x, _prepare_text_conversation=Mock(return_value="never-log"),
            _image_headers=lambda *x: {}, _find_search_value=lambda *x: "", _hide_web_text_conversation=Mock(), session=Mock())
        return method, ns, backend

    def test_actual_method_scopes_http1_and_closes_response_once(self):
        for enabled in [False, True]:
            method, ns, backend = self.backend(enabled)
            response = Mock(payloads=['{"v":"content"}', "[DONE]"], http_version=2)
            backend.session.post.return_value = response
            self.assertEqual(len(list(method(backend, [{"content": "private"}], "gpt-6", object(), "xhigh"))), 2)
            self.assertEqual(backend.session.post.call_count, 1)
            kwargs = backend.session.post.call_args.kwargs
            self.assertEqual(kwargs.get("http_version"), CurlHttpVersion.V1_1 if enabled else None)
            self.assertNotIn("max_tokens", kwargs["json"])
            self.assertTrue(kwargs["json"]["history_and_training_disabled"])
            self.assertEqual(kwargs["json"]["thinking_effort"], "xhigh")
            response.close.assert_called_once()
            if enabled:
                row = ns["logger"].info.call_args.args[0]
                self.assertEqual(row["outcome"], "transport_complete")
                self.assertNotIn("private", json.dumps(row))

    def test_actual_method_post_curl92_is_unknown_and_no_replay(self):
        method, ns, backend = self.backend()
        backend.session.post.side_effect = RequestException("sensitive URL", code=92)
        with self.assertRaisesRegex(WebTextOutcomeUnknown, "HTTP2_STREAM_INTERRUPTED"):
            list(method(backend, [{"content": "private"}], "gpt-6", object()))
        backend.session.post.assert_called_once()
        row = ns["logger"].info.call_args.args[0]
        self.assertIsNone(row["headersMs"])
        self.assertEqual(row["curlCode"], 92)
        self.assertNotIn("sensitive", json.dumps(row))

    def test_actual_method_incomplete_eof_closes_and_fails(self):
        method, ns, backend = self.backend()
        response = Mock(payloads=['{"v":"partial"}'], http_version=2)
        backend.session.post.return_value = response
        with self.assertRaises(WebTextOutcomeUnknown):
            list(method(backend, [{"content": "private"}], "gpt-6", object()))
        response.close.assert_called_once()
        self.assertFalse(ns["logger"].info.call_args.args[0]["doneObserved"])

    def test_actual_session_tls_and_no_account_retry_guards_remain(self):
        tree = ast.parse((ROOT / "services/openai_backend_api.py").read_text(encoding="utf-8"))
        self.assertIn("WEB_TEXT_TRANSPORT_REQUIRES_TLS_VERIFY", ast.unparse(tree))
        self.assertIn("self.web_text_http1 or exc.status_code", ast.unparse(tree))
        conversation = (ROOT / "services/protocol/conversation.py").read_text(encoding="utf-8")
        self.assertIn('not getattr(active_backend, "web_text_http1", False)', conversation)

    def test_real_curl_http1_loopback_sse(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = b'data: {"v":"synthetic"}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        helper = ast.parse((ROOT / "utils/helper.py").read_text(encoding="utf-8"))
        nodes = [n for n in helper.body if isinstance(n, ast.FunctionDef) and n.name in {"iter_sse_payloads", "_format_timeout_secs"}]
        ns = dict(time=time, queue=queue, threading=threading, RequestException=RequestException, STREAM_END=STREAM_END)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])), "actual_sse_helper", "exec"), ns)
        try:
            with requests.Session(trust_env=False, verify=True) as session:
                response = session.post("http://127.0.0.1:" + str(server.server_port), json={"synthetic": True}, stream=True, http_version=CurlHttpVersion.V1_1, timeout=5)
                audit = WebTextStreamAudit("loopback")
                audit.actual_http_version = int(response.http_version)
                self.assertEqual(list(checked_payloads(ns["iter_sse_payloads"](response, max_duration_secs=5), audit))[-1], "[DONE]")
                self.assertEqual(audit.actual_http_version, int(CurlHttpVersion.V1_1))
                self.assertEqual(audit.outcome, "transport_complete")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
