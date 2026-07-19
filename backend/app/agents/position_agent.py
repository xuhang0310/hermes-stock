from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import date
from typing import Any

from backend.app.agents.position_import import decode_image_payload
from backend.app.agents.position_intent import PositionAction, build_intent_hint, detect_position_intent, intent_summary
from backend.app.data.repository import StockRepository
from backend.app.agents.stock_agent import _stream_chat
from backend.app.ai.llm_client import chat_completion
from backend.app.core.llm_config import active_llm_config


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
                                "latest_price": {"type": "number", "description": "现价/最新价。截图里有就顺带填，没有绝不要向用户索要——系统会定时自动同步行情价"},
                                "market_value": {"type": "number", "description": "持仓市值。截图里有才填，缺失由系统按行情自动计算"},
                                "floating_pnl": {"type": "number", "description": "浮动盈亏。截图里有才填，缺失由系统按行情自动计算"},
                                "floating_pnl_pct": {"type": "number", "description": "浮动盈亏比例，单位为百分数。截图里有才填"},
                                "day_pnl": {"type": "number", "description": "当日盈亏"},
                                "position_pct": {"type": "number", "description": "仓位占比，单位为百分数"},
                                "stop_price": {"type": "number", "description": "止损价"},
                                "take_profit_price": {"type": "number", "description": "止盈价"},
                                "thesis": {"type": "string", "description": "持仓逻辑"},
                                "risk_note": {"type": "string", "description": "风险说明"},
                                "confidence": {"type": "number", "description": "识别置信度 0-1"},
                                "action": {
                                    "type": "string",
                                    "enum": ["buy", "sell", "add", "reduce", "close", "replace", "update_field"],
                                    "description": (
                                        "本条持仓的交易动作。根据用户意图选择："
                                        "buy=买入新股票(累加数量+加权成本+自动加入自选)；"
                                        "add=加仓已有持仓(同buy)；"
                                        "sell=全部卖出(清仓关闭)；reduce=减仓(扣减数量)；"
                                        "close=直接关闭持仓(无交易仅关闭)；"
                                        "replace=完全替换(覆盖旧数据，未指定时默认)；"
                                        "update_field=仅修改字段如止损止盈(不改数量)。"
                                        "用户说'买入/加仓/新增'应选buy/add；'卖出全部/清仓/止损出局'应选sell/close；'减持/卖出部分'应选reduce。"
                                    ),
                                },
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
    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)

    system_prompt = _position_agent_prompt()
    # P1: 意图预处理 — 在 LLM 调用前检测交易动作，注入 system prompt
    text = instruction.strip() or "请识别并录入这张持仓截图。"
    intent = detect_position_intent(text) if not image_base64 else None
    intent_hint = build_intent_hint(intent) if intent and intent.action != PositionAction.UNKNOWN else ""
    if intent_hint:
        system_prompt += intent_hint
    user_content: str | list[dict[str, Any]]
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
    cancel_event: threading.Event | None = None,
) -> Generator[dict[str, Any], None, None]:
    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)

    text = instruction.strip() or "请识别并录入这张持仓截图。"
    # P1: 意图预处理 — 在 LLM 调用前检测交易动作，注入 system prompt
    intent = detect_position_intent(text) if not image_base64 else None  # 截图不预处理
    intent_hint = build_intent_hint(intent) if intent and intent.action != PositionAction.UNKNOWN else ""
    system_prompt = _position_agent_prompt()
    if intent_hint:
        system_prompt += intent_hint
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
        {"role": "system", "content": system_prompt},
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
        if cancel_event and cancel_event.is_set():
            return
        message: dict[str, Any] | None = None
        streamed_content = []
        for kind, payload in _stream_chat(messages, llm_config, POSITION_AGENT_TOOLS):
            if cancel_event and cancel_event.is_set():
                return
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
3. 信息不足时先追问，不要保存半截数据。判断"信息是否足够"只看：代码/名称、数量、成本价、**证券账户**这类账户事实。
4. 最新价、市值、浮动盈亏属于行情数据，系统每个交易日会自动同步，**绝不要因为缺这几个字段而追问用户**；截图里有就顺带保存，没有直接留空。
5. **【账户确认 — 必须遵守】保存前必须明确证券账户。如果用户没说明账户（券商名称/账号），且未在账户下拉预选，绝不要直接调 save_position_holdings——先追问"这批持仓保存到哪个证券账户？"。工具返回 need_account 时也按此追问，不要重复保存。卖出/减仓命中原有持仓账户除外。
6. 识别到真实持仓明细且账户已明确后，调用 save_position_holdings 工具保存。
7. 保存后基于工具 observation 回复用户，明确保存了哪些标的、哪些字段缺失（行情类字段缺失不必提醒，说明系统会自动同步即可）。

【交易动作识别规则 — 必须严格遵守】
当用户表达以下意图时，按对应规则处理，不要混淆动作：

1. **买入/建仓**（关键词：买入、买进、建仓、开仓、想买、新开）:
   用户要新建一个标的的持仓。先确认标的代码和名称（用户只给名称时追问代码），
   再确认数量和成本价。保存时 rows 中必须同时包含该新标的行和所有已有持仓行
  （从 get_current_positions 获取完整列表），不能只传新标的导致其他持仓被覆盖。
   新标的 position_quantity=买入数量，status="open"。

2. **卖出/减仓**（关键词：卖出、减持、减仓、出一部分、抛）:
   用户要减少某标的的持仓数量。必须先用 get_current_positions 读取该标的
   当前 quantity，新 quantity = 当前数量 - 卖出数量。保存时 rows 含所有持仓行。
   若卖出后剩余 quantity=0，则等同于清仓（见第3条）。

3. **清仓**（关键词：清仓、全出、全部卖出、全抛、止损出场、不拿了）:
   用户要完全退出某标的。先用 get_current_positions 确认该持仓存在，
   然后 rows 中该标的 position_quantity=0（系统自动将 status 转为 closed）。
   这是不可逆操作，保存前必须复述确认（"将清仓 XXX 共 N 股，确认吗？"）。
   绝不要自作主张替用户清仓；用户明确说清仓关键词时才执行。

4. **加仓/补仓**（关键词：加仓、补仓、增持、多买、再买）:
   用户要对已有持仓增加数量。先用 get_current_positions 获取当前 quantity，
   新 quantity = 当前数量 + 加仓数量。成本价按全仓加权平均：
   新成本 = (原成本*原数量 + 新价格*新数量) / (原数量+新数量)。
   用户没给新成交价时，追问新买入价格。

5. **改止损/止盈/逻辑**（关键词：改止损、设止损、调整止损、修改止盈）:
   这是字段级微调，用 update_position_field 工具（如果有），
   或 save_position_holdings 只改该目标的对应字段（保持 quantity 不变）。
   不需要追问数量，只需要确认新的止损/止盈价格。

6. **查看持仓**（关键词：查看持仓、我的仓位、持有哪些、仓位怎样）:
   调用 get_current_positions 读取并展示即可，不调用保存工具。

A 股代码规范:
- 6 开头通常为 .SH，0/3 开头通常为 .SZ，4/8 开头通常为 .BJ。
- 数值字段传纯数字，例如 8% 传 8，1,000 股传 1000。
- 不能假装保存；是否保存以工具结果为准。

回复要求:
- 中文 Markdown。简短、清楚、有下一步。
- 涉及清仓/卖出操作时，必须复述确认后才保存。"""


def _chat_completion(messages: list[dict[str, Any]], llm_config: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    body = chat_completion(messages, llm_config, tools=tools, temperature=0.1)
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
            "recommended": ["name", "available_quantity", "cost_price", "position_pct", "stop_price"],
            "auto_synced": ["latest_price", "market_value", "floating_pnl", "floating_pnl_pct"],
            "note": "最新价/市值/浮盈由系统每个交易日自动同步行情，无需用户提供。",
            "account": ["broker_name", "account_name", "account_no_masked", "account_type"],
            "examples": [
                "平安银行 000001.SZ，持仓1000股，可用1000股，成本10.2，止损9.89，仓位8%",
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
        # 账户确认兜底：没说账户且账户字段全空且含买入/新增动作 → 追问，不建 batch。
        # 纯卖出/减仓放行：merge_position_holding 内部 _unique_open_account_for
        # 已按标的自动匹配已有持仓账户，不会乱建账户。
        if not account_id and not any(account.get(k) for k in
                ("broker_name", "account_no_masked", "account_name", "broker_code")):
            has_buy = any((r.get("action") or "buy") in ("buy", "add", "replace")
                          for r in rows if isinstance(r, dict))
            if has_buy:
                return {"need_account": True,
                        "message": "请先确认这批持仓保存到哪个证券账户？请提供券商名称或账号，或在抽屉顶部账户下拉预选。"}
        batch = repository.create_position_import_batch(
            source_name=source_name,
            image_mime_type=image_mime_type,
            image_sha256=image_sha256,
            raw_text=json.dumps(args, ensure_ascii=False),
            parsed_json=json.dumps(args, ensure_ascii=False),
            created_by=created_by,
        )
        saved_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            merged = {**default_account, **row}
            action = merged.pop("action", None)
            try:
                saved_rows.append(
                    repository.merge_position_holding(
                        merged, action=action, source_type="ai_ocr", commit=False
                    )
                )
            except ValueError as exc:
                saved_rows.append({"error": str(exc), "raw": row})
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
