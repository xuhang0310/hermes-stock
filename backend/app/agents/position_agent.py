from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date
from typing import Any

import httpx

from backend.app.agents.position_import import decode_image_payload
from backend.app.data.repository import StockRepository
from backend.app.agents.stock_agent import _active_llm_config, _stream_chat


POSITION_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_position_schema",
            "description": "查看 SixRabbits Stock 持仓录入字段说明和 A 股代码规范。用户问需要哪些字段、如何录入时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_positions",
            "description": "读取当前系统已经保存的持仓列表。用户询问已有持仓、是否重复、当前状态时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_position_holdings",
            "description": "把已确认的 A 股持仓明细保存到系统。只有识别到真实持仓记录且字段足够时才能调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "account": {
                        "type": "object",
                        "description": "账户信息，可从截图或用户描述提取；未知字段可省略。",
                        "properties": {
                            "broker_name": {"type": "string", "description": "券商或交易软件名称，未知填 ai_agent"},
                            "account_name": {"type": "string", "description": "账户名称，可空"},
                            "account_no_masked": {"type": "string", "description": "脱敏账号，可空"},
                            "account_type": {"type": "string", "description": "cash / credit / simulation"},
                        },
                    },
                    "as_of_date": {"type": "string", "description": "持仓日期，格式 YYYY-MM-DD，可空"},
                    "rows": {
                        "type": "array",
                        "description": "持仓明细行。数值必须是纯数字，不要带元、股、逗号或百分号。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string", "description": "A股代码，如 600519.SH、000001.SZ，也可传 6 位代码"},
                                "name": {"type": "string", "description": "证券名称"},
                                "position_quantity": {"type": "number", "description": "持仓数量/股份余额"},
                                "available_quantity": {"type": "number", "description": "可用数量/可卖数量"},
                                "frozen_quantity": {"type": "number", "description": "冻结数量"},
                                "today_buy_quantity": {"type": "number", "description": "今日买入数量"},
                                "today_sell_quantity": {"type": "number", "description": "今日卖出数量"},
                                "cost_price": {"type": "number", "description": "成本价"},
                                "cost_amount": {"type": "number", "description": "成本金额"},
                                "latest_price": {"type": "number", "description": "现价/最新价"},
                                "market_value": {"type": "number", "description": "持仓市值"},
                                "floating_pnl": {"type": "number", "description": "浮动盈亏"},
                                "floating_pnl_pct": {"type": "number", "description": "浮动盈亏比例，单位为百分数"},
                                "day_pnl": {"type": "number", "description": "当日盈亏"},
                                "position_pct": {"type": "number", "description": "仓位占比，单位为百分数"},
                                "stop_price": {"type": "number", "description": "止损价"},
                                "take_profit_price": {"type": "number", "description": "止盈价"},
                                "thesis": {"type": "string", "description": "持仓逻辑"},
                                "risk_note": {"type": "string", "description": "风险说明"},
                                "confidence": {"type": "number", "description": "识别置信度 0-1"},
                            },
                            "required": ["symbol", "position_quantity"],
                        },
                    },
                },
                "required": ["rows"],
            },
        },
    },
]


TOOL_DISPLAY_NAMES = {
    "get_position_schema": "查看持仓字段",
    "get_current_positions": "读取当前持仓",
    "save_position_holdings": "保存持仓明细",
}


def run_position_agent(
    *,
    repository: StockRepository,
    instruction: str,
    image_base64: str | None = None,
    mime_type: str = "image/png",
    model_config_id: int | None = None,
    created_by: str | None = None,
    account_id: int | None = None,
) -> dict[str, Any]:
    llm_config = _active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)

    system_prompt = _position_agent_prompt()
    user_content: str | list[dict[str, Any]]
    text = instruction.strip() or "请识别并录入这张持仓截图。"
    if clean_base64:
        user_content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{clean_base64}"}},
        ]
    else:
        user_content = text

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    steps: list[dict[str, Any]] = [
        {
            "type": "model",
            "content": f"使用 {llm_config.get('display_name') or llm_config.get('model')} 判断是否需要调用持仓工具。",
        }
    ]
    saved_rows: list[dict[str, Any]] = []
    saved_count = 0
    reply = ""

    for _ in range(4):
        message = _chat_completion(messages, llm_config, POSITION_AGENT_TOOLS)
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        if not tool_calls:
            reply = content.strip() or _fallback_reply(saved_count)
            break

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _json_args((call.get("function") or {}).get("arguments"))
            steps.append(
                {
                    "type": "action",
                    "tool": name,
                    "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                    "content": _tool_arg_preview(name, args),
                }
            )
            result = _execute_position_tool(
                repository=repository,
                name=name,
                args=args,
                created_by=created_by,
                source_name=llm_config.get("display_name") or llm_config.get("model") or "position_agent",
                image_mime_type=mime_type if image_base64 else "text/plain",
                image_sha256=image_hash,
                account_id=account_id,
            )
            if name == "save_position_holdings":
                saved_rows = result.get("rows") or []
                saved_count = int(result.get("saved_count") or 0)
            steps.append(
                {
                    "type": "observation" if "error" not in result else "error",
                    "tool": name,
                    "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                    "content": _tool_result_preview(name, result),
                    "success": "error" not in result,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{name}",
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
    else:
        reply = _fallback_reply(saved_count)

    return {
        "model": {
            "id": llm_config.get("id"),
            "display_name": llm_config.get("display_name"),
            "model_name": llm_config.get("model"),
            "provider_name": llm_config.get("provider_name"),
        },
        "intent": "add_position" if saved_count else "chat",
        "should_save": bool(saved_count),
        "reply": reply,
        "rows": saved_rows,
        "steps": steps,
        "summary": reply,
    }


def run_position_agent_events(
    *,
    repository: StockRepository,
    instruction: str,
    image_base64: str | None = None,
    mime_type: str = "image/png",
    model_config_id: int | None = None,
    created_by: str | None = None,
    account_id: int | None = None,
) -> Generator[dict[str, Any], None, None]:
    llm_config = _active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)

    text = instruction.strip() or "请识别并录入这张持仓截图。"
    user_content: str | list[dict[str, Any]]
    if clean_base64:
        user_content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{clean_base64}"}},
        ]
    else:
        user_content = text

    model_payload = {
        "id": llm_config.get("id"),
        "display_name": llm_config.get("display_name"),
        "model_name": llm_config.get("model"),
        "provider_name": llm_config.get("provider_name"),
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _position_agent_prompt()},
        {"role": "user", "content": user_content},
    ]
    saved_rows: list[dict[str, Any]] = []
    saved_count = 0
    final_reply = ""

    yield {
        "type": "model_selected",
        "model": model_payload,
        "step": {
            "type": "model",
            "content": f"使用 {model_payload.get('display_name') or model_payload.get('model_name')} 进行持仓录入 ReAct 分析。",
        },
    }
    yield {"type": "thinking", "message": "正在判断用户意图和是否需要调用持仓工具..."}

    for turn in range(4):
        message: dict[str, Any] | None = None
        streamed_content = []
        for kind, payload in _stream_chat(messages, llm_config, POSITION_AGENT_TOOLS):
            if kind == "reasoning":
                yield {"type": "thinking", "message": "模型正在分析输入和工具选择..."}
            elif kind == "content":
                streamed_content.append(str(payload))
                yield {"type": "answer_delta", "content": str(payload)}
            elif kind == "message":
                message = payload

        if message is None:
            raise RuntimeError("持仓 Agent 模型没有返回有效消息。")

        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or "".join(streamed_content)
        if not tool_calls:
            final_reply = content.strip() or _fallback_reply(saved_count)
            yield {
                "type": "done",
                "reply": final_reply,
                "summary": final_reply,
                "rows": saved_rows,
                "should_save": bool(saved_count),
                "intent": "add_position" if saved_count else "chat",
            }
            return

        if content:
            yield {"type": "thinking", "message": "模型已完成当前判断，准备执行工具。"}
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        for call in tool_calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _json_args((call.get("function") or {}).get("arguments"))
            action_step = {
                "type": "action",
                "tool": name,
                "display_name": TOOL_DISPLAY_NAMES.get(name, name),
                "content": _tool_arg_preview(name, args),
            }
            yield {"type": "tool_start", "tool": name, "display_name": action_step["display_name"], "step": action_step}
            result = _execute_position_tool(
                repository=repository,
                name=name,
                args=args,
                created_by=created_by,
                source_name=llm_config.get("display_name") or llm_config.get("model") or "position_agent",
                image_mime_type=mime_type if image_base64 else "text/plain",
                image_sha256=image_hash,
                account_id=account_id,
            )
            if name == "save_position_holdings":
                saved_rows = result.get("rows") or []
                saved_count = int(result.get("saved_count") or 0)
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
                "rows": saved_rows if name == "save_position_holdings" else None,
                "step": observation_step,
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{name}_{turn}",
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

        yield {"type": "thinking", "message": "工具结果已回填模型，正在生成最终回复..."}

    final_reply = _fallback_reply(saved_count)
    yield {
        "type": "done",
        "reply": final_reply,
        "summary": final_reply,
        "rows": saved_rows,
        "should_save": bool(saved_count),
        "intent": "add_position" if saved_count else "chat",
    }


def _position_agent_prompt() -> str:
    return f"""你是 SixRabbits Stock 的持仓录入 Agent，使用 ReAct / tool-calling 工作。今天日期: {date.today().isoformat()}

你的职责:
1. 和用户对话，理解自然语言或截图里的 A 股持仓。
2. 判断是否需要调用工具。问候、闲聊、能力咨询，不调用保存工具。
3. 信息不足时先追问，不要保存半截数据。
4. 识别到真实持仓明细后，调用 save_position_holdings 工具保存。
5. 保存后基于工具 observation 回复用户，明确保存了哪些标的、哪些字段缺失。

工具规则:
- get_position_schema: 用户问字段、格式、需要提供什么信息时调用。
- get_current_positions: 用户问已有持仓、是否重复、当前保存了什么时调用。
- save_position_holdings: 只有确认有真实持仓明细时调用。不要用它保存问候、测试文本或不完整意图。

A 股代码规范:
- 6 开头通常为 .SH，0/3 开头通常为 .SZ，4/8 开头通常为 .BJ。
- 数值字段传纯数字，例如 8% 传 8，1,000 股传 1000。

回复要求:
- 中文 Markdown。
- 简短、清楚、有下一步。
- 不能假装保存；是否保存以工具结果为准。"""


def _chat_completion(messages: list[dict[str, Any]], llm_config: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    url = llm_config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": llm_config["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.1,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {llm_config['api_key']}"}
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"持仓 Agent 模型接口返回 {response.status_code}: {response.text[:600]}")
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("持仓 Agent 模型没有返回 choices")
    return choices[0].get("message") or {}


def _execute_position_tool(
    *,
    repository: StockRepository,
    name: str,
    args: dict[str, Any],
    created_by: str | None,
    source_name: str,
    image_mime_type: str,
    image_sha256: str | None,
    account_id: int | None = None,
) -> dict[str, Any]:
    if name == "get_position_schema":
        return {
            "required": ["symbol", "position_quantity"],
            "recommended": ["name", "available_quantity", "cost_price", "latest_price", "market_value", "floating_pnl", "position_pct", "stop_price"],
            "account": ["broker_name", "account_name", "account_no_masked", "account_type"],
            "examples": [
                "平安银行 000001.SZ，持仓1000股，可用1000股，成本10.2，现价10.8，止损9.89，仓位8%",
                "上传券商 APP 持仓明细截图，并补充截图日期/账户",
            ],
        }
    if name == "get_current_positions":
        return {"positions": repository.positions()[:50]}
    if name == "save_position_holdings":
        rows = args.get("rows") if isinstance(args.get("rows"), list) else []
        if not rows:
            return {"error": "没有可保存的持仓明细 rows"}
        account = args.get("account") if isinstance(args.get("account"), dict) else {}
        default_account = {**account}
        if account_id:
            default_account["account_id"] = account_id
        if args.get("as_of_date"):
            default_account["as_of_date"] = args.get("as_of_date")
        batch = repository.create_position_import_batch(
            source_name=source_name,
            image_mime_type=image_mime_type,
            image_sha256=image_sha256,
            raw_text=json.dumps(args, ensure_ascii=False),
            parsed_json=json.dumps(args, ensure_ascii=False),
            created_by=created_by,
        )
        saved_rows = repository.import_position_rows(rows, batch, default_account)
        repository.finish_position_import_batch(batch, saved_rows)
        errors = [row for row in saved_rows if "error" in row]
        ok_rows = [row for row in saved_rows if "error" not in row]
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "saved_count": len(ok_rows),
            "error_count": len(errors),
            "rows": saved_rows,
        }
    return {"error": f"未知持仓工具: {name}"}


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
    if name == "save_position_holdings":
        rows = args.get("rows") if isinstance(args.get("rows"), list) else []
        symbols = [str(row.get("symbol") or row.get("name") or "-") for row in rows[:5] if isinstance(row, dict)]
        return f"准备保存 {len(rows)} 条持仓: {', '.join(symbols) or '-'}"
    return "读取工具上下文"


def _tool_result_preview(name: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "save_position_holdings":
        return f"保存完成: 成功 {result.get('saved_count', 0)} 条，失败 {result.get('error_count', 0)} 条。"
    if name == "get_current_positions":
        return f"当前持仓 {len(result.get('positions') or [])} 条。"
    return "工具返回字段说明。"


def _fallback_reply(saved_count: int) -> str:
    if saved_count:
        return f"已保存 {saved_count} 条持仓记录。"
    return "你好，我是持仓录入 Agent。你可以直接输入持仓描述，或者上传券商持仓截图，我会在信息足够时调用工具保存。"
