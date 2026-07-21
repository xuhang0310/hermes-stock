from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import date
from typing import Any

from backend.app.agents.stock_agent import _stream_chat
from backend.app.core.llm_config import active_llm_config
from backend.app.data.repository import StockRepository


WATCHLIST_AGENT_PROFILE_CODE = "watchlist-entry-agent"

WATCHLIST_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_watchlist",
            "description": "读取当前活跃自选池。查看、去重或删除前使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_instruments",
            "description": "按股票代码或名称检索 A 股标的。用户只给名称、代码不完整或标的不确定时先使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "股票代码或名称"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_watchlist_items",
            "description": "将一个或多个已确认的 A 股标的加入自选池。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string", "description": "A股代码，如 600519.SH 或 600519"},
                                "name": {"type": "string", "description": "股票名称；没有代码时用于精确检索"},
                                "group_name": {"type": "string", "description": "分组，默认 default"},
                                "reason": {"type": "string", "description": "加入自选的理由，可空"},
                            },
                        },
                    }
                },
                "required": ["rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_watchlist_items",
            "description": "从自选池删除一个或多个标的。只有用户明确表达删除、移除或取消自选时才能调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string", "description": "A股代码"},
                                "name": {"type": "string", "description": "股票名称；没有代码时用于精确检索"},
                                "group_name": {"type": "string", "description": "同一标的在多个分组时必须指定"},
                            },
                        },
                    }
                },
                "required": ["rows"],
            },
        },
    },
]

TOOL_DISPLAY_NAMES = {
    "get_current_watchlist": "读取当前自选",
    "search_instruments": "检索 A 股标的",
    "add_watchlist_items": "加入自选池",
    "remove_watchlist_items": "删除自选",
}


def run_watchlist_agent_events(
    *,
    repository: StockRepository,
    instruction: str,
    model_config_id: int | None = None,
    created_by: str | None = None,
    cancel_event: threading.Event | None = None,
    history: list[dict[str, str]] | None = None,
) -> Generator[dict[str, Any], None, None]:
    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    messages: list[dict[str, Any]] = [{"role": "system", "content": _watchlist_agent_prompt()}]
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": instruction.strip()})

    model_payload = {
        "id": llm_config.get("id"),
        "display_name": llm_config.get("display_name"),
        "model_name": llm_config.get("model"),
        "provider_name": llm_config.get("provider_name"),
    }
    changed_count = 0
    active_rows: list[dict[str, Any]] = []

    yield {
        "type": "model_selected",
        "model": model_payload,
        "step": {
            "type": "model",
            "content": f"使用 {model_payload.get('display_name') or model_payload.get('model_name')} 进行自选录入分析。",
        },
    }
    yield {"type": "thinking", "message": "正在判断是查询、加入还是删除自选..."}

    for turn in range(4):
        if cancel_event and cancel_event.is_set():
            return
        message: dict[str, Any] | None = None
        streamed_content: list[str] = []
        for kind, payload in _stream_chat(messages, llm_config, WATCHLIST_AGENT_TOOLS):
            if cancel_event and cancel_event.is_set():
                return
            if kind == "reasoning":
                yield {"type": "thinking", "message": "模型正在核对标的和工具选择..."}
            elif kind == "content":
                streamed_content.append(str(payload))
                yield {"type": "answer_delta", "content": str(payload)}
            elif kind == "message":
                message = payload

        if message is None:
            raise RuntimeError("自选录入 Agent 模型没有返回有效消息。")

        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or "".join(streamed_content)
        if not tool_calls:
            reply = content.strip() or _fallback_reply(changed_count)
            yield {
                "type": "done",
                "reply": reply,
                "summary": reply,
                "rows": active_rows,
                "should_save": bool(changed_count),
                "intent": "update_watchlist" if changed_count else "chat",
            }
            return

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            if cancel_event and cancel_event.is_set():
                return
            name = (call.get("function") or {}).get("name") or ""
            args = _json_args((call.get("function") or {}).get("arguments"))
            action_step = {
                "type": "action",
                "tool": name,
                "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                "content": _tool_arg_preview(name, args),
            }
            yield {"type": "tool_start", "tool": name, "display_name": action_step["display_name"], "step": action_step}
            result = _execute_watchlist_tool(repository, name, args, created_by=created_by)
            changed_count += int(result.get("changed_count") or 0)
            if name in {"get_current_watchlist", "add_watchlist_items", "remove_watchlist_items"}:
                active_rows = result.get("watchlist") or active_rows
            observation_step = {
                "type": "observation" if "error" not in result else "error",
                "tool": name,
                "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                "content": _tool_result_preview(name, result),
                "success": "error" not in result,
            }
            yield {
                "type": "tool_done",
                "tool": name,
                "display_name": observation_step["display_name"],
                "success": "error" not in result,
                "rows": active_rows if name in {"add_watchlist_items", "remove_watchlist_items"} else None,
                "step": observation_step,
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{name}_{turn}",
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
        yield {"type": "thinking", "message": "自选池结果已回填，正在整理回复..."}

    reply = _fallback_reply(changed_count)
    yield {
        "type": "done",
        "reply": reply,
        "summary": reply,
        "rows": active_rows,
        "should_save": bool(changed_count),
        "intent": "update_watchlist" if changed_count else "chat",
    }


def _watchlist_agent_prompt() -> str:
    return f"""你是 SixRabbits Stock 的自选录入 Agent。今天日期: {date.today().isoformat()}。

你的职责只有四项：查看当前自选、检索 A 股标的、加入自选、删除自选。

规则：
1. 用户只给股票名称或代码不确定时，先调用 search_instruments；有多个候选时列出候选并追问，绝不猜测。
2. 加入自选时尽量记录用户给出的入池理由；未给理由可以留空，不需要追问。
3. 只有用户明确说“删除、移除、取消自选、踢出”时才调用 remove_watchlist_items；分析、看 K 线、觉得不好都不代表删除。
4. 删除前先调用 get_current_watchlist 核对当前记录。用户已经明确要求删除时无需二次确认。
5. 用户只是问能力、打招呼或信息不足时，直接简短回复或追问，不调用写工具。
6. 是否真正写入以工具 observation 为准，绝不能假装成功。
7. 只处理 A 股自选池，不执行买卖，不修改持仓。

A 股代码规范：6 开头通常为 .SH，0/3 开头通常为 .SZ，4/8 开头通常为 .BJ。
回复使用简短中文 Markdown，写入后明确列出标的代码、名称和结果。"""


def _execute_watchlist_tool(
    repository: StockRepository,
    name: str,
    args: dict[str, Any],
    *,
    created_by: str | None,
) -> dict[str, Any]:
    actor = (created_by or "watchlist_agent")[:80]
    if name == "get_current_watchlist":
        rows = repository.watchlist()
        return {"watchlist": rows, "count": len(rows)}
    if name == "search_instruments":
        rows = repository.find_instruments(str(args.get("query") or ""), int(args.get("limit") or 10))
        return {"instruments": rows, "count": len(rows)}
    if name not in {"add_watchlist_items", "remove_watchlist_items"}:
        return {"error": f"未知自选工具: {name}"}

    input_rows = args.get("rows") if isinstance(args.get("rows"), list) else []
    if not input_rows:
        return {"error": "没有可处理的自选标的 rows"}

    changed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for raw in input_rows[:30]:
        if not isinstance(raw, dict):
            continue
        query = str(raw.get("symbol") or raw.get("name") or "").strip()
        instrument, resolve_error = _resolve_instrument(repository, query)
        if resolve_error:
            errors.append({"query": query or "-", "error": resolve_error})
            continue
        try:
            if name == "add_watchlist_items":
                changed.append(
                    repository.add_watchlist_item(
                        instrument["symbol"],
                        str(raw.get("group_name") or "default"),
                        str(raw.get("reason")).strip() if raw.get("reason") is not None else None,
                        added_by=actor,
                        commit=False,
                    )
                )
            else:
                changed.append(
                    repository.remove_watchlist_item(
                        symbol=instrument["symbol"],
                        group_name=str(raw.get("group_name") or "").strip() or None,
                        removed_by=actor,
                        commit=False,
                    )
                )
        except ValueError as exc:
            errors.append({"query": query, "error": str(exc)})
    repository.db.commit()
    return {
        "changed_count": len(changed),
        "changed": changed,
        "error_count": len(errors),
        "errors": errors,
        "watchlist": repository.watchlist(),
    }


def _resolve_instrument(repository: StockRepository, query: str) -> tuple[dict[str, str] | None, str | None]:
    if not query:
        return None, "缺少股票代码或名称"
    matches = repository.find_instruments(query, limit=10)
    if not matches:
        return None, f"未找到 A 股标的：{query}"
    exact_names = [row for row in matches if row["name"].casefold() == query.casefold()]
    if len(exact_names) == 1:
        return exact_names[0], None
    if len(matches) == 1:
        return matches[0], None
    preview = "、".join(f"{row['name']}({row['symbol']})" for row in matches[:5])
    return None, f"标的不唯一，请明确代码：{preview}"


def _json_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tool_arg_preview(name: str, args: dict[str, Any]) -> str:
    if name == "search_instruments":
        return f"检索：{args.get('query') or '-'}"
    if name in {"add_watchlist_items", "remove_watchlist_items"}:
        rows = args.get("rows") if isinstance(args.get("rows"), list) else []
        symbols = [str(row.get("symbol") or row.get("name") or "-") for row in rows[:5] if isinstance(row, dict)]
        return f"处理 {len(rows)} 个标的：{', '.join(symbols) or '-'}"
    return "读取当前活跃自选池"


def _tool_result_preview(name: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "get_current_watchlist":
        return f"当前活跃自选 {result.get('count', 0)} 个。"
    if name == "search_instruments":
        return f"找到 {result.get('count', 0)} 个候选标的。"
    action = "加入" if name == "add_watchlist_items" else "删除"
    return f"{action}成功 {result.get('changed_count', 0)} 个，失败 {result.get('error_count', 0)} 个。"


def _fallback_reply(changed_count: int) -> str:
    if changed_count:
        return f"自选池已更新，共处理 {changed_count} 个标的。"
    return "我是自选录入 Agent。你可以让我加入、删除或查看自选，例如“把招商银行加入自选，理由是跟踪银行板块”。"
