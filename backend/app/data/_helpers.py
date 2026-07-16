"""StockRepository 共用的自由函数与常量（按域 mixin 拆分后共享）。"""
from __future__ import annotations

import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.core.models import Position, SystemLog

CHINA_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_RESOLUTIONS = {"1": "1min", "5": "5min", "15": "15min", "30": "30min", "60": "60min"}


def _account_eq(account_id: int | None):
    return Position.account_id == account_id if account_id is not None else Position.account_id.is_(None)


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def dt(value: Any) -> str | None:
    return value.isoformat() if value else None


def normalize_a_share_symbol(symbol: str) -> str:
    value = (symbol or "").strip().upper()
    if not value:
        raise ValueError("请输入标的代码")
    value = value.replace("_", ".")
    prefixed = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", value)
    if prefixed:
        return f"{prefixed.group(2)}.{prefixed.group(1)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value
    pure = re.fullmatch(r"\d{6}", value)
    if pure:
        if value.startswith(("6", "9")):
            return f"{value}.SH"
        if value.startswith(("4", "8")):
            return f"{value}.BJ"
        return f"{value}.SZ"
    return value


def exchange_from_symbol(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "SSE"
    if symbol.endswith(".SZ"):
        return "SZSE"
    if symbol.endswith(".BJ"):
        return "BSE"
    return "A-share"


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text_value = str(value).strip()
    if text_value in {"-", "--", "—", "无", "null", "None"}:
        return None
    multiplier = 1.0
    text_value = text_value.replace(",", "").replace("，", "").replace(" ", "")
    if text_value.endswith("万"):
        multiplier = 10000.0
        text_value = text_value[:-1]
    elif text_value.endswith("亿"):
        multiplier = 100000000.0
        text_value = text_value[:-1]
    text_value = text_value.replace("%", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text_value)
    return float(match.group(0)) * multiplier if match else None


def system_log_out(row: SystemLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "level": row.level,
        "module": row.module,
        "message": row.message,
        "context": parse_json(row.context_json),
        "created_at": dt(row.created_at),
    }
