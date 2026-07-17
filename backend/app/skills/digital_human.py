"""技能：数字人工具 —— 组合/自选/证据/风控/研报/情报/量化/宏观/短线等数字人专用工具。

Phase 3：roles 按 ROLE_TOOL_NAMES 逐角色补齐；prompt_instructions 从 ROLE_SKILLS 迁移。
注意：role 对应的专属工具绑定在 skills/digital_human.py 的 roles 里统一注册，
具体枚举见 digital_human_tools.ROLE_TOOL_NAMES。
"""
from backend.app.market.digital_human_tools import (
    DIGITAL_HUMAN_TOOL_SCHEMAS,
    _int_arg,
    _optional_str,
    assess_macro_market_backdrop,
    build_daily_brief,
    check_stop_loss_status,
    compare_universe_quant_factors,
    detect_market_anomalies,
    generate_counter_arguments,
    get_hotspot_momentum,
    get_intelligence_digest,
    get_macro_policy_digest,
    get_my_universe,
    get_portfolio_snapshot,
    get_priority_items,
    get_recent_alerts,
    get_recent_internal_evidence,
    get_research_gaps,
    get_risk_context,
    get_symbol_context,
    get_symbol_documents,
    get_symbol_reports,
    get_watchlist_snapshot,
    rank_intelligence_sources,
    run_quant_signal_snapshot,
    scan_short_term_opportunities,
)

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in DIGITAL_HUMAN_TOOL_SCHEMAS}


def _pick(*names: str) -> list[dict]:
    """从 DIGITAL_HUMAN_TOOL_SCHEMAS 按工具名取出对应 schema，保持单一来源不漂移。"""
    return [_SCHEMA_BY_NAME[n] for n in names]


SKILL = {
    "name": "digital_human",
    "display_name": "数字人工具",
    "description": "数字人专用：组合/自选/证据/风控/研报/情报/量化/宏观/短线工具集",
    "version": "1.0.0",
    "chat_modes": ["research"],
    "roles": [],  # 空=所有 role 可见；per-role 工具由 digital_human_tool_schemas/digital_human_tool_names 在组装时筛选
    "tools": _pick(
        "get_my_universe",
        "get_portfolio_snapshot",
        "get_watchlist_snapshot",
        "get_symbol_context",
        "get_recent_internal_evidence",
        "get_priority_items",
        "build_daily_brief",
        "check_stop_loss_status",
        "get_risk_context",
        "generate_counter_arguments",
        "get_recent_alerts",
        "detect_market_anomalies",
        "get_symbol_reports",
        "get_symbol_documents",
        "get_research_gaps",
        "get_intelligence_digest",
        "rank_intelligence_sources",
        "run_quant_signal_snapshot",
        "compare_universe_quant_factors",
        "get_macro_policy_digest",
        "assess_macro_market_backdrop",
        "scan_short_term_opportunities",
        "get_hotspot_momentum",
    ),
    # 参数抽取与 execute_digital_human_tool 同名分支逐字一致
    "executors": {
        "get_my_universe": lambda a: get_my_universe(),
        "get_portfolio_snapshot": lambda a: get_portfolio_snapshot(),
        "get_watchlist_snapshot": lambda a: get_watchlist_snapshot(),
        "get_symbol_context": lambda a: get_symbol_context(str(a.get("symbol") or "")),
        "get_recent_internal_evidence": lambda a: get_recent_internal_evidence(
            symbol=_optional_str(a.get("symbol")),
            limit=_int_arg(a.get("limit"), 12, 30),
        ),
        "get_priority_items": lambda a: get_priority_items(),
        "build_daily_brief": lambda a: build_daily_brief(),
        "check_stop_loss_status": lambda a: check_stop_loss_status(symbol=_optional_str(a.get("symbol"))),
        "get_risk_context": lambda a: get_risk_context(symbol=_optional_str(a.get("symbol"))),
        "generate_counter_arguments": lambda a: generate_counter_arguments(symbol=_optional_str(a.get("symbol"))),
        "get_recent_alerts": lambda a: get_recent_alerts(
            symbol=_optional_str(a.get("symbol")),
            level=_optional_str(a.get("level")),
            limit=_int_arg(a.get("limit"), 10, 30),
        ),
        "detect_market_anomalies": lambda a: detect_market_anomalies(symbol=_optional_str(a.get("symbol"))),
        "get_symbol_reports": lambda a: get_symbol_reports(
            str(a.get("symbol") or ""),
            limit=_int_arg(a.get("limit"), 8, 20),
        ),
        "get_symbol_documents": lambda a: get_symbol_documents(
            str(a.get("symbol") or ""),
            limit=_int_arg(a.get("limit"), 8, 20),
        ),
        "get_research_gaps": lambda a: get_research_gaps(symbol=_optional_str(a.get("symbol"))),
        "get_intelligence_digest": lambda a: get_intelligence_digest(
            query=_optional_str(a.get("query")),
            symbol=_optional_str(a.get("symbol")),
            limit=_int_arg(a.get("limit"), 8, 20),
        ),
        "rank_intelligence_sources": lambda a: rank_intelligence_sources(
            query=_optional_str(a.get("query")),
            symbol=_optional_str(a.get("symbol")),
            limit=_int_arg(a.get("limit"), 8, 20),
        ),
        "run_quant_signal_snapshot": lambda a: run_quant_signal_snapshot(str(a.get("symbol") or "")),
        "compare_universe_quant_factors": lambda a: compare_universe_quant_factors(
            limit=_int_arg(a.get("limit"), 20, 50),
        ),
        "get_macro_policy_digest": lambda a: get_macro_policy_digest(
            query=_optional_str(a.get("query")),
            limit=_int_arg(a.get("limit"), 8, 20),
        ),
        "assess_macro_market_backdrop": lambda a: assess_macro_market_backdrop(),
        "scan_short_term_opportunities": lambda a: scan_short_term_opportunities(
            limit=_int_arg(a.get("limit"), 10, 30),
        ),
        "get_hotspot_momentum": lambda a: get_hotspot_momentum(
            query=_optional_str(a.get("query")),
            symbol=_optional_str(a.get("symbol")),
        ),
    },
    # prompt_instructions 留空：这些规则已在基础系统提示词（_system_prompt 的工作规则 +
    # digital_human_skill_prompt 的角色技能）逐条覆盖，若在此重复声明会经 registry 通道二次注入。
    # registry 通道（get_prompt_instructions）现只承载基础提示词未覆盖的新技能规则（如工作区）。
    "prompt_instructions": [],
}
