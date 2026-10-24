"""Opt-in, isolated Web text transport experiment. No account/config/DB imports.

The default leaves existing transport behavior unchanged. HTTP/1.1 is an
experiment, not a claim that the upstream cause of curl92 has been fixed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping


MODE_ENV = "CHATGPT2API_WEB_TEXT_TRANSPORT"
PROFILE_ENV = "CHATGPT2API_WEB_TEXT_ISOLATED_PROFILE"


def isolated_http1_enabled(source_root: Path, env: Mapping[str, str] | None = None) -> bool:
    """Validate actual runtime paths before repositories/config can be imported.

    The manifest is configuration, not authentication. Never clone an account
    database or credentials into the profile to satisfy this check.
    """
    env = os.environ if env is None else env
    mode = env.get(MODE_ENV, "default")
    if mode == "default":
        return False
    if mode != "http1_v1":
        raise ValueError("WEB_TEXT_TRANSPORT_INVALID_MODE")
    try:
        manifest_path = Path(env[PROFILE_ENV])
        if not manifest_path.is_absolute():
            raise ValueError()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["version"] != 1 or manifest["purpose"] != "isolated-web-text-transport-v1":
            raise ValueError()
        root = Path(manifest["sourceRoot"])
        production = Path(manifest["productionRoot"])
        if not root.is_absolute() or not production.is_absolute():
            raise ValueError()
        root, production = root.resolve(), production.resolve()
        if root != source_root.resolve() or root == production or root.is_relative_to(production) or production.is_relative_to(root):
            raise ValueError()
        if not manifest_path.resolve().is_relative_to(root):
            raise ValueError()
        # Existing source derives config/data from __file__. Bind those actual
        # locations, including junction/symlink resolution, to this copied root.
        if (root / "data").resolve().parent != root or (root / "config.json").resolve().parent != root:
            raise ValueError()
        expected_db = (root / "data" / "chatgpt2api.db").resolve()
        if expected_db.parent != (root / "data").resolve():
            raise ValueError()
        if env.get("DATABASE_URL") != "sqlite:///" + expected_db.as_posix():
            raise ValueError()
    except (KeyError, OSError, TypeError, ValueError):
        # Fixed public text only; no path, DSN or manifest contents in errors.
        raise ValueError("WEB_TEXT_TRANSPORT_REQUIRES_ISOLATED_PROFILE") from None
    return True


class WebTextOutcomeUnknown(ConnectionError):
    def __init__(self, code: str):
        self.code = code
        self.status_code = 502
        self.retryable = False
        super().__init__(code + ": upstream outcome unknown; do not replay POST")

    def to_openai_error(self) -> dict:
        return {"error": {"message": str(self), "type": "server_error", "code": self.code.lower()}}


def text_transport_request_id(body: dict) -> str:
    metadata = body.get("metadata")
    value = metadata.get("studio_request_id") if isinstance(metadata, dict) else None
    if isinstance(value, str) and 0 < len(value) <= 160:
        return value
    value = body.get("_call_id")
    return value if isinstance(value, str) and 0 < len(value) <= 160 else ""


def safe_transport_fields(value: object) -> dict | None:
    """Allowlist internal ledger metadata; never pass arbitrary upstream data."""
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("protocolRequested") != "http1_v1":
        return None
    fingerprint = value.get("requestFingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch("[a-f0-9]{64}", fingerprint):
        return None
    row = {"version": 1, "requestFingerprint": fingerprint, "protocolRequested": "http1_v1", "nativeFinishVerified": False}
    for key in ("actualHttpVersion", "headersMs", "ttfbMs", "firstPayloadMs", "dataFrames", "contentFrames", "curlCode", "durationMs"):
        item = value.get(key)
        row[key] = item if type(item) is int and item >= 0 else None
    for key, choices in {
        "lastMarker": {"none", "data", "done"},
        "outcome": {"unknown", "http_error", "transport_complete"},
        "failureCode": {"HTTP2_STREAM_INTERRUPTED", "UPSTREAM_OUTCOME_UNKNOWN", "UPSTREAM_HTTP_ERROR"},
    }.items():
        item = value.get(key)
        row[key] = item if isinstance(item, str) and item in choices else None
    row["doneObserved"] = value.get("doneObserved") is True
    return row


@dataclass
class WebTextStreamAudit:
    request_id: str
    protocol: str = "http1_v1"
    started: float = field(default_factory=time.monotonic)
    first_payload_ms: int | None = None
    data_frames: int = 0
    content_frames: int | None = None
    done_seen: bool = False
    last_marker: str = "none"
    actual_http_version: int | None = None
    headers_ms: int | None = None
    ttfb_ms: int | None = None
    curl_code: int | None = None
    outcome: str = "unknown"
    failure_code: str | None = None

    def elapsed_ms(self) -> int:
        return max(0, round((time.monotonic() - self.started) * 1000))

    def observe(self, payload: str) -> None:
        if self.first_payload_ms is None:
            self.first_payload_ms = self.elapsed_ms()
        self.data_frames += 1
        self.done_seen = payload == "[DONE]"
        self.last_marker = "done" if self.done_seen else "data"

    def failed(self, exc: BaseException) -> None:
        code = getattr(exc, "code", None)
        self.curl_code = int(code) if isinstance(code, int) else None
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 400 and not isinstance(exc, WebTextOutcomeUnknown):
            self.outcome = "http_error"
            self.failure_code = "UPSTREAM_HTTP_ERROR"
        else:
            self.failure_code = "HTTP2_STREAM_INTERRUPTED" if self.curl_code == 92 else "UPSTREAM_OUTCOME_UNKNOWN"

    def fields(self) -> dict:
        return {
            "event": "web_text_transport_v1", "version": 1,
            "requestFingerprint": hashlib.sha256(self.request_id.encode()).hexdigest(),
            "stage": "web_text", "protocolRequested": self.protocol,
            "actualHttpVersion": self.actual_http_version, "headersMs": self.headers_ms, "ttfbMs": self.ttfb_ms,
            # First SSE payload != raw network first byte. Do not call it TTFB.
            "firstPayloadMs": self.first_payload_ms, "dataFrames": self.data_frames,
            "contentFrames": self.content_frames, "lastMarker": self.last_marker,
            "doneObserved": self.done_seen, "nativeFinishVerified": False,
            "curlCode": self.curl_code, "failureCode": self.failure_code,
            "outcome": self.outcome, "durationMs": self.elapsed_ms(),
        }


def checked_payloads(payloads: Iterator[str], audit: WebTextStreamAudit) -> Iterator[str]:
    """Require the actual terminal marker; never manufacture DONE on EOF.

    No POST replay, reconnect, timeout extension, or native finish inference.
    """
    try:
        for payload in payloads:
            audit.observe(payload)
            if audit.done_seen:
                audit.outcome = "transport_complete"
                yield payload
                return
            yield payload
        raise WebTextOutcomeUnknown("UPSTREAM_OUTCOME_UNKNOWN")
    except Exception as exc:
        audit.failed(exc)
        raise
    finally:
        close = getattr(payloads, "close", None)
        if close:
            close()
