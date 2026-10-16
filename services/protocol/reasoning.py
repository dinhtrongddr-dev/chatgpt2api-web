from __future__ import annotations

from typing import Any


def normalize_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none"}:
        return ""
    if normalized in {"low", "medium", "high"}:
        return normalized
    # ChatGPT Web exposes the top tier as ``extended``. Keep accepting the
    # OpenAI-compatible ``xhigh``/``max`` spellings so callers do not silently
    # lose the requested reasoning level before the Web request is built.
    if normalized in {"xhigh", "extended", "max"}:
        return "extended"
    return ""


def thinking_effort_from_body(body: dict[str, Any]) -> str:
    if "thinking_effort" in body:
        return normalize_thinking_effort(body.get("thinking_effort"))
    if "reasoning_effort" in body:
        return normalize_thinking_effort(body.get("reasoning_effort"))
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        return normalize_thinking_effort(reasoning.get("effort"))
    return ""
