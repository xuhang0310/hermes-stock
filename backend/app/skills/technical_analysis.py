"""技能：技术分析 —— 日线分析、趋势、均线、量能、筹码、资金流、形态。

Phase 2：registry 接管分发；roles + prompt_instructions 补齐。
"""
from backend.app.market.stock_tools import (
    MAX_DAILY_ROWS,
    STOCK_TOOL_SCHEMAS,
    _int_arg,
    _optional_str,
    analyze_daily_bars,
    analyze_pattern,
    calculate_ma,
    get_capital_flow,
    get_chip_distribution,
    get_volume_analysis,
)

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in STOCK_TOOL_SCHEMAS}


def _pick(*names: str) -> list[dict]:
    """从 STOCK_TOOL_SCHEMAS 按工具名取出对应 schema，保持单一来源不漂移。"""
    return [_SCHEMA_BY_NAME[n] for n in names]


SKILL = {
    "name": "technical_analysis",
    "display_name": "技术分析",
    "description": "日线分析、趋势判断、均线、量能、筹码分布、资金流向、形态识别",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    "roles": ["quant-analyst", "short-term-hunter"],
    "tools": _pick(
        "analyze_daily_bars",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "get_chip_distribution",
        "get_capital_flow",
        "analyze_pattern",
    ),
    "executors": {
        "analyze_daily_bars": lambda a: analyze_daily_bars(
            str(a.get("symbol") or ""),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        # analyze_trend 是 analyze_daily_bars 的语义别名，参数抽取一致
        "analyze_trend": lambda a: analyze_daily_bars(
            str(a.get("symbol") or ""),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "calculate_ma": lambda a: calculate_ma(
            str(a.get("symbol") or ""),
            periods=_optional_str(a.get("periods")),
            limit=_int_arg(a.get("limit"), 160, MAX_DAILY_ROWS),
        ),
        "get_volume_analysis": lambda a: get_volume_analysis(
            str(a.get("symbol") or ""),
            days=_int_arg(a.get("days"), 30, 120),
        ),
        "get_chip_distribution": lambda a: get_chip_distribution(str(a.get("symbol") or "")),
        "get_capital_flow": lambda a: get_capital_flow(
            str(a.get("symbol") or ""),
            days=_int_arg(a.get("days"), 10, 60),
        ),
        "analyze_pattern": lambda a: analyze_pattern(
            str(a.get("symbol") or ""),
            days=_int_arg(a.get("days"), 60, 120),
        ),
    },
    # 留空：量化分析师角色技能（ROLE_SKILLS）+ 基础问股技术分析路径已覆盖，避免二次注入。
    "prompt_instructions": [],
}
