"""Bounded market evidence tools; the calling agent composes the final analysis."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.market import industry_service, stock_tools


def normalized_symbol(symbol: str) -> str:
    value = stock_tools.normalize_a_share_symbol(symbol.strip())
    if not re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)", value):
        raise ValueError("请提供明确的 A 股代码，如 600036.SH；名称请先用 resolve_stock 查询。")
    return value


def resolve_stock(query: str) -> dict:
    query = query.strip()
    if not query or len(query) > 80:
        raise ValueError("股票名称或代码长度应为 1–80 字符。")
    return stock_tools.resolve_stock(query)


def get_stock_quote(symbol: str) -> dict:
    return stock_tools.get_latest_price(normalized_symbol(symbol))


def analyze_stock(symbol: str) -> dict:
    symbol = normalized_symbol(symbol)
    # Preserve each source's dates/errors instead of treating a partially available
    # research packet as a current, complete recommendation.
    quote = stock_tools.get_latest_price(symbol)
    technical = stock_tools.analyze_daily_bars(symbol)
    fundamentals = stock_tools.get_stock_info(symbol)
    errors = [
        {"dataset": key, "error": value["error"]}
        for key, value in (("quote", quote), ("technical", technical), ("fundamentals", fundamentals))
        if value.get("error")
    ]
    return {
        "symbol": symbol,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "quote": quote,
        "technical": technical,
        "fundamentals": fundamentals,
        "data_gaps": errors,
        "analysis_guidance": [
            "先注明行情日期、是否盘中数据和缺失数据，再区分技术面与基本面依据。",
            "支撑、压力与均线只是条件参考；列出看多/看空依据及失效条件，不承诺收益。",
            "询问是否卖出或继续持有时，调用 analyze_position 结合本人的成本、数量和风险线。",
        ],
    }


def analyze_market(industry_limit: int = 15) -> dict:
    if not 1 <= industry_limit <= 50:
        raise ValueError("industry_limit 应在 1–50 之间。")
    overview = dict(industry_service.load_snapshot()["overview"])
    industries = list(overview.pop("industries", []))
    ranked = sorted(
        (row for row in industries if row.get("avg_pct") is not None),
        key=lambda row: (-row["avg_pct"], row["industry"]),
    )
    return {
        **overview,
        "industry_count": len(industries),
        "leading_industries": ranked[:industry_limit],
        "lagging_industries": list(reversed(ranked[-industry_limit:])),
        "analysis_guidance": "这是同一交易日的全 A 收盘截面，并非实时指数；结合覆盖率、数据日期和 warnings 解释行业强弱。",
    }
