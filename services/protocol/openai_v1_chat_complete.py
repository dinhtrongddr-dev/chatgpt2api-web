from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.chat_completion_cache import cache_key, chat_completion_cache, normalize_text_messages
from services.protocol.conversation import (
    ConversationRequest,
    ImageOutput,
    collect_image_outputs,
    collect_text,
    count_message_image_tokens,
    count_message_text_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    sanitize_output_text,
    stream_image_outputs_with_pool,
    stream_text_deltas,
    text_backend,
)
from services.protocol.reasoning import thinking_effort_from_body
from services.web_text_transport import WebTextStreamAudit, text_transport_request_id
from services.protocol.web_search_tool import (
    WEB_SEARCH_TOOL_TYPES,
    has_unsupported_tools,
    is_web_search_chat_request,
    run_web_search,
    search_query_from_messages,
    text_with_url_citations,
)
from utils.helper import build_chat_image_markdown_content, extract_chat_image, extract_chat_prompt, is_image_chat_request, parse_image_count
from utils.image_tokens import (
    chat_usage_from_image_usage,
    count_image_inputs_tokens,
    count_image_output_items_tokens,
    image_usage,
    image_output_metadata,
)

TOOL_UNAVAILABLE_SYSTEM_MESSAGE = (
    "This compatibility backend cannot execute local tools, shell commands, non-search tools, "
    "or file operations. Do not claim to have run tools or inspected external resources. "
    "If a user asks you to use a tool, say that tool execution is unavailable through this backend."
)


def completion_chunk(model: str, delta: dict[str, Any], finish_reason: str | None = None, completion_id: str = "", created: int | None = None) -> dict[str, Any]:
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion_response(
    model: str,
    content: str,
    created: int | None = None,
    messages: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt_text_tokens = count_message_text_tokens(messages, model) if messages else 0
    prompt_image_tokens = count_message_image_tokens(messages, model) if messages else 0
    prompt_tokens = prompt_text_tokens + prompt_image_tokens
    completion_tokens = count_text_tokens(content, model) if messages else 0
    message = {"role": "assistant", "content": content}
    if annotations:
        message["annotations"] = annotations
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "text_tokens": prompt_text_tokens,
                "image_tokens": prompt_image_tokens,
                "cached_tokens": 0,
            },
            "completion_tokens_details": {
                "text_tokens": completion_tokens,
                "image_tokens": 0,
                "reasoning_tokens": 0,
            },
        },
    }


def _with_log_metadata(
    payload: dict[str, Any],
    account_email: str = "",
    conversation_id: str = "",
    image_urls: Iterable[str] | None = None,
    image_attempts: Iterable[dict[str, Any]] | None = None,
    image_data: list[dict[str, Any]] | None = None,
    transport_audit: WebTextStreamAudit | None = None,
) -> dict[str, Any]:
    if account_email:
        payload["_account_email"] = account_email
    if conversation_id:
        payload["_conversation_id"] = conversation_id
    urls = [str(url).strip() for url in image_urls or [] if str(url).strip()]
    if urls:
        payload["_image_urls"] = list(dict.fromkeys(urls))
    attempts = [dict(item) for item in image_attempts or [] if isinstance(item, dict)]
    if attempts:
        payload["_image_attempts"] = attempts
    if image_data:
        payload["_image_metadata"] = image_output_metadata(image_data)
    if isinstance(transport_audit, WebTextStreamAudit):
        payload["_web_text_transport"] = transport_audit.fields()
    return payload


def _backend_account_email(backend: object) -> str:
    return str(getattr(backend, "account_email", "") or "").strip()


def preprocess_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    processed = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip().lower()
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")

        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            xml_blocks = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                args = fn.get("arguments") or tc.get("arguments") or "{}"
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                else:
                    args_str = str(args)
                xml_blocks.append(f"<tool_calls><tool_call><tool_name>{name}</tool_name><parameters>{args_str}</parameters></tool_call></tool_calls>")
            text_content = str(content or "").strip()
            combined = f"{text_content}\n" + "\n".join(xml_blocks) if text_content else "\n".join(xml_blocks)
            processed.append({"role": "assistant", "content": combined})
        elif role in ("tool", "function"):
            tool_id = msg.get("tool_call_id") or msg.get("name") or "tool"
            text_content = str(content or "")
            processed.append({"role": "user", "content": f"[Tool Result for {tool_id}]:\n{text_content}"})
        else:
            processed.append(dict(msg))
    return processed


def stream_text_chat_completion(
    backend,
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
    tools: list[dict[str, Any]] | None = None,
    call_id: str = "",
) -> Iterator[dict[str, Any]]:
    from services.protocol.anthropic_v1_messages import parse_tool_calls, strip_tool_markup
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    request = ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort, call_id=call_id)

    if tools:
        accumulated_text = ""
        for delta_text in stream_text_deltas(backend, request):
            accumulated_text += delta_text

        calls = parse_tool_calls(accumulated_text)
        if calls:
            clean_text = strip_tool_markup(accumulated_text)
            if clean_text:
                yield _with_log_metadata(
                    completion_chunk(model, {"role": "assistant", "content": clean_text}, None, completion_id, created),
                    _backend_account_email(backend),
                )
            tool_call_deltas = []
            for idx, (name, args) in enumerate(calls):
                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                tc_dict = {
                    "index": idx,
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args_str,
                    }
                }
                tool_call_deltas.append(tc_dict)

            delta: dict[str, Any] = {"tool_calls": tool_call_deltas}
            if not clean_text:
                delta["role"] = "assistant"
                delta["content"] = None
            yield _with_log_metadata(
                completion_chunk(model, delta, None, completion_id, created),
                _backend_account_email(backend),
            )
            yield _with_log_metadata(
                completion_chunk(model, {}, "tool_calls", completion_id, created),
                _backend_account_email(backend),
                transport_audit=getattr(backend, "web_text_audit", None),
            )
            return
        else:
            yield _with_log_metadata(
                completion_chunk(model, {"role": "assistant", "content": accumulated_text}, None, completion_id, created),
                _backend_account_email(backend),
            )
            yield _with_log_metadata(
                completion_chunk(model, {}, "stop", completion_id, created),
                _backend_account_email(backend),
                transport_audit=getattr(backend, "web_text_audit", None),
            )
            return

    sent_role = False
    for delta_text in stream_text_deltas(backend, request):
        if not sent_role:
            sent_role = True
            yield _with_log_metadata(
                completion_chunk(model, {"role": "assistant", "content": delta_text}, None, completion_id, created),
                _backend_account_email(backend),
            )
        else:
            yield _with_log_metadata(
                completion_chunk(model, {"content": delta_text}, None, completion_id, created),
                _backend_account_email(backend),
            )
    if not sent_role:
        yield _with_log_metadata(
            completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created),
            _backend_account_email(backend),
        )
    yield _with_log_metadata(completion_chunk(model, {}, "stop", completion_id, created), _backend_account_email(backend), transport_audit=getattr(backend, "web_text_audit", None))


def collect_chat_content(chunks: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        choices = chunk.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        content = str(delta.get("content") or "")
        if content:
            parts.append(content)
    return "".join(parts)


def chat_messages_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return [message for message in messages if isinstance(message, dict)]
    prompt = str(body.get("prompt") or "").strip()
    if prompt:
        return [{"role": "user", "content": prompt}]
    raise HTTPException(status_code=400, detail={"error": "messages or prompt is required"})


def chat_image_args(body: dict[str, Any]) -> tuple[str, str, int, list[tuple[bytes, str, str]], str | None]:
    model = str(body.get("model") or "gpt-image-2").strip() or "gpt-image-2"
    prompt = extract_chat_prompt(body)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    images = [
        (data, f"image_{idx}.png", mime)
        for idx, (data, mime) in enumerate(extract_chat_image(body), start=1)
    ]
    base_url = str(body.get("base_url") or "").strip() or None
    return model, prompt, parse_image_count(body.get("n")), images, base_url


def text_chat_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]] | None]:
    from services.protocol.anthropic_v1_messages import build_tool_prompt, merge_system
    model = str(body.get("model") or "auto").strip() or "auto"
    raw_messages = chat_messages_from_body(body)
    preprocessed = preprocess_openai_messages(raw_messages)

    tools = body.get("tools")
    active_tools = tools if isinstance(tools, list) and tools else None

    system_text = ""
    filtered_messages = []
    for msg in preprocessed:
        if str(msg.get("role") or "").lower() == "system":
            content = str(msg.get("content") or "").strip()
            system_text = f"{system_text}\n\n{content}".strip() if system_text else content
        else:
            filtered_messages.append(msg)

    if active_tools:
        system_text = merge_system(system_text, build_tool_prompt(active_tools))

    messages = normalize_text_messages(normalize_messages(filtered_messages, system_text))
    return model, messages, active_tools


def chat_completion_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in annotations:
        if item.get("type") != "url_citation":
            continue
        output.append({
            "type": "url_citation",
            "url_citation": {
                "start_index": item.get("start_index", 0),
                "end_index": item.get("end_index", 0),
                "url": item.get("url", ""),
                "title": item.get("title", ""),
            },
        })
    return output


def web_search_chat_response(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    query = search_query_from_messages(messages)
    if not query:
        raise HTTPException(status_code=400, detail={"error": "messages or prompt is required for web search"})
    text, annotations = text_with_url_citations(run_web_search(query))
    return completion_response(
        model,
        text,
        messages=messages,
        annotations=chat_completion_annotations(annotations),
    )


def stream_web_search_chat_completion(messages: list[dict[str, Any]], model: str) -> Iterator[dict[str, Any]]:
    query = search_query_from_messages(messages)
    if not query:
        raise HTTPException(status_code=400, detail={"error": "messages or prompt is required for web search"})
    text, _annotations = text_with_url_citations(run_web_search(query))
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yield completion_chunk(model, {"role": "assistant", "content": text}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def image_result_content(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, list) and data:
        return build_chat_image_markdown_content(result)
    return str(result.get("message") or "Image generation completed.")


def image_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    model, prompt, n, images, base_url = chat_image_args(body)
    result = collect_image_outputs(stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
        base_url=base_url,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    )))
    response = completion_response(model, image_result_content(result), int(result.get("created") or 0) or None)
    usage = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data")),
    )
    response["usage"] = chat_usage_from_image_usage(usage)
    _with_log_metadata(
        response,
        str(result.get("_account_email") or ""),
        str(result.get("_conversation_id") or ""),
        result.get("_image_urls") if isinstance(result.get("_image_urls"), list) else None,
        result.get("_image_attempts") if isinstance(result.get("_image_attempts"), list) else None,
        result.get("data"),
    )
    return response


def image_chat_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    model, prompt, n, images, base_url = chat_image_args(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
        base_url=base_url,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    ))
    yield from stream_image_chat_completion(image_outputs, model)


def stream_image_chat_completion(image_outputs: Iterable[ImageOutput], model: str) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    for output in image_outputs:
        content = ""
        if output.kind == "progress":
            content = output.text
            sent_text += content
        elif output.kind == "result":
            content = build_chat_image_markdown_content({"data": output.data})
        elif output.kind == "message":
            content = output.text[len(sent_text):] if output.text.startswith(sent_text) else output.text
        if not content:
            continue
        if not sent_role:
            sent_role = True
            yield _with_log_metadata(
                completion_chunk(model, {"role": "assistant", "content": content}, None, completion_id, created),
                output.account_email,
                output.conversation_id,
                output.image_urls,
                output.image_attempts,
                output.data if output.kind == "result" else None,
            )
        else:
            yield _with_log_metadata(
                completion_chunk(model, {"content": content}, None, completion_id, created),
                output.account_email,
                output.conversation_id,
                output.image_urls,
                output.image_attempts,
                output.data if output.kind == "result" else None,
            )
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def text_completion_response(
    model: str,
    messages: list[dict[str, Any]],
    thinking_effort: str,
    tools: list[dict[str, Any]] | None = None,
    call_id: str = "",
) -> dict[str, Any]:
    from services.protocol.anthropic_v1_messages import parse_tool_calls, strip_tool_markup
    backend = text_backend(model=model)
    raw_text = collect_text(backend, ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort, call_id=call_id))

    calls = parse_tool_calls(raw_text) if tools else []
    if calls:
        clean_text = strip_tool_markup(raw_text)
        openai_tool_calls = [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                }
            }
            for name, args in calls
        ]
        prompt_text_tokens = count_message_text_tokens(messages, model) if messages else 0
        prompt_image_tokens = count_message_image_tokens(messages, model) if messages else 0
        prompt_tokens = prompt_text_tokens + prompt_image_tokens
        completion_tokens = count_text_tokens(raw_text, model)

        response = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": clean_text or None,
                    "tool_calls": openai_tool_calls,
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {
                    "text_tokens": prompt_text_tokens,
                    "image_tokens": prompt_image_tokens,
                    "cached_tokens": 0,
                },
                "completion_tokens_details": {
                    "text_tokens": completion_tokens,
                    "image_tokens": 0,
                    "reasoning_tokens": 0,
                },
            },
        }
        return _with_log_metadata(response, _backend_account_email(backend), transport_audit=getattr(backend, "web_text_audit", None))

    response = completion_response(
        model,
        sanitize_output_text(raw_text, is_streaming=False),
        messages=messages,
    )
    return _with_log_metadata(response, _backend_account_email(backend), transport_audit=getattr(backend, "web_text_audit", None))


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    if body.get("stream"):
        if is_image_chat_request(body):
            return image_chat_events(body)
        model, messages, tools = text_chat_parts(body)
        if is_web_search_chat_request(body) and not has_unsupported_tools(body, WEB_SEARCH_TOOL_TYPES):
            return stream_web_search_chat_completion(messages, model)
        thinking_effort = thinking_effort_from_body(body)
        key = cache_key(body, messages, stream=True)
        return chat_completion_cache.get_or_compute_stream(
            key,
            lambda: stream_text_chat_completion(text_backend(model=model), messages, model, thinking_effort, tools=tools, call_id=text_transport_request_id(body)),
        )
    if is_image_chat_request(body):
        return image_chat_response(body)
    model, messages, tools = text_chat_parts(body)
    if is_web_search_chat_request(body) and not has_unsupported_tools(body, WEB_SEARCH_TOOL_TYPES):
        return web_search_chat_response(messages, model)
    thinking_effort = thinking_effort_from_body(body)
    key = cache_key(body, messages, stream=False)
    return chat_completion_cache.get_or_compute_response(
        key,
        lambda: text_completion_response(model, messages, thinking_effort, tools=tools, call_id=text_transport_request_id(body)),
    )
