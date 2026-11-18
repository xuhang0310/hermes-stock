"""全站统一 L1–L7 分层量化选股技能（Unified Quantitative Selection Skill）。

让数字人彻底告别“拍脑袋追涨分”，统一使用经 A 股实证校准的 L1 市场环境温度计、
L2–L6 六层客观评分 + 一票否决门禁、量化限价买点与 L7 账户风险股数引擎。
"""
from __future__ import annotations

from backend.app.quant import expectation_gap, layered_selection, market_regime

QUANT_SELECTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_regime_l1",
            "description": (
                "读取 A 股 L1 市场环境温度计：全市场站上 20/60 日均线宽度占比、两市成交分位、"
                "沪深300/中证1000 趋势，输出今日建议总仓位上限（进攻90%/中性60%/防守30%）与大小盘风格象限。"
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
                "检索全站统一 L1–L7 分层量化选股池。支持波段(swing)、长线(long)、事件驱动(event)三档策略，"
                "返回一票否决排雷后的候选标的、L2–L6 分层得分、动手/观察/放弃结论、量化建议挂买价及 L7 建议股数。"
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
                    "capital": {"type": "number", "default": 200000, "description": "账户总资金预算（元），用于 L7 风险股数测算"},
                    "risk_per_trade": {"type": "number", "default": 0.01, "description": "单笔最大可承受风险比例（如 0.01 代表 1%）"},
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
                "对单只 A 股执行完整 L1–L7 分层深度体检：同时给出波段/长线/事件三档得分、一票否决排雷结果、"
                "PIT 财报增速加速度、L4 预期差三段论、量化限价挂买位与 L7 账户最大风险股数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码，如 600519.SH 或 000001.SZ"},
                    "capital": {"type": "number", "default": 200000, "description": "账户总资金预算（元）"},
                    "risk_per_trade": {"type": "number", "default": 0.01, "description": "单笔最大亏损比例"},
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
        risk_per_trade=float(args.get("risk_per_trade") or 0.01),
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
    "display_name": "统一分层量化选股",
    "description": "基于 A 股实证校准的 L1–L7 分层量化选股、单票六层深度体检、限价买点与 L7 风险预算股数测算。",
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
            risk_per_trade=float(a.get("risk_per_trade") or 0.01),
            limit=int(a.get("limit") or 15),
        ),
        "inspect_stock_layered_l1_l7": _exec_inspect_stock,
    },
    "prompt_instructions": [
        "选股或评估买点时必须优先调用 get_market_regime_l1 明确今日大盘总仓位上限（90%/60%/30%），大盘防守期严禁建议满仓进攻。",
        "严禁使用短期放量暴涨追高逻辑推荐股票；必须以 query_unified_quant_selection 或 inspect_stock_layered_l1_l7 的 L2–L6 分层证据与一票否决（vetoed）为准。",
        "触发一票否决（vetoed=True，如立案调查、退市警示、ST、波段深跌破位）的标的，无论其他分项多高，结论一律为「放弃」，绝不推荐买入。",
        "给出操作计划草案时必须同时说明执行层建议限价（suggested_buy_price）、证伪参考价（stop_ref_price）与 L7 风险预算股数，且明确声明仅供研究不自动下单。",
    ],
}
