"""Web text transport resilience (2026-11-11).

Two distinct failure classes used to surface as an opaque 502:

1. Pre-dispatch: bootstrap / chat-requirements / prepare failed on the network.
   No conversation POST was sent, so nothing ran upstream. Those steps are
   retried a bounded number of times; if they still fail the client gets a
   503 ``upstream_not_dispatched`` that it may retry safely.
2. Mid-stream: chatgpt.com reset the HTTP/2 stream (curl 92) or the SSE ended
   without ``[DONE]`` after the conversation was created. The generation keeps
   running server-side, so the finished assistant turn is read back with
   GET /backend-api/conversation/{id}. That is reconciliation, never a replay.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from curl_cffi import requests

TRANSPORT_ERRORS = (requests.exceptions.RequestException, ConnectionError, TimeoutError)
_CURL_CODE = re.compile(r"curl:\s*\((\d+)\)")


class WebTextNotDispatched(ConnectionError):
    """No conversation POST left the gateway; the caller may retry safely."""

    def __init__(self, stage: str):
        self.code = "upstream_not_dispatched"
        self.stage = stage
        self.status_code = 503
        self.retryable = True
        super().__init__(f"UPSTREAM_NOT_DISPATCHED: {stage} failed before the conversation was sent; safe to retry")

    def to_openai_error(self) -> dict:
        return {"error": {"message": str(self), "type": "server_error", "code": self.code, "stage": self.stage}}


class WebTextNoConversation(ConnectionError):
    """The stream broke before chatgpt.com sent a conversation_id.

    The turn cannot be read back (history-disabled turns are not listed), so the
    outcome is UNKNOWN. The code lets a client that has its owner's permission
    resend content-only work once; the gateway itself never resends.
    """

    def __init__(self, cause: BaseException):
        self.code = "upstream_outcome_unknown_no_conversation"
        self.status_code = 502
        self.curl_code = curl_code_of(cause)
        super().__init__(f"UPSTREAM_OUTCOME_UNKNOWN_NO_CONVERSATION: stream broke before a conversation id "
                         f"(curl {self.curl_code}); the turn cannot be read back")

    def to_openai_error(self) -> dict:
        return {"error": {"message": str(self), "type": "server_error", "code": self.code, "curl_code": self.curl_code}}


class WebTextRecoveryFailed(RuntimeError):
    pass


def curl_code_of(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code > 0:
        return code
    match = _CURL_CODE.search(str(exc))
    return int(match.group(1)) if match else None


def is_transport_error(exc: BaseException) -> bool:
    """Network-level failure only; HTTP status errors (401/403/429/...) are answers, not transport loss."""
    if isinstance(exc, (WebTextNotDispatched, WebTextNoConversation)):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:
        return False
    return isinstance(exc, TRANSPORT_ERRORS)


def run_predispatch(stage: str, fn: Callable[[], Any], *, attempts: int = 3, sleep: Callable[[float], None] = time.sleep,
                    on_retry: Callable[[str, int, BaseException], None] | None = None) -> Any:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transport_error(exc):
                raise
            if attempt == attempts - 1:
                raise WebTextNotDispatched(stage) from exc
            if on_retry:
                on_retry(stage, attempt + 1, exc)
            sleep(2.0 * (attempt + 1))
    raise AssertionError("unreachable")


def _message_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    text = "".join(part for part in parts if isinstance(part, str)) if isinstance(parts, list) else ""
    return text or str(content.get("text") or "")


def _is_visible_assistant(message: dict) -> bool:
    content = message.get("content") or {}
    content_type = str(content.get("content_type") or "").lower()
    recipient = str(message.get("recipient") or "all").lower()
    return (str((message.get("author") or {}).get("role") or "").lower() == "assistant"
            and content_type in {"text", "multimodal_text"} and recipient in {"", "all"})


def final_assistant_state(conversation: dict) -> tuple[str, dict | None]:
    """Return ("done"|"pending"|"failed", message) for the turn at current_node."""
    mapping = conversation.get("mapping") if isinstance(conversation, dict) else None
    node_id = conversation.get("current_node") if isinstance(conversation, dict) else None
    if not isinstance(mapping, dict) or not node_id or node_id not in mapping:
        return "pending", None
    message = (mapping[node_id] or {}).get("message") or {}
    if not _is_visible_assistant(message):
        # Still thinking / tool step, or the user turn is the latest node.
        return "pending", None
    status = str(message.get("status") or "").lower()
    finish = ((message.get("metadata") or {}).get("finish_details") or {})
    finish_type = str(finish.get("type") or "").lower()
    if status in {"finished_partial_completion", "failed", "cancelled"} or finish_type in {"max_tokens", "interrupted"}:
        return "failed", message
    if status == "finished_successfully" and (message.get("end_turn") is True or finish_type == "stop"):
        return ("done", message) if _message_text(message).strip() else ("failed", message)
    return "pending", None


def recover_assistant_message(fetch: Callable[[str], dict], conversation_id: str, deadline: float, *,
                              now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                              poll_secs: float = 5.0) -> dict:
    """Poll the conversation until the assistant turn finishes. GET only."""
    missing = 0
    last_error = ""
    while True:
        try:
            state, message = final_assistant_state(fetch(conversation_id))
            missing = 0
            if state == "done" and message is not None:
                return message
            if state == "failed":
                raise WebTextRecoveryFailed("assistant turn ended without a complete answer")
        except WebTextRecoveryFailed:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise WebTextRecoveryFailed(f"conversation read refused: HTTP {status}") from exc
            if status == 404:
                missing += 1
                if missing >= 3:
                    raise WebTextRecoveryFailed("conversation not found") from exc
            last_error = type(exc).__name__
        remaining = deadline - now()
        if remaining <= 0:
            raise WebTextRecoveryFailed("recovery deadline reached" + (f" ({last_error})" if last_error else ""))
        sleep(min(poll_secs, remaining))
