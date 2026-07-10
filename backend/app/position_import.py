from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import httpx

from .stock_agent import _active_llm_config


KEY_MAP = {
    "代码": "symbol",
    "证券代码": "symbol",
    "股票代码": "symbol",
    "code": "symbol",
    "stock_code": "symbol",
    "ts_code": "symbol",
    "名称": "name",
    "证券名称": "name",
    "股票名称": "name",
    "stock_name": "name",
    "股份余额": "position_quantity",
    "持仓数量": "position_quantity",
    "持仓": "position_quantity",
    "股票余额": "position_quantity",
    "shares": "position_quantity",
    "quantity": "position_quantity",
    "可用数量": "available_quantity",
    "可用股份": "available_quantity",
    "可卖数量": "available_quantity",
    "available": "available_quantity",
    "冻结数量": "frozen_quantity",
    "frozen": "frozen_quantity",
    "今买": "today_buy_quantity",
    "今日买入": "today_buy_quantity",
    "今卖": "today_sell_quantity",
    "今日卖出": "today_sell_quantity",
    "成本价": "cost_price",
    "持仓成本": "cost_price",
    "成本金额": "cost_amount",
    "当前价": "latest_price",
    "最新价": "latest_price",
    "现价": "latest_price",
    "市值": "market_value",
    "证券市值": "market_value",
    "持仓市值": "market_value",
    "浮动盈亏": "floating_pnl",
    "参考盈亏": "floating_pnl",
    "盈亏": "floating_pnl",
    "profit_loss": "floating_pnl",
    "盈亏比例": "floating_pnl_pct",
    "盈亏率": "floating_pnl_pct",
    "profit_loss_pct": "floating_pnl_pct",
    "当日盈亏": "day_pnl",
    "仓位": "position_pct",
    "仓位占比": "position_pct",
}


def decode_image_payload(image_base64: str) -> tuple[bytes, str, str]:
    raw = (image_base64 or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    raw = re.sub(r"\s+", "", raw)
    if not raw:
        raise ValueError("图片内容为空")
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("图片 base64 解码失败") from exc
    if len(image_bytes) > 8 * 1024 * 1024:
        raise ValueError("图片过大，请控制在 8MB 以内")
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    return image_bytes, raw, image_hash


def parse_position_screenshot(
    *,
    image_base64: str,
    mime_type: str = "image/png",
    instruction: str | None = None,
    model_config_id: int | None = None,
) -> dict[str, Any]:
    return parse_position_input(
        instruction=instruction or "请识别截图中的 A 股持仓明细并保存为结构化持仓。",
        image_base64=image_base64,
        mime_type=mime_type,
        model_config_id=model_config_id,
    )


def parse_position_text(
    *,
    instruction: str,
    model_config_id: int | None = None,
) -> dict[str, Any]:
    return parse_position_input(instruction=instruction, model_config_id=model_config_id)


def parse_position_input(
    *,
    instruction: str,
    image_base64: str | None = None,
    mime_type: str = "image/png",
    model_config_id: int | None = None,
) -> dict[str, Any]:
    clean_base64 = None
    image_hash = None
    if image_base64:
        _, clean_base64, image_hash = decode_image_payload(image_base64)
    llm_config = _active_llm_config(model_config_id)
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在设置中配置可用模型。")

    schema_example = """
{
  "intent": "greeting / add_position / clarify / unsupported",
  "should_save": true,
  "reply": "给用户看的中文回复。没有识别到持仓时，要正常寒暄或追问需要的字段；识别到持仓时，简短说明准备保存几条。",
  "account": {
    "broker_name": "券商或交易软件名称，未知填 ai_ocr",
    "account_name": "账户名称，可空",
    "account_no_masked": "脱敏账号，可空",
    "account_type": "cash / credit / simulation"
  },
  "as_of_date": "截图日期，未知可空，格式 YYYY-MM-DD",
  "rows": [
    {
      "symbol": "A股代码，优先输出 600519.SH / 000001.SZ / 430xxx.BJ，只有6位代码也可以",
      "name": "证券名称",
      "position_quantity": 1000,
      "available_quantity": 1000,
      "frozen_quantity": 0,
      "today_buy_quantity": 0,
      "today_sell_quantity": 0,
      "cost_price": 10.23,
      "cost_amount": 10230,
      "latest_price": 10.88,
      "market_value": 10880,
      "floating_pnl": 650,
      "floating_pnl_pct": 6.35,
      "day_pnl": 120,
      "position_pct": 8.2,
      "confidence": 0.92
    }
  ]
}
""".strip()
    prompt = f"""你是 SixRabbits Stock 的持仓录入 Agent。你不是普通 OCR 工具，而是一个对话式持仓录入助手。

用户输入：
{instruction.strip()}

你必须只输出 JSON，不要输出解释文字。JSON 格式如下：
{schema_example}

工作规则：
1. 如果用户只是问候、测试、闲聊，例如“你好”“在吗”“你是谁”，intent=greeting，should_save=false，rows=[]，reply 正常回应并提示可以输入持仓描述或上传截图。
2. 如果用户想添加持仓但字段不足，例如只说“帮我添加贵州茅台”，intent=clarify，should_save=false，rows=[]，reply 追问代码/数量/成本价等关键信息。
3. 只有当能识别出至少一条持仓明细时，intent=add_position，should_save=true，并填充 rows。
4. 数值字段输出纯数字，不要带逗号、元、股、%。
5. 不确定的字段填 null，不要猜。
6. 图片场景只识别持仓明细行，不要把资产汇总、按钮、广告识别成持仓。"""

    user_content: str | list[dict[str, Any]]
    if clean_base64:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{clean_base64}"}},
        ]
    else:
        user_content = prompt

    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": "你是严谨的 A 股持仓录入结构化助手，只能返回可解析 JSON。"},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {llm_config['api_key']}"}
    url = llm_config["base_url"].rstrip("/") + "/chat/completions"
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        body = response.text[:600]
        raise RuntimeError(f"视觉模型接口返回 {response.status_code}: {body}")

    body = response.json()
    content = _extract_message_content(body)
    parsed = _extract_json(content)
    account = _normalize_account(parsed.get("account") if isinstance(parsed, dict) else None)
    rows = _extract_rows(parsed)
    normalized_rows = [_normalize_row(row, account, parsed) for row in rows if isinstance(row, dict)]
    should_save = _boolish(parsed.get("should_save")) if isinstance(parsed, dict) else bool(normalized_rows)
    intent = str(parsed.get("intent") or ("add_position" if normalized_rows else "clarify")) if isinstance(parsed, dict) else "clarify"
    reply = (
        str(parsed.get("reply") or "").strip()
        if isinstance(parsed, dict)
        else ""
    )
    if not reply:
        reply = "我可以帮你录入 A 股持仓。你可以直接输入代码、名称、数量、成本价，也可以上传券商持仓截图。"
    return {
        "model": {
            "id": llm_config.get("id"),
            "display_name": llm_config.get("display_name"),
            "model_name": llm_config.get("model"),
            "provider_name": llm_config.get("provider_name"),
        },
        "image_sha256": image_hash,
        "raw_text": content,
        "intent": intent,
        "should_save": should_save,
        "reply": reply,
        "account": account,
        "rows": normalized_rows,
        "parsed": parsed,
    }


def _extract_message_content(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("模型没有返回 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _extract_json(content: str) -> dict[str, Any] | list[Any]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        try:
            return json.loads(text[object_start : object_end + 1])
        except json.JSONDecodeError:
            pass
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        return json.loads(text[array_start : array_end + 1])
    raise RuntimeError("模型返回内容不是可解析 JSON")


def _extract_rows(parsed: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    for key in ("rows", "positions", "holdings", "data", "items"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    return []


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "保存", "save"}


def _normalize_account(account: Any) -> dict[str, Any]:
    if not isinstance(account, dict):
        return {"broker_name": "ai_ocr", "account_type": "cash"}
    return {
        "broker_name": account.get("broker_name") or account.get("broker") or "ai_ocr",
        "account_name": account.get("account_name"),
        "account_no_masked": account.get("account_no_masked") or account.get("account_no"),
        "account_type": account.get("account_type") or "cash",
    }


def _normalize_row(row: dict[str, Any], account: dict[str, Any], parsed: dict[str, Any] | list[Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        mapped_key = KEY_MAP.get(str(key).strip(), str(key).strip())
        normalized[mapped_key] = value
    if isinstance(parsed, dict) and parsed.get("as_of_date") and not normalized.get("as_of_date"):
        normalized["as_of_date"] = parsed.get("as_of_date")
    for key, value in account.items():
        normalized.setdefault(key, value)
    return normalized
