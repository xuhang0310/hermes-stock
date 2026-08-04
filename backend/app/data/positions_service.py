"""持仓域 service 层：承载跨步编排逻辑（截图解析 / OCR / Agent 校验 / 持仓修正）。

不依赖 FastAPI 与 HTTP 异常——路由层只做"解析入参 → 调 service → 把异常映射为状态码"。
异常约定：PositionNotFoundError → 404，ValueError → 400，RuntimeError → 502。
"""

from __future__ import annotations

import json

from backend.app.agents.position_agent import (
    _execute_position_tool,
    extract_position_ocr_arguments,
    run_position_agent,
    validate_position_rows,
    validate_position_symbol as _validate_position_symbol,
)
from backend.app.agents.position_import import decode_image_payload, parse_position_screenshot
from backend.app.agent_runtime.transaction_state import transaction_state_service
from backend.app.data.errors import PositionNotFoundError
from backend.app.data.repository import StockRepository


def import_position_rows_from_image(
    *,
    repository: StockRepository,
    image_base64: str,
    mime_type: str,
    model_config_id: int | None,
    account_id: int | None,
    created_by: str,
) -> dict:
    """AI 截图导入：解析 → 建批 → 导入行 → 收尾。"""
    parsed = parse_position_screenshot(
        image_base64=image_base64,
        mime_type=mime_type,
        model_config_id=model_config_id,
    )
    batch = repository.create_position_import_batch(
        source_name=parsed["model"].get("display_name") or parsed["model"].get("model_name") or "vision_model",
        image_mime_type=mime_type,
        image_sha256=parsed.get("image_sha256"),
        raw_text=parsed.get("raw_text"),
        parsed_json=json.dumps(parsed.get("parsed"), ensure_ascii=False),
        created_by=created_by,
    )
    account_ctx = dict(parsed.get("account") or {})
    if account_id:
        account_ctx["account_id"] = account_id
    saved_rows = repository.import_position_rows(parsed.get("rows") or [], batch, account_ctx)
    repository.finish_position_import_batch(batch, saved_rows)
    return {
        "batch": {
            "id": batch.id,
            "status": batch.status,
            "image_sha256": batch.image_sha256,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
        },
        "model": parsed.get("model"),
        "rows": saved_rows,
    }


def recognize_position_ocr(
    *,
    image_base64: str,
    mime_type: str,
    model_config_id: int | None,
) -> dict:
    """识别一张持仓截图，返回固定 JSON 草稿，不写库。"""
    if not mime_type.lower().startswith("image/"):
        raise ValueError("仅支持图片文件")
    _, clean_base64, image_sha256 = decode_image_payload(image_base64)
    extracted = extract_position_ocr_arguments(
        user_content=[
            {
                "type": "text",
                "text": (
                    "识别这张 A 股券商持仓截图。只提取持仓明细行，不要提取资产汇总、"
                    "按钮或广告；返回完整持仓快照，每个标的只出现一次。截图没有显示股票"
                    "代码时，symbol 和 name 都填写截图中的证券名称，严禁凭记忆猜代码。"
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{clean_base64}",
                },
            },
        ],
        model_config_id=model_config_id,
    )
    arguments = extracted.get("arguments") or {}
    source_rows = arguments.get("rows")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("图片中没有识别到持仓明细")
    rows, validations = validate_position_rows(
        [dict(row) for row in source_rows if isinstance(row, dict)],
        prefer_name_on_conflict=True,
    )
    for row in rows:
        row["action"] = "replace"
    invalid_count = sum(1 for item in validations if not item.get("valid"))
    return {
        "schema_version": "position_ocr.v1",
        "image_sha256": image_sha256,
        "model": extracted.get("model") or {},
        "account": (
            arguments.get("account")
            if isinstance(arguments.get("account"), dict)
            else {}
        ),
        "as_of_date": arguments.get("as_of_date"),
        "rows": rows,
        "validations": validations,
        "valid_count": len(rows) - invalid_count,
        "invalid_count": invalid_count,
    }


def confirm_position_ocr(
    *,
    repository: StockRepository,
    rows: list[dict],
    account_id: int,
    as_of_date: str | None,
    image_sha256: str,
    mime_type: str,
    created_by: str,
) -> dict:
    """落库用户复核过的 OCR 草稿；所有 symbol 再次服务端校验（含名称-代码冲突）。"""
    if not repository.position_account_exists(account_id):
        raise PositionNotFoundError("持仓账户不存在或无权访问")
    # 与 recognize 同口径复验：拒绝仍带名称-代码冲突的行，防止误码被静默覆盖为规范名
    rows, validations = validate_position_rows(
        [dict(row) for row in rows if isinstance(row, dict)],
        prefer_name_on_conflict=True,
    )
    invalid = [item for item in validations if not item.get("valid")]
    if invalid:
        details = "；".join(
            f"第 {int(item['index']) + 1} 行 {item.get('input_symbol') or '-'}：{item.get('message')}"
            for item in invalid
        )
        raise ValueError(f"OCR 结果存在未通过校验的行：{details}")
    arguments: dict = {
        "rows": [
            {**row, "action": "replace"}
            for row in rows
            if isinstance(row, dict)
        ]
    }
    if as_of_date:
        arguments["as_of_date"] = as_of_date
    result = _execute_position_tool(
        repository=repository,
        name="save_position_holdings",
        args=arguments,
        created_by=created_by,
        source_name="持仓 OCR 人工确认",
        image_mime_type=mime_type,
        image_sha256=image_sha256,
        account_id=account_id,
        instruction=None,
        trusted_pending_action=True,
    )
    if result.get("need_account"):
        raise ValueError(result.get("message"))
    if result.get("error"):
        raise ValueError(result["error"])
    saved_count = int(result.get("saved_count") or 0)
    return {
        "schema_version": "position_ocr.v1",
        "batch_id": result.get("batch_id"),
        "status": result.get("status"),
        "saved_count": saved_count,
        "error_count": int(result.get("error_count") or 0),
        "rows": result.get("rows") or [],
        "summary": f"OCR 复核完成，已保存 {saved_count} 条持仓。",
    }


def agent_add_positions(
    *,
    repository: StockRepository,
    instruction: str,
    image_base64: str | None,
    mime_type: str,
    model_config_id: int | None,
    created_by: str,
    account_id: int | None,
    history: list[dict],
    pending_action: dict | None,
) -> dict:
    """自然语言 / 截图录入持仓：跑 Agent 解析意图，不直接写库。"""
    result = run_position_agent(
        repository=repository,
        instruction=instruction,
        image_base64=image_base64,
        mime_type=mime_type,
        model_config_id=model_config_id,
        created_by=created_by,
        account_id=account_id,
        history=history,
        pending_action=pending_action,
    )
    return {
        "batch": None,
        "model": result.get("model"),
        "intent": result.get("intent"),
        "should_save": result.get("should_save"),
        "reply": result.get("reply"),
        "rows": result.get("rows") or [],
        "steps": result.get("steps") or [],
        "summary": result.get("summary") or result.get("reply"),
        "pending_action": result.get("pending_action"),
    }


def confirm_agent_positions(
    *,
    repository: StockRepository,
    rows: list[dict],
    account: dict | None,
    as_of_date: str | None,
    account_id: int | None,
    user_id: int,
    created_by: str,
) -> dict:
    """人工复核 Agent 解析结果后落库；成功后清空待办。"""
    arguments: dict = {"rows": rows}
    if account:
        arguments["account"] = account
    if as_of_date:
        arguments["as_of_date"] = as_of_date
    result = _execute_position_tool(
        repository=repository,
        name="save_position_holdings",
        args=arguments,
        created_by=created_by,
        source_name="AgentScope OCR 人工复核",
        image_mime_type="image/ocr-reviewed",
        image_sha256=None,
        account_id=account_id,
    )
    if result.get("need_account"):
        raise ValueError(result.get("message"))
    if result.get("error"):
        raise ValueError(result["error"])
    saved_count = int(result.get("saved_count") or 0)
    if saved_count:
        transaction_state_service.clear_pending_for_user(
            user_id,
            "position-entry-agent",
        )
    return {
        "rows": result.get("rows") or [],
        "should_save": saved_count > 0,
        "summary": f"复核完成，已保存 {saved_count} 条持仓。",
    }


def correct_position(
    *,
    repository: StockRepository,
    position_id: int,
    symbol: str,
    name: str,
    position_quantity: float,
    cost_price: float,
) -> dict:
    """修正持仓：校验股票代码（含同代码豁免），更新持仓。"""
    current = next(
        (item for item in repository.positions() if int(item.get("id") or 0) == position_id),
        None,
    )
    if current is None:
        raise PositionNotFoundError("持仓不存在或不属于当前用户")
    validation = validate_position_symbol(symbol, name)
    same_existing_symbol = (
        validation.get("normalized_symbol") == current.get("symbol")
        and validation.get("status") == "catalog_unavailable"
    )
    if not validation.get("valid") and not same_existing_symbol:
        raise ValueError(validation.get("message") or "股票代码校验失败")
    data = {
        "symbol": validation.get("normalized_symbol") or current["symbol"],
        "name": name.strip(),
        "position_quantity": position_quantity,
        "cost_price": cost_price,
    }
    # DAO 直接抛 PositionNotFoundError（预检与落库间竞态删除时同样 404），
    # 数量/成本/重复持仓等校验错误保持 ValueError → 400。
    return repository.correct_position_holding(position_id, data)


def validate_position_symbol(symbol: str, name: str | None = None) -> dict:
    """校验单个股票代码/名称（供路由透传，与 correct_position 同源）。"""
    return _validate_position_symbol(symbol, name)


def discard_agent_position_draft(user_id: int) -> bool:
    """清空持仓 Agent 的待办草稿。"""
    return transaction_state_service.clear_pending_for_user(user_id, "position-entry-agent")