"""量化选股（L1–L7）技能：给数字人查大盘环境、分层得分、排雷结果和单票体检。

只查信息，不定计划：除模型排名外，分层得分、“动手/观察/放弃”和大盘仓位上限都没有经过历史回测。
操作计划里的买价、止损和股数以 plans/quant_orders、plans/quant_sizes 算的为准，这里的买价和止损下限
用的是同一套代码，股数对新开仓只是参考。
"""
from __future__ import annotations

from backend.app.quant import expectation_gap, layered_selection, market_regime

QUANT_SELECTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_regime_l1",
            "description": (
                "读取大盘环境：全市场站上 20/60 日均线的比例、两市成交额分位、沪深300/中证1000 趋势，"
                "给出进攻/中性/防守和大小盘风格。其中的总仓位上限（90%/60%/30%）还没有经过回测，只作参考。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {"type": "string", "description": "可选交易日 YYYYMMDD，缺省取最新截面"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_unified_quant_selection",
            "description": (
                "查量化选股（L1–L7）的分层得分。支持波段(swing)、长线(long)、事件驱动(event)三档，"
                "返回排雷结果、L2–L6 分层得分、动手/观察/放弃、买入模型的挂买价和新开仓参考股数。"
                "除模型排名外，分层得分和结论都没有经过回测，只作参考。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_type": {
                        "type": "string",
                        "enum": ["swing", "long", "event"],
                        "default": "swing",
                        "description": "选股持有周期档位：swing(波段5-15日) / long(长线30-120日) / event(事件3-10日)",
                    },
                    "verdict_filter": {
                        "type": "string",
                        "enum": ["action", "watch", "pass"],
                        "description": "可选结论过滤：action(动手≥70且无否决) / watch(观察55-69) / pass(放弃)",
                    },
                    "industry": {"type": "string", "description": "可选行业精确筛选（如 半导体、银行）"},
                    "boards": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["MAIN", "CHINEXT", "STAR", "BSE"]},
                        "description": "账户可交易板块过滤（主板MAIN / 创业板CHINEXT / 科创板STAR / 北交所BSE）",
                    },
                    "price_max": {"type": "number", "description": "股价上限（元），适配小资金买不起高价整手"},
                    "pe_max": {"type": "number", "description": "正市盈率 PE(TTM) 上限"},
                    "total_mv_min": {"type": "number", "description": "总市值下限（亿元）"},
                    "total_mv_max": {"type": "number", "description": "总市值上限（亿元）"},
                    "capital": {"type": "number", "default": 200000, "description": "账户总资金预算（元），用于估算新开仓参考股数"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_stock_layered_l1_l7",
            "description": (
                "单只 A 股的 L1–L7 体检：波段/长线/事件三档得分、排雷结果、按披露日取的财报增速变化、"
                "预期差线索、买入模型的挂买价、止损下限和新开仓参考股数。分层得分没有经过回测，只作参考。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码，如 600519.SH 或 000001.SZ"},
                    "capital": {"type": "number", "default": 200000, "description": "账户总资金预算（元）"},
                    "with_ai_thesis": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否实时触发 L4 预期差三段论核验",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
]


def _exec_inspect_stock(args: dict) -> dict:
    symbol = str(args.get("symbol") or "")
    detail = layered_selection.inspect_single_stock_layered(
        symbol,
        capital=float(args.get("capital") or 200_000.0),
    )
    if args.get("with_ai_thesis"):
        detail["ai_expectation_gap"] = expectation_gap.generate_expectation_gap_thesis(
            symbol,
            trade_date=detail.get("trade_date"),
            persist=True,
        )
    return detail


SKILL = {
    "name": "quant_selection",
    "display_name": "量化选股（L1–L7）",
    "description": "查大盘环境、分层得分、排雷结果和单票体检；除模型排名外的分层打分没有经过回测，只作参考。",
    "version": "1.0.0",
    "chat_modes": ["research", "stock"],
    "roles": ["quant-analyst", "research-analyst", "stock-pm", "risk-officer"],
    "tools": QUANT_SELECTION_TOOL_SCHEMAS,
    "executors": {
        "get_market_regime_l1": lambda a: market_regime.get_latest_market_regime(
            trade_date=a.get("trade_date"),
            compute_if_missing=True,
        ),
        "query_unified_quant_selection": lambda a: layered_selection.query_layered_selection(
            plan_type=str(a.get("plan_type") or "swing"),
            verdict_filter=a.get("verdict_filter"),
            industry=a.get("industry"),
            boards=a.get("boards"),
            price_max=a.get("price_max"),
            pe_max=a.get("pe_max"),
            total_mv_min=a.get("total_mv_min"),
            total_mv_max=a.get("total_mv_max"),
            capital=float(a.get("capital") or 200_000.0),
            limit=int(a.get("limit") or 15),
        ),
        "inspect_stock_layered_l1_l7": _exec_inspect_stock,
    },
    "prompt_instructions": [
        "量化选股（L1–L7）的工具只用来查信息。除模型排名外，分层得分、动手/观察/放弃和大盘仓位上限都没有经过历史回测，引用时说明只作参考，不能当成买入的理由。",
        "命中立案调查、退市风险、财务造假或 ST 的股票，可以直接说不建议买。",
        "写操作计划时，买价、止损和股数以计划提示里系统算好的为准；和这里查到的不一致时，听计划提示的。",
    ],
}
