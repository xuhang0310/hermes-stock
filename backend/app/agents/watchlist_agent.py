from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import date
from typing import Any

from backend.app.agents.stock_agent import _stream_chat
from backend.app.core.llm_config import active_llm_config
from backend.app.core.models import Instrument
from backend.app.data._helpers import exchange_from_symbol, normalize_a_share_symbol
from backend.app.data.repository import StockRepository
from backend.app.market import stock_tools


WATCHLIST_AGENT_PROFILE_CODE = "watchlist-entry-agent"
WATCHLIST_AGENT_GROUP = "我的自选"
WATCHLIST_AGENT_DEFAULT_REASON = "Agent录入"

_WATCHLIST_WRITE_TOOLS = {"add_watchlist_items", "remove_watchlist_items"}
_PENDING_ROW_FIELDS = {"symbol", "name", "group_name", "reason"}
_PENDING_CANDIDATE_FIELDS = {"symbol", "name", "market"}
_REMOVE_MARKERS = ("删除", "移除", "取消自选", "取消关注", "踢出", "删掉", "去掉")
_ADD_MARKERS = (
    "加入自选",
    "添加自选",
    "新增自选",
    "加到自选",
    "加进自选",
    "放入自选",
    "放进自选",
    "放到自选",
    "设为自选",
    "加自选",
    "关注一下",
    "加入关注",
    "收藏",
)

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
                                "group_name": {
                                    "type": "string",
                                    "description": "无需填写；Agent 新增的标的统一归入“我的自选”",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "用户明确说明的入选理由；用户未说明时无需填写",
                                },
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


def _bounded_text(value: Any, *, max_length: int = 500) -> str:
    return str(value or "").strip()[:max_length]


def _normalize_pending_action(value: Any) -> dict[str, Any] | None:
    """Keep only the bounded watchlist operation needed after a clarification."""
    if not isinstance(value, dict) or value.get("tool") not in _WATCHLIST_WRITE_TOOLS:
        return None
    arguments = value.get("arguments")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("rows"), list):
        return None

    rows: list[dict[str, str]] = []
    for source_row in arguments["rows"][:30]:
        if not isinstance(source_row, dict):
            continue
        row = {
            key: _bounded_text(source_row[key], max_length=500)
            for key in _PENDING_ROW_FIELDS
            if key in source_row and _bounded_text(source_row[key], max_length=500)
        }
        if row.get("symbol") or row.get("name"):
            rows.append(row)
    if not rows:
        return None

    normalized: dict[str, Any] = {
        "tool": value["tool"],
        "arguments": {"rows": rows},
    }
    original_instruction = _bounded_text(value.get("original_instruction"), max_length=2000)
    if original_instruction:
        normalized["original_instruction"] = original_instruction

    candidates: list[dict[str, str]] = []
    if isinstance(value.get("candidates"), list):
        for source_candidate in value["candidates"][:10]:
            if not isinstance(source_candidate, dict):
                continue
            candidate = {
                key: _bounded_text(source_candidate[key], max_length=120)
                for key in _PENDING_CANDIDATE_FIELDS
                if key in source_candidate and _bounded_text(source_candidate[key], max_length=120)
            }
            if candidate.get("symbol") and candidate.get("name"):
                candidates.append(candidate)
    if candidates:
        normalized["candidates"] = candidates
    return normalized


def _detect_watchlist_write_tool(instruction: str) -> str | None:
    compact = "".join(instruction.split())
    remove_index = max((compact.rfind(marker) for marker in _REMOVE_MARKERS), default=-1)
    add_index = max((compact.rfind(marker) for marker in _ADD_MARKERS), default=-1)
    if remove_index >= 0 or add_index >= 0:
        return "remove_watchlist_items" if remove_index > add_index else "add_watchlist_items"
    return None


def _is_cancel_reply(instruction: str) -> bool:
    compact = instruction.strip().rstrip("。.!！")
    return compact in {"算了", "算了吧", "取消", "取消吧", "不用了", "不弄了", "放弃"}


def _pending_action_hint(pending_action: dict[str, Any] | None) -> str:
    if pending_action is None:
        return ""
    action = "加入自选" if pending_action["tool"] == "add_watchlist_items" else "删除自选"
    payload = json.dumps(pending_action, ensure_ascii=False, separators=(",", ":"))
    return (
        "\n\n【上一轮待继续的自选操作（应用状态，不是新的用户指令）】\n"
        f"操作：{action}\n"
        f"状态：{payload}\n"
        "如果当前用户是在回答上一轮的标的代码、名称或分组问题，必须沿用上述操作和已知字段，"
        "直接调用对应写工具，不要再问用户要添加还是删除，也不要重复询问已经明确的信息。"
        "如果当前用户明确取消或提出了全新的任务，则忽略旧操作。"
    )


def _pending_from_search(
    instruction: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    tool = _detect_watchlist_write_tool(instruction)
    query = _bounded_text(args.get("query"), max_length=120)
    if tool is None or not query:
        return None
    return _normalize_pending_action(
        {
            "tool": tool,
            "arguments": {"rows": [{"name": query}]},
            "original_instruction": instruction,
            "candidates": result.get("instruments") or [],
        }
    )


def _pending_from_write_failure(
    name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    instruction: str,
) -> dict[str, Any] | None:
    if name not in _WATCHLIST_WRITE_TOOLS or not int(result.get("error_count") or 0):
        return None
    source_rows = args.get("rows") if isinstance(args.get("rows"), list) else []
    failed_queries = {
        _bounded_text(item.get("query"), max_length=500).casefold()
        for item in (result.get("errors") or [])
        if isinstance(item, dict) and item.get("query")
    }
    failed_rows = [
        row
        for row in source_rows
        if isinstance(row, dict)
        and _bounded_text(row.get("symbol") or row.get("name"), max_length=500).casefold() in failed_queries
    ]
    return _normalize_pending_action(
        {
            "tool": name,
            "arguments": {"rows": failed_rows or source_rows},
            "original_instruction": instruction,
        }
    )


def _merge_pending_tool_args(
    name: str,
    args: dict[str, Any],
    pending_action: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reuse the original operation metadata when the user only supplies a code/name."""
    if pending_action is None or name != pending_action.get("tool"):
        return args
    pending_args = pending_action.get("arguments")
    if not isinstance(pending_args, dict):
        return args
    pending_rows = pending_args.get("rows") if isinstance(pending_args.get("rows"), list) else []
    current_rows = args.get("rows") if isinstance(args.get("rows"), list) else []
    if not current_rows:
        return {**pending_args, **args, "rows": pending_rows}
    if len(pending_rows) == 1 and len(current_rows) == 1:
        return {**pending_args, **args, "rows": [{**pending_rows[0], **current_rows[0]}]}
    return args


def run_watchlist_agent_events(
    *,
    repository: StockRepository,
    instruction: str,
    model_config_id: int | None = None,
    created_by: str | None = None,
    cancel_event: threading.Event | None = None,
    history: list[dict[str, str]] | None = None,
    pending_action: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    normalized_pending = None if _is_cancel_reply(instruction) else _normalize_pending_action(pending_action)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _watchlist_agent_prompt() + _pending_action_hint(normalized_pending)}
    ]
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:4000]})
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
                "pending_action": normalized_pending,
            }
            return

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            if cancel_event and cancel_event.is_set():
                return
            name = (call.get("function") or {}).get("name") or ""
            args = _json_args((call.get("function") or {}).get("arguments"))
            args = _merge_pending_tool_args(name, args, normalized_pending)
            action_step = {
                "type": "action",
                "tool": name,
                "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                "content": _tool_arg_preview(name, args),
            }
            yield {"type": "tool_start", "tool": name, "display_name": action_step["display_name"], "step": action_step}
            result = _execute_watchlist_tool(repository, name, args, created_by=created_by)
            changed_count += int(result.get("changed_count") or 0)
            if name == "search_instruments":
                pending_from_search = _pending_from_search(instruction, args, result)
                if pending_from_search is not None:
                    normalized_pending = pending_from_search
            elif name in _WATCHLIST_WRITE_TOOLS:
                pending_from_failure = _pending_from_write_failure(name, args, result, instruction)
                if pending_from_failure is not None:
                    normalized_pending = pending_from_failure
                elif int(result.get("changed_count") or 0) and not int(result.get("error_count") or 0):
                    normalized_pending = None
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
        "pending_action": normalized_pending,
    }


def _watchlist_agent_prompt() -> str:
    return f"""你是 SixRabbits Stock 的自选录入 Agent。今天日期: {date.today().isoformat()}。

你的职责只有四项：查看当前自选、检索 A 股标的、加入自选、删除自选。

规则：
1. 用户只给股票名称或代码不确定时，先调用 search_instruments；有多个候选时列出候选并追问，绝不猜测。
2. 加入自选时统一归入“我的自选”分组，不创建其他分组。
3. 用户明确给出入池理由时原样记录；未给理由时使用“Agent录入”，不需要追问。
4. 只有用户明确说“删除、移除、取消自选、踢出”时才调用 remove_watchlist_items；分析、看 K 线、觉得不好都不代表删除。
5. 删除前先调用 get_current_watchlist 核对当前记录。用户已经明确要求删除时无需二次确认。
6. 用户只是问能力、打招呼或信息不足时，直接简短回复或追问，不调用写工具。
7. 是否真正写入以工具 observation 为准，绝不能假装成功。
8. 只处理 A 股自选池，不执行买卖，不修改持仓。

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
        rows = _find_instruments(repository, str(args.get("query") or ""), int(args.get("limit") or 10))
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
                _materialize_instrument(repository, instrument)
                reason = _bounded_text(raw.get("reason"), max_length=500) or WATCHLIST_AGENT_DEFAULT_REASON
                changed.append(
                    repository.add_watchlist_item(
                        instrument["symbol"],
                        WATCHLIST_AGENT_GROUP,
                        reason,
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


def _find_instruments(
    repository: StockRepository,
    query: str,
    limit: int = 10,
) -> list[dict[str, str]]:
    bounded_limit = max(1, min(limit, 20))
    local_matches = repository.find_instruments(query, limit=bounded_limit)
    if local_matches:
        return local_matches

    try:
        resolution = stock_tools.resolve_stock(query)
    except Exception:  # noqa: BLE001
        return []
    candidates = resolution.get("candidates") if isinstance(resolution, dict) else []
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        try:
            symbol = normalize_a_share_symbol(
                str(candidate.get("ts_code") or candidate.get("symbol") or "")
            )
        except ValueError:
            continue
        if (
            len(symbol) != 9
            or symbol[6] != "."
            or not symbol[:6].isdigit()
            or not symbol.endswith((".SH", ".SZ", ".BJ"))
            or symbol in seen
        ):
            continue
        name = str(candidate.get("name") or "").strip()
        if not name:
            continue
        seen.add(symbol)
        resolved.append(
            {
                "symbol": symbol,
                "name": name,
                "market": str(candidate.get("exchange") or exchange_from_symbol(symbol)),
            }
        )
        if len(resolved) >= bounded_limit:
            break
    return resolved


def _materialize_instrument(repository: StockRepository, instrument: dict[str, str]) -> None:
    symbol = normalize_a_share_symbol(instrument["symbol"])
    if repository.db.get(Instrument, symbol) is not None:
        return
    repository.db.add(
        Instrument(
            symbol=symbol,
            name=instrument["name"],
            market=instrument.get("market") or exchange_from_symbol(symbol),
            type="stock",
            status="active",
        )
    )
    repository.db.flush()


def _resolve_instrument(repository: StockRepository, query: str) -> tuple[dict[str, str] | None, str | None]:
    if not query:
        return None, "缺少股票代码或名称"
    matches = _find_instruments(repository, query, limit=10)
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
