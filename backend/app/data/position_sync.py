"""持仓同步向导：多张券商截图 → 识别 → 和系统持仓对比 → 一次确认 → 可一键撤销。

和旧的单张 OCR 导入相比：
- 一次可传多张截图（同一账户往下滑的几屏），按股票合并去重；
- 同时读出账户资金（可用资金、总资产、持仓市值）和账号尾号、券商，自动判断是哪个账户；
- 先给“变化清单”（新增 / 有变化 / 截图里没有 / 没变化 / 代码待确认），用户点一次确认才写库；
- 同步是“校正”，不写交易流水；只有用户勾选“这是一笔买入/卖出”才记成交；
- 每次同步一个批次，记下同步前的资金和水位线，可一键撤销。

本模块只做识别、合并、账户判断和入参校验，不写库；写库在 PositionSyncMixin。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.agents.position_import import decode_image_payload
from backend.app.data.broker_presets import BROKER_PRESETS
from backend.app.data.repository import StockRepository

MAX_SYNC_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SYNC_EXTRACT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_position_screenshot",
        "description": "提交从一张券商持仓截图里读到的账户、资金和持仓明细。",
        "parameters": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "object",
                    "properties": {
                        "broker_name": {"type": "string", "description": "截图上能看到的券商名称，看不到留空"},
                        "app_name": {"type": "string", "description": "交易 App 名称，如 小方、易淘金、信e投、同花顺，看不到留空"},
                        "account_no_masked": {
                            "type": "string",
                            "description": "截图上的资金账号或股东账号，可能是脱敏的（如 28****642），原样抄写，看不到留空",
                        },
                    },
                },
                "funds": {
                    "type": "object",
                    "description": "账户资金区，看得到才填",
                    "properties": {
                        "total_asset": {"type": "number", "description": "总资产"},
                        "available_cash": {"type": "number", "description": "可用资金/可用余额（不是可取资金）"},
                        "holding_market_value": {"type": "number", "description": "持仓市值/股票市值"},
                        "total_pnl": {"type": "number", "description": "持仓盈亏/浮动盈亏合计"},
                        "day_pnl": {"type": "number", "description": "当日盈亏/今日参考盈亏"},
                    },
                },
                "as_of_date": {"type": "string", "description": "截图上能看到的日期，格式 YYYY-MM-DD，看不到留空"},
                "rows": {
                    "type": "array",
                    "description": "持仓明细，每个完整可见的标的一行；被截断、看不全的行不要提交。截图里没有持仓明细就传空数组。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "6 位股票代码；截图没显示代码时填证券名称，严禁凭记忆猜代码",
                            },
                            "name": {"type": "string", "description": "证券名称，原样抄写"},
                            "position_quantity": {"type": "number", "description": "持仓数量/股份余额"},
                            "available_quantity": {"type": "number", "description": "可用/可卖数量"},
                            "cost_price": {"type": "number", "description": "成本价/摊薄成本价"},
                            "latest_price": {"type": "number", "description": "现价/最新价"},
                            "market_value": {"type": "number", "description": "市值"},
                            "floating_pnl": {"type": "number", "description": "浮动盈亏"},
                            "floating_pnl_pct": {"type": "number", "description": "盈亏比例，百分数，如 -12.5"},
                        },
                        "required": ["symbol", "position_quantity"],
                    },
                },
            },
            "required": ["rows"],
        },
    },
}

_EXTRACT_SYSTEM_PROMPT = (
    "你是券商持仓截图读取器。只读取用户这一张截图，然后调用 submit_position_screenshot 提交，不要回答文字。"
    "持仓明细每个完整可见的标的提交一行，被截断、看不全的行不要提交。"
    "账户资金区（总资产、可用资金、持仓市值、盈亏）看得到就填进 funds。"
    "截图上能看到券商名、App 名或账号就填进 account，账号按原样抄写。"
    "数值只填纯数字，不带逗号、元、股、%。看不清的字段留空，不要猜。"
    "截图没显示股票代码是正常情况：symbol 和 name 都填截图上的证券名称，交给系统查代码，严禁凭记忆猜代码。"
)

# 截图里的券商名 / App 名 → 券商预置 code。先匹配更长、更具体的名字（“中信建投”要先于“中信”）。
_BROKER_ALIASES: list[tuple[str, str]] = sorted(
    [
        *((item["name"], item["code"]) for item in BROKER_PRESETS if item["code"] != "manual"),
        ("小方", "fangzheng"),
        ("易淘金", "gf"),
        ("信e投", "citic"),
        ("信E投", "citic"),
        ("智远一户通", "cmschina"),
        ("涨乐", "huatai"),
        ("君弘", "gtja"),
        ("中国银河", "galaxy"),
        ("东方财富", "eastmoney"),
    ],
    key=lambda pair: len(pair[0]),
    reverse=True,
)

_NUMBER_FIELDS = (
    "position_quantity",
    "available_quantity",
    "cost_price",
    "latest_price",
    "market_value",
    "floating_pnl",
    "floating_pnl_pct",
)
_FUND_FIELDS = ("total_asset", "available_cash", "holding_market_value", "total_pnl", "day_pnl")
_CORRECTED_STATUSES = {"symbol_resolved_from_name", "symbol_corrected_from_name_conflict"}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "").replace("元", "").replace("股", "").replace("%", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def broker_code_from_text(text: str) -> str | None:
    """从截图上的券商名或 App 名认出券商；认不出返回 None。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return None
    for alias, code in _BROKER_ALIASES:
        if alias in compact:
            return code
    return None


def detect_account(accounts: list[dict[str, Any]], hints: list[dict[str, Any]]) -> tuple[int | None, str]:
    """按截图上的账号尾号、券商名判断是哪个账户。只有唯一匹配才返回，拿不准就返回 (None, "")。"""
    active = [item for item in accounts if (item.get("status") or "active") == "active"]
    for hint in hints:
        tail = _digits(hint.get("account_no_masked"))[-3:]
        if len(tail) < 3:
            continue
        matches = [item for item in active if _digits(item.get("account_no_masked")).endswith(tail)]
        if len(matches) == 1:
            return int(matches[0]["id"]), f"截图里的账号尾号 {tail}"
    text = " ".join(f"{hint.get('broker_name') or ''} {hint.get('app_name') or ''}" for hint in hints)
    code = broker_code_from_text(text)
    if code:
        preset_name = next((item["name"] for item in BROKER_PRESETS if item["code"] == code), "")
        matches = [
            item
            for item in active
            if item.get("broker_code") == code or (preset_name and preset_name in str(item.get("broker_name") or ""))
        ]
        if len(matches) == 1:
            return int(matches[0]["id"]), f"截图是{preset_name or matches[0].get('broker_name')}的界面"
    return None, ""


def _extract_one(image: dict[str, Any], model_config_id: int | None) -> dict[str, Any]:
    """识别一张截图，返回 submit_position_screenshot 的参数。"""
    from backend.app.agents.position_agent import _json_args
    from backend.app.ai.llm_client import chat_completion
    from backend.app.core.llm_config import active_llm_config, resolve_vision_model_config_id

    llm_config = active_llm_config(resolve_vision_model_config_id(model_config_id))
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前视觉模型未配置 API Key 或 Base URL。")
    model_name = str(llm_config.get("model") or "").strip().lower()
    body = chat_completion(
        [
            {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "读取这张券商截图里的账户、资金和持仓明细。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image['mime_type']};base64,{image['clean_base64']}"},
                    },
                ],
            },
        ],
        llm_config,
        tools=[SYNC_EXTRACT_TOOL],
        tool_choice="required",
        temperature=1.0 if model_name == "k3" or model_name.startswith("k3-") else 0.0,
        enable_thinking=False,
    )
    choices = body.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") == "submit_position_screenshot":
            arguments = _json_args(function.get("arguments"))
            arguments["_model"] = {
                "id": llm_config.get("id"),
                "display_name": llm_config.get("display_name"),
                "model_name": llm_config.get("model"),
            }
            return arguments
    raise RuntimeError("视觉模型没有返回识别结果，请换一张更清晰的截图。")


def merge_extractions(extractions: list[dict[str, Any]]) -> dict[str, Any]:
    """把多张截图的识别结果合并：同一只股票只留一行，资金取第一张有值的。"""
    from backend.app.agents.position_agent import validate_position_rows

    merged: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    funds: dict[str, float] = {}
    hints: list[dict[str, Any]] = []
    as_of_date: str | None = None
    for image_index, extraction in enumerate(extractions):
        account = extraction.get("account") if isinstance(extraction.get("account"), dict) else {}
        if any(account.get(key) for key in ("broker_name", "app_name", "account_no_masked")):
            hints.append(account)
        raw_funds = extraction.get("funds") if isinstance(extraction.get("funds"), dict) else {}
        for field in _FUND_FIELDS:
            value = _number(raw_funds.get(field))
            if value is not None and field not in funds:
                funds[field] = value
        if not as_of_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(extraction.get("as_of_date") or "")):
            as_of_date = str(extraction["as_of_date"])
        source_rows = [row for row in extraction.get("rows") or [] if isinstance(row, dict)]
        if not source_rows:
            continue
        rows, validations = validate_position_rows(source_rows, prefer_name_on_conflict=True)
        for row, validation in zip(rows, validations):
            quantity = _number(row.get("position_quantity"))
            label = str(row.get("name") or row.get("symbol") or "").strip()
            if quantity is None or quantity < 0:
                warnings.append(f"“{label or '一行持仓'}”没读出持仓数量，已跳过。")
                continue
            valid = bool(validation.get("valid"))
            symbol = str(row.get("symbol") or "").strip()
            key = symbol if valid else f"name:{label}"
            item = {
                "key": key,
                "symbol": symbol if valid else str(validation.get("input_symbol") or symbol),
                "name": row.get("name") or label,
                "valid": valid,
                "validation_status": validation.get("status"),
                "validation_message": validation.get("message"),
                "corrected": validation.get("status") in _CORRECTED_STATUSES,
                "suggested_symbol": validation.get("suggested_symbol"),
                "image_index": image_index,
            }
            for field in _NUMBER_FIELDS:
                item[field] = _number(row.get(field))
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                continue
            if abs((existing["position_quantity"] or 0) - quantity) >= 1:
                warnings.append(
                    f"{item['name']}在两张截图里数量不一样"
                    f"（{existing['position_quantity']:g} 股 / {quantity:g} 股），已按第一张；不对的话请核对后再同步。"
                )
            for field in _NUMBER_FIELDS:
                if existing.get(field) is None and item.get(field) is not None:
                    existing[field] = item[field]
    return {
        "rows": list(merged.values()),
        "funds": funds,
        "account_hints": hints,
        "account_no_masked": next(
            (str(hint["account_no_masked"]).strip() for hint in hints if _digits(hint.get("account_no_masked"))),
            None,
        ),
        "as_of_date": as_of_date,
        "warnings": warnings,
    }


def _decode_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not images:
        raise ValueError("请至少选择一张截图。")
    if len(images) > MAX_SYNC_IMAGES:
        raise ValueError(f"一次最多同步 {MAX_SYNC_IMAGES} 张截图。")
    decoded: list[dict[str, Any]] = []
    for index, image in enumerate(images):
        mime_type = str(image.get("mime_type") or "image/png")
        if not mime_type.lower().startswith("image/"):
            raise ValueError(f"第 {index + 1} 个文件不是图片。")
        raw, clean_base64, sha256 = decode_image_payload(str(image.get("image_base64") or ""))
        if not raw:
            raise ValueError(f"第 {index + 1} 张截图是空的，请重新选择。")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"第 {index + 1} 张截图超过 8MB，请压缩后再传。")
        decoded.append(
            {
                "index": index,
                "name": str(image.get("name") or f"截图 {index + 1}")[:200],
                "mime_type": mime_type,
                "clean_base64": clean_base64,
                "sha256": sha256,
            }
        )
    return decoded


def recognize_sync(
    *,
    repository: StockRepository,
    images: list[dict[str, Any]],
    account_id: int | None,
    model_config_id: int | None,
) -> dict[str, Any]:
    """识别多张截图并合并；能确定账户时顺带给出变化清单。不写库。"""
    decoded = _decode_images(images)
    results: list[dict[str, Any] | Exception] = []
    with ThreadPoolExecutor(max_workers=min(4, len(decoded))) as pool:
        futures = [pool.submit(_extract_one, image, model_config_id) for image in decoded]
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - 单张失败不影响其他截图
                results.append(exc)
    image_reports: list[dict[str, Any]] = []
    extractions: list[dict[str, Any]] = []
    model: dict[str, Any] = {}
    for image, result in zip(decoded, results):
        if isinstance(result, Exception):
            image_reports.append({"index": image["index"], "name": image["name"], "ok": False, "row_count": 0, "error": str(result)[:200]})
            continue
        model = model or dict(result.get("_model") or {})
        row_count = len([row for row in result.get("rows") or [] if isinstance(row, dict)])
        image_reports.append({"index": image["index"], "name": image["name"], "ok": True, "row_count": row_count, "error": None})
        extractions.append(result)
    if not extractions:
        first_error = next((item["error"] for item in image_reports if item["error"]), "")
        raise RuntimeError(f"截图都没识别成功：{first_error}" if first_error else "截图都没识别成功，请换更清晰的截图。")

    merged = merge_extractions(extractions)
    if not merged["rows"] and not merged["funds"]:
        raise ValueError("截图里没有读到持仓明细，也没有读到账户资金。请确认截的是持仓页面。")
    accounts = repository.list_position_accounts()
    detected_id, detected_reason = detect_account(accounts, merged["account_hints"])
    target_id = account_id or detected_id
    preview = (
        repository.position_sync_preview(account_id=target_id, rows=merged["rows"], funds=merged["funds"])
        if target_id
        else None
    )
    return {
        **merged,
        "detected_account_id": detected_id,
        "detected_reason": detected_reason,
        "image_sha256": decoded[0]["sha256"],
        "images": image_reports,
        "model": model,
        "preview": preview,
    }


def validate_apply_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确认同步前再校验一遍：要写入的行代码必须有效、数量合理，买卖记录要有股数和价格。"""
    from backend.app.agents.position_agent import validate_position_symbol

    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("apply"):
            continue
        kind = str(item.get("kind") or "")
        if kind not in {"new", "changed", "missing"}:
            continue
        symbol = str(item.get("symbol") or "").strip()
        after = item.get("after") if isinstance(item.get("after"), dict) else {}
        name = str(after.get("name") or item.get("name") or "").strip()
        validation = validate_position_symbol(symbol, None)
        if not validation.get("valid"):
            raise ValueError(f"“{name or symbol}”的股票代码 {symbol or '（空）'} 没通过校验，请先改正或取消勾选这一行。")
        normalized = str(validation["normalized_symbol"])
        row: dict[str, Any] = {"kind": kind, "symbol": normalized, "name": name or validation.get("canonical_name") or normalized}
        if kind in {"new", "changed"}:
            quantity = _number(after.get("position_quantity"))
            if quantity is None or quantity <= 0:
                raise ValueError(f"“{row['name']}”的持仓数量不对，请核对。")
            row["after"] = {field: _number(after.get(field)) for field in _NUMBER_FIELDS}
        trade = item.get("trade") if isinstance(item.get("trade"), dict) else None
        if trade and trade.get("enabled"):
            action = str(trade.get("action") or "")
            quantity = _number(trade.get("quantity"))
            price = _number(trade.get("price"))
            if action not in {"buy", "sell"} or not quantity or quantity <= 0:
                raise ValueError(f"“{row['name']}”要记的成交方向或股数不对。")
            if not price or price <= 0:
                raise ValueError(f"“{row['name']}”要记成交，请填成交价。")
            row["trade"] = {"action": action, "quantity": quantity, "price": price}
        cleaned.append(row)
    return cleaned


def apply_sync(
    *,
    repository: StockRepository,
    account_id: int,
    items: list[dict[str, Any]],
    funds: dict[str, Any] | None,
    apply_funds: bool,
    as_of_date: str | None,
    image_sha256: str | None,
    account_no_masked: str | None,
    created_by: str | None,
) -> dict[str, Any]:
    cleaned = validate_apply_items(items)
    clean_funds = {field: _number((funds or {}).get(field)) for field in _FUND_FIELDS}
    use_funds = bool(apply_funds and clean_funds.get("available_cash") is not None)
    if not cleaned and not use_funds:
        raise ValueError("没有要同步的内容。")
    if as_of_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of_date):
        raise ValueError("持仓日期格式应为 YYYY-MM-DD。")
    # 快照日期一律用今天：同步是“以用户此刻确认的截图为准”。若沿用截图上较旧的日期，
    # _save_position_holding 会把它当成过期数据忽略，同步就静默失效了。截图日期只记进批次。
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return repository.apply_position_sync(
        account_id=account_id,
        items=cleaned,
        funds=clean_funds if use_funds else None,
        as_of_date=max(today, as_of_date or today),
        screenshot_date=as_of_date,
        image_sha256=image_sha256,
        account_no_masked=(account_no_masked or "").strip()[:80] or None,
        created_by=created_by,
    )
