"""LLM 调用公共客户端 —— 统一 OpenAI 兼容 ``/chat/completions`` 的 url 拼接、
Bearer 认证、超时、状态码检查，消灭分散在 stock_agent / position_agent /
position_import / prompt_recommendations / memory 五处的重复样板。

只收敛"怎么发请求 + 怎么处理错误 + 怎么解析 SSE"，不动调用方的 payload
构造与结果解析：``temperature`` / ``tools`` / ``tool_choice`` / ``stream``
均由参数传入，保持各路径原行为不变（重构即净收益，无行为漂移）。
"""
from __future__ import annotations

import json
from collections.abc import Generator, Iterable
from typing import Any

import httpx

from backend.app.core.applog import log_event
import time
from backend.app.ai.ledger_store import append_llm_call as _append_llm_call


def _append_llm_call_safely(
    llm_config: dict,
    messages: list[dict],
    response: dict | None,
    *,
    error: str | None = None,
    usage: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    """LLM 调用留档（L3，方案 7.2 全量留档）。fire-and-forget，绝不影响主流程。"""
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


def _build_payload(
    llm_config: dict[str, Any],
    messages: list[dict],
    *,
    tools: list[dict] | None,
    tool_choice: str | None,
    temperature: float | None,
    stream: bool,
    enable_thinking: bool | None = None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """组装 url / payload / headers。temperature=None 时沿用 llm_config 里的值。"""
    if not llm_config.get("api_key") or not llm_config.get("base_url"):
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在模型平台/模型配置中补齐。")
    url = llm_config["base_url"].rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "messages": messages,
        "temperature": llm_config["temperature"] if temperature is None else temperature,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if enable_thinking is not None:
        payload["enable_thinking"] = enable_thinking
    headers = {"Authorization": f"Bearer {llm_config['api_key']}"}
    return url, payload, headers


def chat_completion(
    messages: list[dict],
    llm_config: dict,
    tools: list[dict] | None = None,
    *,
    tool_choice: str | None = "auto",
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    timeout: float = 120.0,
    log_module: str | None = None,
) -> dict:
    """同步非流式 chat completion，返回 ``response.json()``。

    HTTP 状态 >= 400 时抛 ``RuntimeError``（含响应前 600 字符），并在落库日志里
    记一条 ERROR（``log_module`` 为 None 则不落库，避免无 ctx 的调用点出错）。
    """
    url, payload, headers = _build_payload(
        llm_config, messages,
        tools=tools,
        tool_choice=tool_choice,
        temperature=temperature,
        stream=False,
        enable_thinking=enable_thinking,
    )
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        response = client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        body = response.text[:600]
        if log_module:
            log_event(log_module, "ERROR", "LLM 接口错误", status=response.status_code)
        _append_llm_call_safely(llm_config, messages, None, error=body)
        raise RuntimeError(f"LLM 接口返回 {response.status_code}: {body}")
    data = response.json()
    _append_llm_call_safely(llm_config, messages, data, usage=data.get("usage"))
    return data


def parse_sse_stream(lines: Iterable[str]) -> Generator[tuple[str, Any], None, None]:
    """解析 OpenAI 兼容 SSE 流，yield ``(kind, payload)``。

    kind 取值：
    - ``"reasoning"`` —— 推理内容片段（DeepSeek/某些模型带 ``reasoning_content``）
    - ``"content"`` —— 正文片段
    - ``"tool_args"`` —— 工具调用参数片段 ``{"index", "name", "delta"}``（模型生成长参数
      如写文件内容时，正文静默；旁路吐片段供调用方做进度可视化，不影响 message 组装）
    - ``"message"`` —— 流结束时一次性 yield 完整 message dict（含累积 content + tool_calls）

    注意：新增 kind 时消费方必须显式匹配（stock_agent 的 else 分支会把未知 kind 当
    message 覆盖；position_agent 是显式 elif，天然忽略未知 kind）。
    """
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
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
            slot = tool_calls.setdefault(index, {"id": "", "function": {"name": "", "arguments": ""}})
            if item.get("id"):
                slot["id"] = item["id"]
            function = item.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] = function["name"]
            if function.get("arguments"):
                slot["function"]["arguments"] += function["arguments"]
            if function.get("name") or function.get("arguments"):
                yield "tool_args", {
                    "index": index,
                    "name": slot["function"]["name"],
                    "delta": function.get("arguments") or "",
                }

    message: dict = {"content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [
            {"id": slot["id"] or f"call_{index}", "type": "function", "function": slot["function"]}
            for index, slot in sorted(tool_calls.items())
        ]
    yield "message", message


def stream_chat(
    messages: list[dict],
    llm_config: dict,
    tools: list[dict] | None = None,
    *,
    temperature: float | None = None,
    timeout: float = 180.0,
) -> Generator[tuple[str, Any], None, None]:
    """流式 chat completion，yield ``(kind, payload)``（见 :func:`parse_sse_stream`）。"""
    url, payload, headers = _build_payload(
        llm_config, messages,
        tools=tools, tool_choice=None, temperature=temperature, stream=True,
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
    # 流结束留档（调用方迭代完才到此；中途 break 不记，可接受）
    _append_llm_call_safely(
        llm_config, messages, final_message,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
