from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import date
from typing import Any

from backend.app.agents.position_import import decode_image_payload
from backend.app.agents.position_intent import PositionAction, build_intent_hint, detect_position_intent
from backend.app.data.repository import StockRepository
from backend.app.agents.stock_agent import _stream_chat
from backend.app.ai.llm_client import chat_completion
from backend.app.core.llm_config import active_llm_config, resolve_vision_model_config_id
from backend.app.market.stock_tools import (
    lookup_a_share_instrument,
    normalize_a_share_symbol,
    resolve_stock,
)


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
            "description": (
                "增量保存本次已确认的 A 股持仓变更。rows 只能包含用户本次明确要求新增、"
                "加减仓、清仓或修改的标的；已有但未发生变化的持仓只作为读取上下文，严禁重复提交。"
                "只有识别到真实持仓记录且字段足够时才能调用。"
            ),
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
                        "description": (
                            "本次发生变更的持仓明细行，不是完整持仓清单。数值必须是纯数字，"
                            "不要带元、股、逗号或百分号。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string", "description": "A股代码，如 600519.SH、000001.SZ，也可传 6 位代码"},
                                "name": {"type": "string", "description": "证券名称"},
                                "position_quantity": {
                                    "type": "number",
                                    "description": (
                                        "数量。action=buy/add/sell/reduce 时表示本次买入、加仓、卖出或减仓的数量；"
                                        "action=replace 时才表示变更后的完整持仓数量；action=close 时传 0。"
                                    ),
                                },
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

POSITION_AGENT_PROFILE_CODE = "position-entry-agent"

_PENDING_ACCOUNT_FIELDS = {
    "broker_name",
    "account_name",
    "account_no_masked",
    "account_type",
}
_PENDING_ROW_FIELDS = {
    "symbol",
    "name",
    "position_quantity",
    "available_quantity",
    "frozen_quantity",
    "today_buy_quantity",
    "today_sell_quantity",
    "cost_price",
    "cost_amount",
    "latest_price",
    "market_value",
    "floating_pnl",
    "floating_pnl_pct",
    "day_pnl",
    "position_pct",
    "stop_price",
    "take_profit_price",
    "thesis",
    "risk_note",
    "confidence",
    "action",
}


def _bounded_pending_value(value: Any, *, max_length: int = 500) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:max_length]


def _normalize_pending_action(value: Any) -> dict[str, Any] | None:
    """Keep only the bounded position draft needed to continue a clarification turn."""
    if not isinstance(value, dict) or value.get("tool") != "save_position_holdings":
        return None
    arguments = value.get("arguments")
    if not isinstance(arguments, dict):
        return None
    source_rows = arguments.get("rows")
    if not isinstance(source_rows, list):
        return None

    rows: list[dict[str, Any]] = []
    for source_row in source_rows[:100]:
        if not isinstance(source_row, dict):
            continue
        row = {
            key: _bounded_pending_value(source_row[key])
            for key in _PENDING_ROW_FIELDS
            if key in source_row
        }
        if row:
            rows.append(row)
    if not rows:
        return None

    account_source = arguments.get("account")
    account = (
        {
            key: _bounded_pending_value(account_source[key], max_length=200)
            for key in _PENDING_ACCOUNT_FIELDS
            if key in account_source
        }
        if isinstance(account_source, dict)
        else {}
    )
    normalized_arguments: dict[str, Any] = {"rows": rows}
    if account:
        normalized_arguments["account"] = account
    if arguments.get("as_of_date"):
        normalized_arguments["as_of_date"] = str(arguments["as_of_date"])[:32]
    normalized: dict[str, Any] = {
        "tool": "save_position_holdings",
        "arguments": normalized_arguments,
    }
    if value.get("review_required"):
        normalized["review_required"] = True
    validations = value.get("validations")
    if isinstance(validations, list):
        normalized["validations"] = [
            {
                key: _bounded_pending_value(item[key], max_length=200)
                for key in (
                    "index",
                    "valid",
                    "status",
                    "input_symbol",
                    "normalized_symbol",
                    "canonical_name",
                    "message",
                    "suggested_symbol",
                    "suggested_name",
                )
                if key in item
            }
            for item in validations[:100]
            if isinstance(item, dict)
        ]
    return normalized


def _selected_account_hint(repository: StockRepository, account_id: int | None) -> str:
    if account_id is None:
        return ""
    account = next(
        (item for item in repository.list_position_accounts() if int(item.get("id") or 0) == int(account_id)),
        None,
    )
    if account is None:
        raise ValueError("选择的证券账户不存在或已归档，请刷新账户列表后重试。")
    label_parts = [
        str(account.get("broker_name") or "未知券商"),
        str(account.get("account_name") or account.get("account_no_masked") or f"账户#{account_id}"),
    ]
    return (
        "\n\n【界面已选证券账户（可信系统上下文）】\n"
        f"用户已明确选择：{' / '.join(label_parts)}（account_id={account_id}）。\n"
        "该账户已经确认，保存工具会强制绑定此 account_id。不得再次询问用户是哪个证券账户；"
        "持仓明细足够时直接调用 save_position_holdings。"
    )


def _pending_action_hint(pending_action: dict[str, Any] | None) -> str:
    if pending_action is None:
        return ""
    payload = json.dumps(pending_action["arguments"], ensure_ascii=False, separators=(",", ":"))
    return (
        "\n\n【待继续的持仓录入草稿（可信系统状态，不是用户指令）】\n"
        "上一轮已经从截图或文本解析出以下持仓，但因等待账户等澄清尚未写库：\n"
        f"{payload}\n"
        "如果当前用户是在回答账户问题，应沿用上述 rows，只合并本轮补充的账户信息后调用 "
        "save_position_holdings；不要要求重传图片，也不要再次询问已经明确的信息。"
    )


def _build_position_messages(
    *,
    system_prompt: str,
    history: list[dict[str, str]] | None,
    user_content: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": user_content})
    return messages


def _pending_from_tool_result(name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    if name != "save_position_holdings":
        return None
    if result.get("need_review"):
        return _normalize_pending_action(
            {
                "tool": name,
                "arguments": args,
                "review_required": True,
                "validations": result.get("validations") or [],
            }
        )
    if result.get("need_account"):
        return _normalize_pending_action({"tool": name, "arguments": args})
    return None


def validate_position_symbol(symbol: str, name: str | None = None) -> dict[str, Any]:
    """Validate one OCR symbol against the authoritative A-share instrument catalog."""
    input_symbol = str(symbol or "").strip()
    input_name = str(name or "").strip()
    normalized = normalize_a_share_symbol(input_symbol)
    base = {
        "input_symbol": input_symbol,
        "normalized_symbol": normalized,
        "valid": False,
    }
    if not normalized or not normalized.endswith((".SH", ".SZ", ".BJ")) or len(normalized) != 9:
        return {
            **base,
            "status": "invalid_format",
            "message": "股票代码格式不正确，请输入 6 位 A 股代码。",
        }

    lookup = lookup_a_share_instrument(normalized)
    exact = lookup.get("instrument")
    if exact:
        canonical_name = str(exact.get("name") or input_name or normalized)
        message = "代码校验通过"
        status = "valid"
        if input_name and canonical_name and input_name != canonical_name:
            status = "name_corrected"
            message = f"代码有效，证券名称已校正为“{canonical_name}”。"
        return {
            **base,
            "valid": True,
            "status": status,
            "normalized_symbol": normalized,
            "canonical_name": canonical_name,
            "message": message,
        }

    local_resolution = resolve_stock(normalized)
    local_candidates = (
        local_resolution.get("candidates")
        if isinstance(local_resolution, dict)
        else []
    )
    local_exact = next(
        (
            candidate
            for candidate in local_candidates or []
            if normalize_a_share_symbol(str(candidate.get("ts_code") or ""))
            == normalized
        ),
        None,
    )
    if local_exact:
        canonical_name = str(local_exact.get("name") or input_name or normalized)
        status = "valid_local"
        message = "代码已通过本地证券目录校验"
        if input_name and canonical_name and input_name != canonical_name:
            status = "name_corrected"
            message = f"代码有效，证券名称已校正为“{canonical_name}”。"
        return {
            **base,
            "valid": True,
            "status": status,
            "normalized_symbol": normalized,
            "canonical_name": canonical_name,
            "message": message,
        }

    if not lookup.get("available"):
        return {
            **base,
            "status": "catalog_unavailable",
            "message": "上市证券目录暂时不可用，当前不能可靠校验，请稍后重试。",
        }

    suggestion = None
    if input_name:
        by_name = resolve_stock(input_name)
        name_candidates = by_name.get("candidates") if isinstance(by_name, dict) else []
        if len(name_candidates or []) == 1:
            suggestion = name_candidates[0]
    return {
        **base,
        "status": "not_found",
        "message": "未在 A 股证券目录中查到该代码，请核对 OCR 识别结果。",
        "suggested_symbol": str(suggestion.get("ts_code") or "") if suggestion else None,
        "suggested_name": str(suggestion.get("name") or "") if suggestion else None,
    }


def validate_position_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized_rows: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    for index, source_row in enumerate(rows):
        row = dict(source_row)
        validation = {"index": index, **validate_position_symbol(row.get("symbol", ""), row.get("name"))}
        if validation["valid"]:
            row["symbol"] = validation["normalized_symbol"]
            row["name"] = validation.get("canonical_name") or row.get("name")
        normalized_rows.append(row)
        validations.append(validation)
    return normalized_rows, validations


_COLLECTIVE_POSITION_WRITE_HINTS = (
    "整仓",
    "全部持仓",
    "所有持仓",
    "完整持仓",
    "整批",
    "批量录入",
    "整张截图",
)


def _restrict_rows_to_explicit_targets(
    rows: list[dict[str, Any]],
    instruction: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep a text-driven incremental write scoped to symbols named by the user.

    Current positions may be read for context, but an LLM must not echo those
    unchanged rows into a one-symbol write. Screenshot/full-portfolio imports
    remain multi-row operations.
    """
    if instruction is None:
        return rows, []
    text = str(instruction).strip()
    if not text:
        return [], [
            str(row.get("name") or row.get("symbol") or "未命名标的")
            for row in rows
            if isinstance(row, dict)
        ]

    upper_text = text.upper()
    matched: list[dict[str, Any]] = []
    omitted: list[str] = []
    for row in rows:
        symbol = normalize_a_share_symbol(
            str(row.get("symbol") or row.get("ts_code") or row.get("code") or "")
        )
        digits = symbol.split(".", 1)[0] if symbol else ""
        name = str(row.get("name") or row.get("symbol_name") or "").strip()
        is_target = bool(
            (symbol and symbol in upper_text)
            or (digits and digits in upper_text)
            or (len(name) >= 2 and name in text)
        )
        if is_target:
            matched.append(row)
        else:
            omitted.append(name or symbol or "未命名标的")

    return matched, omitted


def _merge_pending_tool_args(
    name: str,
    args: dict[str, Any],
    pending_action: dict[str, Any] | None,
) -> dict[str, Any]:
    """Make a clarification turn reuse the parsed rows even if the model only emits the new account."""
    if name != "save_position_holdings" or pending_action is None:
        return args
    pending_args = pending_action.get("arguments")
    if not isinstance(pending_args, dict):
        return args

    merged = {**pending_args, **args}
    if not isinstance(args.get("rows"), list) or not args["rows"]:
        merged["rows"] = pending_args.get("rows") or []
    pending_account = pending_args.get("account") if isinstance(pending_args.get("account"), dict) else {}
    current_account = args.get("account") if isinstance(args.get("account"), dict) else {}
    if pending_account or current_account:
        merged["account"] = {**pending_account, **current_account}
    return merged


def run_position_agent(
    *,
    repository: StockRepository,
    instruction: str,
    image_base64: str | None = None,
    mime_type: str = "image/png",
    model_config_id: int | None = None,
    created_by: str | None = None,
    account_id: int | None = None,
    history: list[dict[str, str]] | None = None,
    pending_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)
        model_config_id = resolve_vision_model_config_id(model_config_id)

    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    normalized_pending = _normalize_pending_action(pending_action)
    system_prompt = (
        _position_agent_prompt()
        + _selected_account_hint(repository, account_id)
        + _pending_action_hint(normalized_pending)
    )
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

    messages = _build_position_messages(
        system_prompt=system_prompt,
        history=history,
        user_content=user_content,
    )
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
            args = _merge_pending_tool_args(name, args, normalized_pending)
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
                instruction=text,
            )
            pending_from_result = _pending_from_tool_result(name, args, result)
            if pending_from_result is not None:
                normalized_pending = pending_from_result
            if name == "save_position_holdings":
                saved_rows = result.get("rows") or []
                saved_count = int(result.get("saved_count") or 0)
                if saved_count:
                    normalized_pending = None
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
        "pending_action": normalized_pending,
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
    history: list[dict[str, str]] | None = None,
    pending_action: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)
        model_config_id = resolve_vision_model_config_id(model_config_id)

    llm_config = active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    text = instruction.strip() or "请识别并录入这张持仓截图。"
    # P1: 意图预处理 — 在 LLM 调用前检测交易动作，注入 system prompt
    intent = detect_position_intent(text) if not image_base64 else None  # 截图不预处理
    intent_hint = build_intent_hint(intent) if intent and intent.action != PositionAction.UNKNOWN else ""
    normalized_pending = _normalize_pending_action(pending_action)
    system_prompt = (
        _position_agent_prompt()
        + _selected_account_hint(repository, account_id)
        + _pending_action_hint(normalized_pending)
    )
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
    messages = _build_position_messages(
        system_prompt=system_prompt,
        history=history,
        user_content=user_content,
    )
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
                "pending_action": normalized_pending,
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
            args = _merge_pending_tool_args(name, args, normalized_pending)
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
                instruction=text,
            )
            pending_from_result = _pending_from_tool_result(name, args, result)
            if pending_from_result is not None:
                normalized_pending = pending_from_result
            if name == "save_position_holdings":
                saved_rows = result.get("rows") or []
                saved_count = int(result.get("saved_count") or 0)
                if saved_count:
                    normalized_pending = None
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
        "pending_action": normalized_pending,
    }


def _position_agent_prompt() -> str:
    return f"""你是 SixRabbits Stock 的持仓录入 Agent，使用 ReAct / tool-calling 工作。今天日期: {date.today().isoformat()}

你的职责:
1. 和用户对话，理解自然语言或截图里的 A 股持仓。
2. 判断是否需要调用工具。问候、闲聊、能力咨询，不调用保存工具。
3. 信息不足时先追问，不要保存半截数据。判断"信息是否足够"只看：代码/名称、数量、成本价、**证券账户**这类账户事实。
4. 最新价、市值、浮动盈亏属于行情数据，系统每个交易日会自动同步，**绝不要因为缺这几个字段而追问用户**；截图里有就顺带保存，没有直接留空。
5. **【账户确认 — 必须遵守】保存前必须明确证券账户。系统提示界面已预选账户时，该账户已经确认，直接使用且不得再次追问。如果没有预选账户、截图也没有账户信息，但持仓明细已经识别完整，仍调用一次 save_position_holdings 提交解析结果；工具会在不写库的情况下返回 need_account 并保留待续草稿，然后只追问"这批持仓保存到哪个证券账户？"。下一轮用户补充账户后必须沿用待续草稿，不得要求重传图片。卖出/减仓命中原有持仓账户除外。
6. 识别到真实持仓明细且账户已明确后，调用 save_position_holdings 工具保存。
7. 保存后基于工具 observation 回复用户，明确保存了哪些标的、哪些字段缺失（行情类字段缺失不必提醒，说明系统会自动同步即可）。
8. save_position_holdings 是增量工具。rows 只允许包含用户本轮明确要求变更的标的；
   get_current_positions 返回的其他已有持仓只能用于核对，严禁原样塞回 rows 重复写入。
9. 当前轮没有新截图、没有系统提供的待确认草稿、也没有明确持仓明细时，禁止根据历史对话
   重建或保存旧持仓；只说明当前没有可确认草稿，并请用户重新上传截图或提供明细。

【交易动作识别规则 — 必须严格遵守】
当用户表达以下意图时，按对应规则处理，不要混淆动作：

1. **买入/建仓**（关键词：买入、买进、建仓、开仓、想买、新开）:
   用户要新建一个标的的持仓。先确认标的代码和名称（用户只给名称时追问代码），
   再确认数量和成本价。保存时 rows 只传该新标的一行，action="buy"，
   position_quantity=本次买入数量。绝不能附带其他未变化的已有持仓。

2. **卖出/减仓**（关键词：卖出、减持、减仓、出一部分、抛）:
   用户要减少某标的的持仓数量。必须先用 get_current_positions 读取该标的
   当前 quantity 并校验不超卖。保存时 rows 只传该目标标的一行，action="sell" 或
   action="reduce"，position_quantity=本次卖出/减仓数量；后端负责计算剩余持仓。
   不要自行计算后再传剩余数量，也不要附带其他持仓。

3. **清仓**（关键词：清仓、全出、全部卖出、全抛、止损出场、不拿了）:
   用户要完全退出某标的。先用 get_current_positions 确认该持仓存在，
   然后 rows 中该标的 position_quantity=0（系统自动将 status 转为 closed）。
   这是不可逆操作，保存前必须复述确认（"将清仓 XXX 共 N 股，确认吗？"）。
   绝不要自作主张替用户清仓；用户明确说清仓关键词时才执行。

4. **加仓/补仓**（关键词：加仓、补仓、增持、多买、再买）:
   用户要对已有持仓增加数量。先用 get_current_positions 获取当前 quantity，
   保存时 rows 只传该目标标的一行，action="add"，position_quantity=本次加仓数量，
   cost_price=本次成交价；后端负责累加数量和计算加权成本。用户没给成交价时追问。

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
    body = chat_completion(messages, llm_config, tools=tools)
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("持仓 Agent 模型没有返回 choices")
    return choices[0].get("message") or {}


def extract_position_ocr_arguments(
    *,
    user_content: str | list[dict[str, Any]],
    model_config_id: int | None,
) -> dict[str, Any]:
    """Extract a new screenshot into one bounded, structured import draft."""

    resolved_model_id = resolve_vision_model_config_id(model_config_id)
    llm_config = active_llm_config(resolved_model_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前视觉模型未配置 API Key 或 Base URL。")
    messages = [
        {
            "role": "system",
            "content": (
                "你是 A 股券商持仓截图结构化提取器。只处理本轮用户消息中的新截图，"
                "不得引用历史对话中的持仓。必须调用 save_position_holdings，不能直接回答文本。"
                "完整持仓截图中的每个可见标的只输出一次，action 使用 replace；"
                "提取代码、名称、数量、成本价以及截图中明确可见的现价、市值和盈亏。"
                "截图可见券商、账户名称或脱敏账号时写入 account。看不清的字段不要猜。"
            ),
        },
        {"role": "user", "content": user_content},
    ]
    body = chat_completion(
        messages,
        llm_config,
        tools=[POSITION_AGENT_TOOLS[2]],
        tool_choice="auto",
    )
    choices = body.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        function = call.get("function") or {}
        if function.get("name") != "save_position_holdings":
            continue
        arguments = _json_args(function.get("arguments"))
        rows = arguments.get("rows")
        if isinstance(rows, list) and rows:
            return {
                "arguments": arguments,
                "model": {
                    "id": llm_config.get("id"),
                    "display_name": llm_config.get("display_name"),
                    "model_name": llm_config.get("model"),
                    "provider_name": llm_config.get("provider_name"),
                },
            }
    raise RuntimeError("视觉模型未返回可用的持仓明细，请重新上传更清晰的截图。")


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
    instruction: str | None = None,
    trusted_pending_action: bool = False,
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
        ignored_rows: list[str] = []
        if not image_sha256 and not trusted_pending_action:
            rows, ignored_rows = _restrict_rows_to_explicit_targets(
                [row for row in rows if isinstance(row, dict)],
                instruction,
            )
        if not rows:
            return {
                "error": (
                    "本轮没有明确指定可写入的持仓，系统已阻止复用历史聊天中的旧持仓。"
                    "请重新上传截图，或明确输入股票代码、数量和成本价。"
                ),
                "history_reuse_blocked": True,
                "saved_count": 0,
                "error_count": 0,
                "rows": [],
                "ignored_unchanged_count": len(ignored_rows),
                "ignored_unchanged_rows": ignored_rows,
            }
        rows, validations = validate_position_rows(
            [row for row in rows if isinstance(row, dict)]
        )
        args["rows"] = rows
        invalid = [item for item in validations if not item.get("valid")]
        if image_sha256 and not trusted_pending_action:
            return {
                "need_review": True,
                "message": (
                    f"OCR 已识别 {len(rows)} 条持仓，其中 {len(invalid)} 条代码需要修正。"
                    if invalid
                    else f"OCR 已识别并校验 {len(rows)} 条持仓，请确认后再入库。"
                ),
                "saved_count": 0,
                "error_count": len(invalid),
                "rows": rows,
                "validations": validations,
            }
        if invalid:
            details = "；".join(
                f"第 {int(item['index']) + 1} 行 {item.get('input_symbol') or '-'}：{item.get('message')}"
                for item in invalid
            )
            return {
                "error": f"股票代码校验未通过：{details}",
                "saved_count": 0,
                "error_count": len(invalid),
                "rows": rows,
                "validations": validations,
            }
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
                        merged,
                        action=action,
                        source_type="ai_ocr",
                        import_batch=batch,
                        commit=False,
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
            "ignored_unchanged_count": len(ignored_rows),
            "ignored_unchanged_rows": ignored_rows,
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
        if result.get("need_review"):
            return str(result.get("message") or "OCR 识别完成，等待核对股票代码后入库。")
        if result.get("need_account"):
            return str(result.get("message") or "等待用户确认证券账户。")
        ignored = int(result.get("ignored_unchanged_count") or 0)
        suffix = f"；已拦截 {ignored} 条非本次目标持仓的重复提交。" if ignored else ""
        return (
            f"保存完成: 成功 {result.get('saved_count', 0)} 条，"
            f"失败 {result.get('error_count', 0)} 条{suffix}"
        )
    if name == "get_current_positions":
        return f"当前持仓 {len(result.get('positions') or [])} 条。"
    return "工具返回字段说明。"


def _fallback_reply(saved_count: int) -> str:
    if saved_count:
        return f"已保存 {saved_count} 条持仓记录。"
    return "你好，我是持仓录入 Agent。你可以直接输入持仓描述，或者上传券商持仓截图，我会在信息足够时调用工具保存。"
