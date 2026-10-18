"""统一的 Responses API 客户端。

历史调用方仍然使用 ``chat_completion`` / ``stream_chat`` 这两个函数名，
但底层请求已经统一发送到 OpenAI Responses API。返回值会适配成旧调用方
使用的 ``choices[0].message`` 形状，因此持仓、选股、记忆和推荐等模块不必
各自重复实现协议转换。
"""
from __future__ import annotations

import json
import time
from collections.abc import Generator, Iterable
from typing import Any

import httpx

from backend.app.ai.ledger_store import append_llm_call as _append_llm_call
from backend.app.core.applog import log_event


def _append_llm_call_safely(
    llm_config: dict,
    messages: list[dict],
    response: dict | None,
    *,
    error: str | None = None,
    usage: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    """LLM 调用留档（L3），失败绝不影响主流程。"""
    try:
        _append_llm_call(
            model=llm_config.get("model", ""),
            messages=messages,
            response=response,
            usage=usage,
            error=error,
            duration_ms=duration_ms,
        )
    except Exception:
        pass


def _responses_url(base_url: str) -> str:
    """将平台地址规范化为 Responses endpoint。"""
    value = base_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/chat/completion", "/responses"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/") + "/responses"


def _input_content(content: Any) -> Any:
    """把 Chat Completions 内容块转换成 Responses input 内容块。"""
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return str(content)

    converted: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            converted.append({"type": "input_text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
        elif kind in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, dict):
                image = image.get("url")
            converted.append({"type": "input_image", "image_url": image})
        elif kind == "file_url":
            file_url = item.get("file_url") or item.get("url")
            converted.append({"type": "input_file", "file_url": file_url})
        else:
            converted.append(item)
    return converted


def _messages_to_input(messages: list[dict]) -> list[dict[str, Any]]:
    """转换历史消息和工具结果为 Responses input item。"""
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")

        if role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or message.get("call_id") or "",
                    "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                }
            )
            continue

        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            if content:
                result.append({"role": "assistant", "content": _input_content(content)})
            for call in tool_calls:
                function = call.get("function") or {}
                arguments = function.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or call.get("call_id") or "",
                        "name": function.get("name") or call.get("name") or "",
                        "arguments": arguments,
                    }
                )
            continue

        result.append({"role": role, "content": _input_content(content)})
    return result


def _tools_to_responses(tools: list[dict] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = dict(tool["function"])
            item: dict[str, Any] = {"type": "function"}
            for key in ("name", "description", "parameters", "strict"):
                if key in function and function[key] is not None:
                    item[key] = function[key]
            converted.append(item)
        else:
            converted.append(tool)
    return converted or None


def _response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def _response_tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        arguments = item.get("arguments", item.get("input", "{}"))
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            {
                "id": item.get("call_id") or item.get("id") or f"call_{len(calls)}",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": arguments,
                },
            }
        )
    return calls


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    """Expose Responses output through the legacy choices/message shape."""
    message: dict[str, Any] = {"role": "assistant", "content": _response_text(data)}
    tool_calls = _response_tool_calls(data)
    if tool_calls:
        message["tool_calls"] = tool_calls
    normalized = dict(data)
    normalized["choices"] = [{"index": 0, "message": message, "finish_reason": "stop"}]
    return normalized


def _build_payload(
    llm_config: dict[str, Any],
    messages: list[dict],
    *,
    tools: list[dict] | None,
    tool_choice: str | None,
    temperature: float | None,
    stream: bool,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    if not llm_config.get("api_key") or not llm_config.get("base_url"):
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在模型平台/模型配置中补齐。")

    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "input": _messages_to_input(messages),
        "stream": stream,
    }
    configured_temperature = llm_config.get("temperature") if temperature is None else temperature
    if configured_temperature is not None:
        payload["temperature"] = configured_temperature
    responses_tools = _tools_to_responses(tools)
    if responses_tools:
        payload["tools"] = responses_tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    elif enable_thinking is False:
        payload["reasoning"] = {"effort": "none"}
    headers = {"Authorization": f"Bearer {llm_config['api_key']}", "Content-Type": "application/json"}
    return _responses_url(str(llm_config["base_url"])), payload, headers


def chat_completion(
    messages: list[dict],
    llm_config: dict,
    tools: list[dict] | None = None,
    *,
    tool_choice: str | None = "auto",
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
    timeout: float = 120.0,
    log_module: str | None = None,
) -> dict:
    """同步调用 Responses API，并返回兼容旧调用方的 choices/message。"""
    url, payload, headers = _build_payload(
        llm_config,
        messages,
        tools=tools,
        tool_choice=tool_choice,
        temperature=temperature,
        stream=False,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
    )
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        response = client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        body = response.text[:600]
        if log_module:
            log_event(log_module, "ERROR", "LLM 接口错误", status=response.status_code)
        _append_llm_call_safely(llm_config, messages, None, error=body)
        raise RuntimeError(f"LLM 接口返回 {response.status_code}: {body}")
    data = _normalize_response(response.json())
    _append_llm_call_safely(
        llm_config,
        messages,
        data,
        usage=data.get("usage"),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def _append_response_stream_item(
    item: dict[str, Any],
    content_parts: list[str],
    tool_calls: dict[str, dict[str, Any]],
) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    kind = item.get("type")
    if kind in {"message", "output_text", "text"}:
        text = _response_text({"output": [item]})
        if text and not content_parts:
            content_parts.append(text)
            events.append(("content", text))
    elif kind in {"function_call", "custom_tool_call"}:
        key = str(item.get("id") or item.get("call_id") or len(tool_calls))
        slot = tool_calls.setdefault(
            key,
            {
                "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                "name": item.get("name") or "",
                "arguments": "",
            },
        )
        arguments = item.get("arguments", item.get("input", ""))
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if arguments and not slot["arguments"]:
            slot["arguments"] = arguments
            events.append(("tool_args", {"index": list(tool_calls).index(key), "name": slot["name"], "delta": arguments}))
    return events


def parse_sse_stream(lines: Iterable[str]) -> Generator[tuple[str, Any], None, None]:
    """解析 Responses SSE，同时兼容旧的 Chat Completions SSE 测试数据。"""
    content_parts: list[str] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    event_name = ""
    completed_response: dict[str, Any] | None = None

    for line in lines:
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue

        kind = str(chunk.get("type") or event_name or "")
        if kind == "response.output_text.delta":
            text = str(chunk.get("delta") or "")
            if text:
                content_parts.append(text)
                yield "content", text
            continue
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            text = str(chunk.get("delta") or "")
            if text:
                yield "reasoning", text
            continue
        if kind == "response.output_item.added":
            item = chunk.get("item") or {}
            if item.get("type") in {"function_call", "custom_tool_call"}:
                key = str(item.get("id") or item.get("call_id") or len(tool_calls))
                tool_calls[key] = {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                }
            continue
        if kind == "response.function_call_arguments.delta":
            key = str(chunk.get("item_id") or chunk.get("call_id") or len(tool_calls))
            slot = tool_calls.setdefault(key, {"id": chunk.get("call_id") or key, "name": chunk.get("name") or "", "arguments": ""})
            delta = str(chunk.get("delta") or "")
            slot["arguments"] += delta
            yield "tool_args", {
                "index": list(tool_calls).index(key),
                "name": slot.get("name") or chunk.get("name") or "",
                "delta": delta,
            }
            continue
        if kind == "response.output_item.done":
            for event_kind, payload in _append_response_stream_item(chunk.get("item") or {}, content_parts, tool_calls):
                yield event_kind, payload
            continue
        if kind == "response.completed":
            completed_response = chunk.get("response") or {}
            continue
        if kind in {"response.failed", "response.error"}:
            error = chunk.get("error") or chunk
            raise RuntimeError(f"Responses API 流式调用失败: {error}")

        # 兼容本地旧测试和第三方适配器的 Chat Completions SSE 数据。
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield "reasoning", reasoning
            text_piece = delta.get("content")
            if text_piece:
                content_parts.append(text_piece)
                yield "content", text_piece
            for item in delta.get("tool_calls") or []:
                index = int(item.get("index", 0))
                key = str(index)
                slot = tool_calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                if item.get("id"):
                    slot["id"] = item["id"]
                function = item.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                argument_delta = function.get("arguments") or ""
                slot["arguments"] += argument_delta
                if function.get("name") or argument_delta:
                    yield "tool_args", {"index": index, "name": slot["name"], "delta": argument_delta}

    if completed_response:
        if not content_parts:
            text = _response_text(completed_response)
            if text:
                content_parts.append(text)
        for call in _response_tool_calls(completed_response):
            function = call["function"]
            key = next(
                (candidate for candidate, slot in tool_calls.items() if slot.get("id") == call["id"]),
                str(call["id"]),
            )
            slot = tool_calls.setdefault(key, {"id": call["id"], "name": function["name"], "arguments": ""})
            if not slot["arguments"]:
                slot["arguments"] = function["arguments"]
            if not slot["name"]:
                slot["name"] = function["name"]

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": slot.get("name") or "", "arguments": slot.get("arguments") or ""},
            }
            for index, slot in enumerate(tool_calls.values())
        ]
    yield "message", message


def stream_chat(
    messages: list[dict],
    llm_config: dict,
    tools: list[dict] | None = None,
    *,
    temperature: float | None = None,
    timeout: float = 180.0,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> Generator[tuple[str, Any], None, None]:
    """Responses 流式调用，yield 与历史调用方相同的事件元组。"""
    url, payload, headers = _build_payload(
        llm_config,
        messages,
        tools=tools,
        tool_choice="auto",
        temperature=temperature,
        stream=True,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
    )
    started = time.monotonic()
    final_message: dict | None = None
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                body = response.read().decode("utf-8", "replace")[:300]
                _append_llm_call_safely(llm_config, messages, None, error=body)
                raise RuntimeError(f"LLM 接口返回 {response.status_code}: {body}")
            for kind, item in parse_sse_stream(response.iter_lines()):
                if kind == "message":
                    final_message = item
                yield kind, item
    _append_llm_call_safely(
        llm_config,
        messages,
        final_message,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
