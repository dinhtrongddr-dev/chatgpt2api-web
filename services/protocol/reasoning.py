from __future__ import annotations

from typing import Any


def normalize_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    # ChatGPT Web conduit upstream strictly accepts either "extended" or omitted.
    # Passing "low", "medium", "high" triggers 422 Unprocessable Entity from upstream.
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
