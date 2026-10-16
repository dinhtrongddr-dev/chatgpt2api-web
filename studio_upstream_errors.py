import json
import re
from datetime import datetime, timezone


def safe_model(model):
    return re.sub(r"[^A-Za-z0-9_.:/-]", "", str(model or "unknown"))[:300]


def safe_reset(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def model_limit_from_init(payload, model):
    model = safe_model(model)
    detail = {"kind": "usage", "requestedModel": model, "checkedAt": datetime.now(timezone.utc).isoformat(), "source": "provider", "state": "not_reported"}
    limits = payload.get("model_limits")
    if not isinstance(limits, list):
        raise ValueError("Model limit metadata unavailable")
    for item in limits:
        if isinstance(item, dict) and item.get("model_slug") == model:
            detail["state"] = "limited"
            reset = safe_reset(item.get("resets_after"))
            if reset:
                detail["resetAt"] = reset
            return detail
    banner = payload.get("banner_info") or {}
    if isinstance(banner, dict) and banner and payload.get("intended_default_model_slug") == model and payload.get("default_model_slug") != model:
        detail["state"] = "limited"
        reset = safe_reset(banner.get("resets_after"))
        if reset:
            detail["resetAt"] = reset
    return detail


def read_model_limit(backend, model):
    bootstrap = getattr(backend, "_bootstrap", None)
    if bootstrap:
        bootstrap()
    return model_limit_from_init(backend._get_conversation_init(model), model)


def enrich_usage_payload(backend, payload, model):
    if payload == "[DONE]":
        return payload
    try:
        event = json.loads(payload)
    except (ValueError, TypeError):
        return payload
    if not isinstance(event, dict) or not event.get("error") or event.get("error_code") != "usage_limit":
        return payload
    try:
        detail = read_model_limit(backend, model)
    except Exception:
        detail = {"kind": "usage", "requestedModel": safe_model(model), "checkedAt": datetime.now(timezone.utc).isoformat(), "source": "unknown"}
    detail["state"] = "limited"
    event["studio_limit"] = detail
    return json.dumps(event, ensure_ascii=True)


def raise_upstream_error(event):
    if not event.get("error"):
        return
    code = re.sub(r"[^A-Za-z0-9_/-]", "", str(event.get("error_code") or "unknown"))[:80]
    if code == "usage_limit":
        raw = event.get("studio_limit") or {}
        raw = raw if isinstance(raw, dict) else {}
        detail = {"kind": "usage", "requestedModel": safe_model(raw.get("requestedModel")), "checkedAt": safe_reset(raw.get("checkedAt")) or datetime.now(timezone.utc).isoformat(), "source": "provider" if raw.get("source") == "provider" else "unknown", "state": "limited"}
        reset = safe_reset(raw.get("resetAt"))
        if reset:
            detail["resetAt"] = reset
        raise RuntimeError("UPSTREAM_USAGE_LIMIT: STUDIO_LIMIT=" + json.dumps(detail, ensure_ascii=True))
    raise RuntimeError("UPSTREAM_CONVERSATION_ERROR: upstream rejected the conversation; code=" + code)
