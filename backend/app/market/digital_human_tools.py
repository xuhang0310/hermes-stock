from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from backend.app.core.database import engine
from backend.app.market.stock_tools import STOCK_TOOL_SCHEMAS, execute_stock_tool, normalize_a_share_symbol, stock_observation_preview, stock_tool_display_name


COMMON_TOOL_NAMES = [
    "get_my_universe",
    "get_portfolio_snapshot",
    "get_watchlist_snapshot",
    "get_symbol_context",
    "get_recent_internal_evidence",
    # 联网搜索：每个数字人都具备，用于补充系统内没有的外部信息（个股新闻、行业、政策、宏观等）
    "search_market_intel",
    "search_stock_news",
]

ROLE_TOOL_NAMES: dict[str, list[str]] = {
    "stock-pm": [
        "get_priority_items",
        "build_daily_brief",
    ],
    "risk-officer": [
        "check_stop_loss_status",
        "get_risk_context",
        "generate_counter_arguments",
    ],
    "market-sentinel": [
        "get_recent_alerts",
        "detect_market_anomalies",
        "get_realtime_quote",
    ],
    "research-analyst": [
        "get_symbol_reports",
        "get_symbol_documents",
        "get_research_gaps",
        "get_daily_history",
        "analyze_trend",
    ],
    "intelligence-officer": [
        "get_intelligence_digest",
        "rank_intelligence_sources",
    ],
    "quant-analyst": [
        "run_quant_signal_snapshot",
        "compare_universe_quant_factors",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "get_chip_distribution",
        "get_capital_flow",
        "analyze_pattern",
    ],
    "macro-strategist": [
        "get_macro_policy_digest",
        "assess_macro_market_backdrop",
    ],
    "short-term-hunter": [
        "scan_short_term_opportunities",
        "get_hotspot_momentum",
        "get_realtime_quote",
        "get_volume_analysis",
        "analyze_pattern",
    ],
}

DEFAULT_ROLE_CODE = "stock-pm"

ROLE_SKILLS: dict[str, dict[str, Any]] = {
    "stock-pm": {
        "name": "A股研究总调度",
        "skills": [
            "任务拆解: 先判断问题属于持仓、自选、单票研究、风险还是日报，再选择最少工具。",
            "优先级排序: 优先处理持仓，其次自选池；有止损、L0/L1事件、缺研究证据的标的优先。",
            "统稿输出: 把事实、推断、风险、下一步动作分开，不替用户自动下单。",
        ],
        "output": "输出固定包含: 今日重点、持仓观察、自选池观察、风险/缺口、下一步动作。",
    },
    "risk-officer": {
        "name": "风控复盘官",
        "skills": [
            "仓位约束: 优先检查 open positions 的仓位、成本、止损、风险说明。",
            "止损检查: 对接近或跌破止损的标的给出硬约束提醒。",
            "反方论证: 每个偏多观点必须给出反证、触发条件和撤销条件。",
        ],
        "output": "输出固定包含: 风险等级、触发事实、反方观点、需要验证的数据、处理建议。",
    },
    "market-sentinel": {
        "name": "盘中哨兵",
        "skills": [
            "异常识别: 只关注持仓/自选池里的事件、异动、重复噪声和是否需要通知。",
            "短句报告: 输出短、快、明确，不写长篇宏观解释。",
            "通知判断: 区分立即处理、继续观察、可忽略噪声。",
        ],
        "output": "输出固定包含: 异常标的、异常原因、级别、证据、是否通知。",
    },
    "research-analyst": {
        "name": "个股研究员",
        "skills": [
            "证据链整理: 围绕持仓/自选池的单个标的聚合持仓、自选理由、K线、报告、文档和事件。",
            "研究缺口: 明确哪些数据缺失，不能用想象补齐。",
            "个股备忘录: 输出结构化研究备忘录，区分事实、推断、风险和后续验证。",
        ],
        "output": "输出固定包含: 标的概况、已有证据、技术/事件/报告摘要、研究缺口、观察计划。",
    },
    "intelligence-officer": {
        "name": "情报官",
        "skills": [
            "消息过滤: 先区分内部事件、公告/新闻、政策/行业叙事和未经验证的外部文本。",
            "可信度分层: 对每条情报标注来源、时效、影响方向和是否需要进一步核验。",
            "一遍过滤: 面向持仓和自选池输出真正需要行动或转交其他 Agent 的信息。",
        ],
        "output": "输出固定包含: 情报摘要、来源可信度、影响标的、风险/机会方向、需要转交的 Agent、核验动作。",
    },
    "quant-analyst": {
        "name": "量化分析师",
        "skills": [
            "指标归因: 用均线、量能、形态、资金流、筹码和本地K线做确定性统计，不靠主观描述代替数据。",
            "横向比较: 默认先在持仓和自选池内比较相对强弱、放量、回撤和信号一致性。",
            "边界说明: 数据不足、TuShare权限不足或本地缓存不足时明确说明缺口。",
        ],
        "output": "输出固定包含: 样本范围、核心指标表、信号强弱、异常项、数据缺口、只做研究不做交易指令。",
    },
    "macro-strategist": {
        "name": "宏观策略师",
        "skills": [
            "宏观映射: 把政策、利率、外盘、行业轮动和指数环境映射到当前持仓/自选池。",
            "假设管理: 区分事实、政策文本、市场预期和个人推断，避免把情绪当结论。",
            "情境推演: 给出基准/乐观/压力三种情景下的观察重点。",
        ],
        "output": "输出固定包含: 宏观背景、影响链条、受影响标的、情景推演、验证指标、风险提示。",
    },
    "short-term-hunter": {
        "name": "短线猎手",
        "skills": [
            "短线扫描: 聚焦涨跌幅、量能突变、近期事件、热点叙事和情绪阶段。",
            "纪律优先: 必须提示仓位上限、止损和追高冷静期，不输出无条件买卖。",
            "热点验证: 区分板块共振、个股孤立异动和纯消息噪音。",
        ],
        "output": "输出固定包含: 候选标的、触发信号、情绪/热点依据、风险位、冷静期、后续观察条件。",
    },
}

TOOL_DISPLAY_NAMES = {
    "get_my_universe": "获取持仓与自选范围",
    "get_portfolio_snapshot": "获取持仓快照",
    "get_watchlist_snapshot": "获取自选池快照",
    "get_symbol_context": "获取标的综合上下文",
    "get_recent_internal_evidence": "获取内部证据",
    "get_priority_items": "生成优先级事项",
    "build_daily_brief": "生成研究台日报",
    "check_stop_loss_status": "检查止损状态",
    "get_risk_context": "获取风控上下文",
    "generate_counter_arguments": "生成反方论证",
    "get_recent_alerts": "获取近期告警",
    "detect_market_anomalies": "检测市场异常",
    "get_symbol_reports": "获取标的报告",
    "get_symbol_documents": "获取标的文档",
    "get_research_gaps": "识别研究缺口",
}


TOOL_DISPLAY_NAMES.update(
    {
        "get_intelligence_digest": "生成情报摘要",
        "rank_intelligence_sources": "评估情报可信度",
        "run_quant_signal_snapshot": "生成量化信号快照",
        "compare_universe_quant_factors": "比较自选持仓量化指标",
        "get_macro_policy_digest": "生成宏观政策摘要",
        "assess_macro_market_backdrop": "评估宏观市场背景",
        "scan_short_term_opportunities": "扫描短线候选",
        "get_hotspot_momentum": "分析热点动量",
    }
)


def digital_human_skill_prompt(profile_code: str | None) -> str:
    code = _role_code(profile_code)
    role = ROLE_SKILLS.get(code, ROLE_SKILLS[DEFAULT_ROLE_CODE])
    skills = "\n".join(f"- {item}" for item in role["skills"])
    return f"""当前数字人能力包: {role["name"]}

专属技能:
{skills}

输出契约:
- {role["output"]}

工具使用规则:
1. 研究本系统自己的持仓和自选池时，优先调用 get_my_universe 或对应快照工具，用真实数据说话。
2. run_sql / get_schema 是通用底座工具，可以兜底探索，但优先使用结构化专属工具。
3. 用户问系统外的标的、行业、政策、新闻、宏观或突发事件时，用 search_market_intel / search_stock_news 联网搜索补充；可以先说明它是否在系统持仓·自选范围内，再结合搜索结果和你的专业能力作答，绝不因为"不在持仓/自选池"就拒绝回答。
4. 遇到你本职专业之外、但用户确实需要的问题，也尽力帮忙（能联网就联网），做既专业又肯搭把手的伙伴，而不是只会查库的机器。
5. 结论要标注来源（内部数据 / 联网搜索 / 你的推断）；没有数据时如实说缺失，不要编造。"""


def digital_human_tool_schemas(profile_code: str | None) -> list[dict[str, Any]]:
    selected = set(COMMON_TOOL_NAMES + ROLE_TOOL_NAMES.get(_role_code(profile_code), ROLE_TOOL_NAMES[DEFAULT_ROLE_CODE]))
    schemas = [schema for schema in DIGITAL_HUMAN_TOOL_SCHEMAS if schema["function"]["name"] in selected]
    schemas.extend(_stock_schemas_for(selected))
    return schemas


def digital_human_tool_names(profile_code: str | None) -> set[str]:
    return {schema["function"]["name"] for schema in digital_human_tool_schemas(profile_code)}


def digital_human_tool_display_name(name: str) -> str:
    return TOOL_DISPLAY_NAMES.get(name) or stock_tool_display_name(name)


def execute_digital_human_tool(profile_code: str | None, name: str, args: dict[str, Any]) -> dict:
    """Phase 3：stock 工具委托 execute_stock_tool（Phase 2 已转发 registry），数字人工具
    转发 skill_registry 执行。原 23 分支 if/elif 已由 skills/digital_human.py 的 executor
    （调用同一批函数、同一套参数抽取）接管，结果逐字一致。profile_code 当前不参与执行
    （与原实现一致），仅保留签名以兼容调用方。

    工具组装与角色路由（ROLE_TOOL_NAMES + COMMON_TOOL_NAMES）未改，仍在
    digital_human_tool_schemas；run_sql / get_schema 基础工具不经此函数，沙箱不变。
    """
    if name in _stock_tool_names():
        return execute_stock_tool(name, args)
    from backend.app.skills.registry import skill_registry

    if skill_registry.has_tool(name):
        return skill_registry.execute(name, args)
    return {"error": f"未知数字人工具: {name}"}


def digital_human_observation_preview(name: str, result: dict) -> str:
    if name in _stock_tool_names():
        return stock_observation_preview(name, result)
    if "error" in result:
        return str(result["error"])
    if name == "get_my_universe":
        return f"持仓 {len(result.get('positions', []))} 个，自选 {len(result.get('watchlist', []))} 个，重点标的 {len(result.get('symbols', []))} 个。"
    if name == "get_portfolio_snapshot":
        return f"返回 {len(result.get('positions', []))} 个持仓，风险提示 {len(result.get('risk_flags', []))} 条。"
    if name == "get_watchlist_snapshot":
        return f"返回 {len(result.get('watchlist', []))} 个自选标的。"
    if name == "get_symbol_context":
        return f"已聚合 {result.get('symbol', '-')} 上下文：持仓 {bool(result.get('position'))}，自选 {bool(result.get('watchlist'))}。"
    if name == "get_recent_internal_evidence":
        return f"返回内部证据 {len(result.get('events', []))} 条事件、{len(result.get('reports', []))} 条报告、{len(result.get('risks', []))} 条风险记录。"
    if name == "get_priority_items":
        return f"生成优先事项 {len(result.get('items', []))} 条。"
    if name == "build_daily_brief":
        return f"生成日报素材：持仓 {result.get('counts', {}).get('positions', 0)}，自选 {result.get('counts', {}).get('watchlist', 0)}。"
    if name == "check_stop_loss_status":
        return f"检查 {len(result.get('items', []))} 个持仓止损状态。"
    if name == "get_risk_context":
        return f"返回风控上下文：风险记录 {len(result.get('risk_checks', []))} 条，报告 {len(result.get('reports', []))} 条。"
    if name == "generate_counter_arguments":
        return f"生成反方论证 {len(result.get('counter_arguments', []))} 条。"
    if name == "get_recent_alerts":
        return f"返回近期告警 {len(result.get('alerts', []))} 条。"
    if name == "detect_market_anomalies":
        return f"检测异常 {len(result.get('anomalies', []))} 条。"
    if name == "get_symbol_reports":
        return f"返回标的报告 {len(result.get('reports', []))} 条。"
    if name == "get_symbol_documents":
        return f"返回标的文档 {len(result.get('documents', []))} 条。"
    if name == "get_research_gaps":
        return f"识别研究缺口 {len(result.get('gaps', []))} 条。"
    if name == "get_intelligence_digest":
        return f"生成情报摘要：内部事件 {len(result.get('internal_events', []))} 条，外部搜索 {len(result.get('external_results', []))} 条。"
    if name == "rank_intelligence_sources":
        return f"完成情报可信度排序：高可信 {len(result.get('trusted', []))} 条，待核验 {len(result.get('unverified', []))} 条。"
    if name == "run_quant_signal_snapshot":
        return f"完成 {result.get('symbol', '-')} 量化信号快照。"
    if name == "compare_universe_quant_factors":
        return f"完成持仓/自选池量化横向比较：{len(result.get('items', []))} 个标的。"
    if name == "get_macro_policy_digest":
        return f"生成宏观政策摘要：本地文档 {len(result.get('local_documents', []))} 条，外部搜索 {len(result.get('external_results', []))} 条。"
    if name == "assess_macro_market_backdrop":
        return f"完成宏观市场背景评估：指数 {len(result.get('indices', []))} 个，持仓 {len(result.get('positions', []))} 个。"
    if name == "scan_short_term_opportunities":
        return f"完成短线扫描：候选 {len(result.get('candidates', []))} 个。"
    if name == "get_hotspot_momentum":
        return f"完成热点动量分析：外部搜索 {len(result.get('external_results', []))} 条。"
    return "数字人工具调用完成。"


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


DIGITAL_HUMAN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool("get_my_universe", "获取当前系统自己的持仓和自选池范围，作为数字人默认分析边界。", {}),
    _tool("get_portfolio_snapshot", "获取 open positions 持仓快照，包含仓位、成本、止损和风险提示。", {}),
    _tool("get_watchlist_snapshot", "获取 active watchlist_items 自选池快照，包含入池理由和基础信息。", {}),
    _tool(
        "get_symbol_context",
        "按单个标的聚合持仓、自选、K线、事件、报告、风险和文档上下文。",
        {"symbol": {"type": "string", "description": "A股代码或 ts_code，例如 600519.SH。"}},
        ["symbol"],
    ),
    _tool(
        "get_recent_internal_evidence",
        "获取系统内近期事件、报告和风险记录，可按标的过滤。",
        {
            "symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"},
            "limit": {"type": "integer", "description": "每类最多返回条数，默认 12。"},
        },
    ),
    _tool("get_priority_items", "总调度专用：根据持仓、自选、事件、风险和研究缺口生成优先处理事项。", {}),
    _tool("build_daily_brief", "总调度专用：生成研究台日报素材，覆盖持仓、自选、风险和研究缺口。", {}),
    _tool(
        "check_stop_loss_status",
        "风控官专用：检查持仓是否接近或跌破止损。",
        {"symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"}},
    ),
    _tool(
        "get_risk_context",
        "风控官专用：获取持仓风险记录、相关报告和反证素材。",
        {"symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"}},
    ),
    _tool(
        "generate_counter_arguments",
        "风控官专用：基于持仓理由、风险记录和报告生成反方论证素材。",
        {"symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"}},
    ),
    _tool(
        "get_recent_alerts",
        "盘中哨兵专用：获取近期事件/告警，可按标的和级别过滤。",
        {
            "symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"},
            "level": {"type": "string", "description": "可选事件级别，例如 L0/L1/L2。"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 10。"},
        },
    ),
    _tool(
        "detect_market_anomalies",
        "盘中哨兵专用：围绕持仓/自选池识别近期事件、行情缓存和风险异常。",
        {"symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"}},
    ),
    _tool(
        "get_symbol_reports",
        "研究员专用：获取某标的历史 Agent 报告。",
        {
            "symbol": {"type": "string", "description": "A股代码或 ts_code。"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 8。"},
        },
        ["symbol"],
    ),
    _tool(
        "get_symbol_documents",
        "研究员专用：获取某标的关联文档和证据。",
        {
            "symbol": {"type": "string", "description": "A股代码或 ts_code。"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 8。"},
        },
        ["symbol"],
    ),
    _tool(
        "get_research_gaps",
        "研究员专用：识别持仓/自选池或单个标的的研究缺口。",
        {"symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"}},
    ),
    _tool(
        "get_intelligence_digest",
        "情报官专用：聚合内部事件/报告/风险、历史文档和 Tavily 外部搜索，生成情报摘要。",
        {
            "query": {"type": "string", "description": "可选搜索主题，例如 政策、公告、行业催化。"},
            "symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"},
            "limit": {"type": "integer", "description": "每类最多返回条数，默认 8。"},
        },
    ),
    _tool(
        "rank_intelligence_sources",
        "情报官专用：把内部证据、文档和外部搜索结果按可信度排序，并标记待核验信息。",
        {
            "query": {"type": "string", "description": "可选搜索主题。"},
            "symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"},
            "limit": {"type": "integer", "description": "每类最多返回条数，默认 8。"},
        },
    ),
    _tool(
        "run_quant_signal_snapshot",
        "量化分析师专用：围绕单个标的生成均线、量能、形态、资金流、筹码和技术趋势快照。",
        {"symbol": {"type": "string", "description": "A股代码或 ts_code。"}},
        ["symbol"],
    ),
    _tool(
        "compare_universe_quant_factors",
        "量化分析师专用：基于本地K线缓存横向比较当前持仓和自选池的短期收益、量能和异常信号。",
        {"limit": {"type": "integer", "description": "最多比较多少个标的，默认 20。"}},
    ),
    _tool(
        "get_macro_policy_digest",
        "宏观策略师专用：搜索并聚合宏观、政策、行业轮动和外盘相关信息。",
        {
            "query": {"type": "string", "description": "可选宏观主题，例如 利率、汇率、地产政策、AI产业政策。"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 8。"},
        },
    ),
    _tool(
        "assess_macro_market_backdrop",
        "宏观策略师专用：读取指数、本地事件和持仓自选池，评估当前市场背景。",
        {},
    ),
    _tool(
        "scan_short_term_opportunities",
        "短线猎手专用：基于持仓/自选池的涨跌幅、放量、事件和风险约束扫描短线候选。",
        {"limit": {"type": "integer", "description": "最多返回候选数量，默认 10。"}},
    ),
    _tool(
        "get_hotspot_momentum",
        "短线猎手专用：围绕热点主题或单个标的获取情绪、量能、形态和外部热点搜索结果。",
        {
            "query": {"type": "string", "description": "热点主题，例如 AI算力、机器人、低空经济。"},
            "symbol": {"type": "string", "description": "可选 A股代码或 ts_code。"},
        },
    ),
]


def get_my_universe() -> dict:
    positions = get_portfolio_snapshot().get("positions", [])
    watchlist = get_watchlist_snapshot().get("watchlist", [])
    by_symbol: dict[str, dict[str, Any]] = {}
    for item in positions:
        row = by_symbol.setdefault(item["symbol"], {"symbol": item["symbol"], "name": item.get("name")})
        row.update(
            {
                "in_position": True,
                "position_pct": item.get("position_pct"),
                "quantity": item.get("quantity"),
                "market_value": item.get("market_value"),
                "floating_pnl": item.get("floating_pnl"),
                "floating_pnl_pct": item.get("floating_pnl_pct"),
                "stop_price": item.get("stop_price"),
                "position_status": item.get("status"),
            }
        )
    for item in watchlist:
        row = by_symbol.setdefault(item["symbol"], {"symbol": item["symbol"], "name": item.get("name")})
        row.update(
            {
                "in_watchlist": True,
                "watch_reason": item.get("reason"),
                "watch_group": item.get("group_name"),
                "watch_status": item.get("status"),
            }
        )
    for row in by_symbol.values():
        row.setdefault("in_position", False)
        row.setdefault("in_watchlist", False)
    symbols = sorted(by_symbol.values(), key=lambda row: (not row["in_position"], row["symbol"]))
    return {"source": "positions+watchlist_items", "positions": positions, "watchlist": watchlist, "symbols": symbols}


def get_portfolio_snapshot() -> dict:
    rows = _query(
        """
        SELECT p.id,
               p.symbol,
               COALESCE(s.symbol_name, i.name) AS name,
               COALESCE(s.position_pct, p.position_pct) AS position_pct,
               COALESCE(s.cost_price, p.cost_price) AS cost_price,
               COALESCE(s.position_quantity, p.quantity) AS quantity,
               s.available_quantity,
               s.frozen_quantity,
               s.latest_price,
               s.market_value,
               s.floating_pnl,
               s.floating_pnl_pct,
               s.day_pnl,
               COALESCE(s.stop_price, p.stop_price) AS stop_price,
               COALESCE(s.take_profit_price, p.take_profit_price) AS take_profit_price,
               COALESCE(s.thesis, p.thesis) AS thesis,
               COALESCE(s.risk_note, p.risk_note) AS risk_note,
               p.status,
               p.opened_at,
               COALESCE(s.captured_at, p.updated_at) AS updated_at,
               s.as_of_date,
               s.source_type,
               a.broker_name,
               a.account_name
        FROM positions p
        JOIN instruments i ON i.symbol = p.symbol
        LEFT JOIN (
          SELECT symbol, MAX(id) AS snapshot_id
          FROM position_holding_snapshots
          GROUP BY symbol
        ) latest ON latest.symbol = p.symbol
        LEFT JOIN position_holding_snapshots s ON s.id = latest.snapshot_id
        LEFT JOIN position_accounts a ON a.id = s.account_id
        WHERE p.status = 'open'
        ORDER BY COALESCE(s.position_pct, p.position_pct) DESC, COALESCE(s.captured_at, p.updated_at) DESC
        LIMIT 100
        """
    )
    risk_flags = []
    for row in rows:
        if row.get("stop_price") is None:
            risk_flags.append({"symbol": row["symbol"], "level": "medium", "reason": "持仓缺少止损价"})
        if not row.get("thesis"):
            risk_flags.append({"symbol": row["symbol"], "level": "low", "reason": "持仓缺少投资逻辑记录"})
    return {"source": "positions", "positions": rows, "risk_flags": risk_flags}


def get_watchlist_snapshot() -> dict:
    rows = _query(
        """
        SELECT w.id, w.symbol, i.name, g.name AS group_name, w.reason, w.status, w.added_at, w.updated_at
        FROM watchlist_items w
        JOIN instruments i ON i.symbol = w.symbol
        JOIN watchlist_groups g ON g.id = w.group_id
        WHERE w.status = 'active'
        ORDER BY w.updated_at DESC
        LIMIT 100
        """
    )
    return {"source": "watchlist_items", "watchlist": rows}


def get_symbol_context(symbol: str) -> dict:
    ts_code = _normalize_symbol(symbol)
    if not ts_code:
        return {"error": f"无法识别标的: {symbol}"}
    instrument = _one(
        "SELECT symbol, name, market, type, status, list_date FROM instruments WHERE symbol = :symbol",
        {"symbol": ts_code},
    )
    position = _one(
        """
        SELECT p.id,
               p.symbol,
               COALESCE(s.position_pct, p.position_pct) AS position_pct,
               COALESCE(s.cost_price, p.cost_price) AS cost_price,
               COALESCE(s.position_quantity, p.quantity) AS quantity,
               s.available_quantity,
               s.frozen_quantity,
               s.latest_price,
               s.market_value,
               s.floating_pnl,
               s.floating_pnl_pct,
               s.day_pnl,
               COALESCE(s.stop_price, p.stop_price) AS stop_price,
               COALESCE(s.take_profit_price, p.take_profit_price) AS take_profit_price,
               COALESCE(s.thesis, p.thesis) AS thesis,
               COALESCE(s.risk_note, p.risk_note) AS risk_note,
               p.status,
               p.opened_at,
               COALESCE(s.captured_at, p.updated_at) AS updated_at,
               s.as_of_date,
               s.source_type,
               a.broker_name,
               a.account_name
        FROM positions p
        LEFT JOIN (
          SELECT symbol, MAX(id) AS snapshot_id
          FROM position_holding_snapshots
          GROUP BY symbol
        ) latest ON latest.symbol = p.symbol
        LEFT JOIN position_holding_snapshots s ON s.id = latest.snapshot_id
        LEFT JOIN position_accounts a ON a.id = s.account_id
        WHERE p.symbol = :symbol
        ORDER BY COALESCE(s.captured_at, p.updated_at) DESC
        LIMIT 1
        """,
        {"symbol": ts_code},
    )
    watchlist = _query(
        """
        SELECT w.id, w.symbol, g.name AS group_name, w.reason, w.status, w.added_at, w.updated_at
        FROM watchlist_items w JOIN watchlist_groups g ON g.id = w.group_id
        WHERE w.symbol = :symbol ORDER BY w.updated_at DESC LIMIT 5
        """,
        {"symbol": ts_code},
    )
    bars = _query(
        """
        SELECT trade_date, open, high, low, close, pre_close, change_pct, volume, amount, source
        FROM daily_bars WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 20
        """,
        {"symbol": ts_code},
    )
    evidence = get_recent_internal_evidence(symbol=ts_code, limit=8)
    return {
        "source": "instruments+positions+watchlist_items+daily_bars+events+agent_reports+risk_checks+documents",
        "symbol": ts_code,
        "instrument": instrument,
        "position": position,
        "watchlist": watchlist,
        "recent_bars": list(reversed(bars)),
        "evidence": evidence,
        "in_user_universe": bool(position and position.get("status") == "open") or any(row.get("status") == "active" for row in watchlist),
    }


def get_recent_internal_evidence(symbol: str | None = None, limit: int = 12) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    params = {"limit": limit}
    symbol_filter = ""
    if ts_code:
        symbol_filter = "AND symbol = :symbol"
        params["symbol"] = ts_code
    events = _query(
        f"""
        SELECT event_id, level, event_type, symbol, title, summary, source_type, status, created_at
        FROM events WHERE 1=1 {symbol_filter}
        ORDER BY created_at DESC LIMIT :limit
        """,
        params,
    )
    reports = _query(
        f"""
        SELECT report_id, agent_name, symbol, title, conclusion, risk_points, action_suggestion, created_at
        FROM agent_reports WHERE 1=1 {symbol_filter}
        ORDER BY created_at DESC LIMIT :limit
        """,
        params,
    )
    risks = _query(
        f"""
        SELECT id, symbol, risk_level, risk_type, finding, required_action, cool_down_required, created_at
        FROM risk_checks WHERE 1=1 {symbol_filter}
        ORDER BY created_at DESC LIMIT :limit
        """,
        params,
    )
    return {"source": "events+agent_reports+risk_checks", "symbol": ts_code, "events": events, "reports": reports, "risks": risks}


def get_priority_items() -> dict:
    universe = get_my_universe()
    items: list[dict[str, Any]] = []
    stop_checks = check_stop_loss_status().get("items", [])
    for item in stop_checks:
        if item.get("status") != "normal":
            items.append({"priority": "high", "symbol": item["symbol"], "type": "stop_loss", "reason": item["message"]})
    alerts = get_recent_alerts(limit=10).get("alerts", [])
    for alert in alerts:
        if alert.get("level") in {"L0", "L1"}:
            items.append({"priority": "high" if alert.get("level") == "L0" else "medium", "symbol": alert.get("symbol"), "type": "alert", "reason": alert.get("title")})
    gaps = get_research_gaps().get("gaps", [])
    for gap in gaps[:8]:
        items.append({"priority": "medium", "symbol": gap.get("symbol"), "type": "research_gap", "reason": gap.get("reason")})
    return {"source": "portfolio+alerts+research_gaps", "universe_count": len(universe.get("symbols", [])), "items": items[:20]}


def build_daily_brief() -> dict:
    universe = get_my_universe()
    priority = get_priority_items()
    return {
        "source": "positions+watchlist_items+events+agent_reports+risk_checks",
        "date": date.today().isoformat(),
        "counts": {"positions": len(universe["positions"]), "watchlist": len(universe["watchlist"]), "symbols": len(universe["symbols"])},
        "priority_items": priority.get("items", []),
        "positions": universe["positions"],
        "watchlist": universe["watchlist"],
        "disclaimer": "仅供研究与复盘，不自动下单，不构成投资建议。",
    }


def check_stop_loss_status(symbol: str | None = None) -> dict:
    snapshot = get_portfolio_snapshot()
    ts_code = _normalize_symbol(symbol) if symbol else None
    items = []
    for row in snapshot.get("positions", []):
        if ts_code and row.get("symbol") != ts_code:
            continue
        latest = _latest_bar(row["symbol"])
        close = latest.get("close") if latest else None
        stop_price = row.get("stop_price")
        status = "unknown"
        distance_pct = None
        message = "缺少最新K线或止损价，无法判断"
        if close is not None and stop_price:
            distance_pct = round((float(close) / float(stop_price) - 1) * 100, 2)
            if distance_pct <= 0:
                status = "breached"
                message = f"收盘价 {close} 已跌破止损价 {stop_price}"
            elif distance_pct <= 3:
                status = "near"
                message = f"距止损价仅 {distance_pct}%"
            else:
                status = "normal"
                message = f"距止损价 {distance_pct}%"
        items.append({**row, "latest_close": close, "latest_trade_date": latest.get("trade_date") if latest else None, "distance_to_stop_pct": distance_pct, "status": status, "message": message})
    return {"source": "positions+daily_bars", "symbol": ts_code, "items": items}


def get_risk_context(symbol: str | None = None) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    evidence = get_recent_internal_evidence(symbol=ts_code, limit=20)
    stop_status = check_stop_loss_status(symbol=ts_code)
    return {"source": "risk_checks+positions+agent_reports+events", "symbol": ts_code, "stop_status": stop_status.get("items", []), "risk_checks": evidence.get("risks", []), "reports": evidence.get("reports", []), "events": evidence.get("events", [])}


def generate_counter_arguments(symbol: str | None = None) -> dict:
    context = get_risk_context(symbol)
    items = context.get("stop_status", [])
    counter_arguments = []
    for item in items:
        symbol_code = item.get("symbol")
        if item.get("risk_note"):
            counter_arguments.append({"symbol": symbol_code, "argument": f"已有风险说明: {item['risk_note']}", "verify": "检查最新事件、风险记录和价格是否继续支持持仓。"})
        if item.get("status") in {"near", "breached", "unknown"}:
            counter_arguments.append({"symbol": symbol_code, "argument": item.get("message"), "verify": "复核止损价、收盘价和持仓逻辑是否仍成立。"})
        if not item.get("thesis"):
            counter_arguments.append({"symbol": symbol_code, "argument": "持仓缺少明确 thesis，无法证明继续持有的逻辑。", "verify": "补充持仓理由或降低观察优先级。"})
    for risk in context.get("risk_checks", []):
        counter_arguments.append({"symbol": risk.get("symbol"), "argument": risk.get("finding"), "verify": risk.get("required_action") or "按风险记录要求复核。"})
    return {"source": "positions+risk_checks", "symbol": context.get("symbol"), "counter_arguments": counter_arguments[:20]}


def get_recent_alerts(symbol: str | None = None, level: str | None = None, limit: int = 10) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    params: dict[str, Any] = {"limit": limit}
    filters = []
    if ts_code:
        filters.append("symbol = :symbol")
        params["symbol"] = ts_code
    if level:
        filters.append("level = :level")
        params["level"] = level.strip().upper()
    where = " AND ".join(filters) if filters else "1=1"
    alerts = _query(
        f"""
        SELECT event_id, level, event_type, symbol, title, summary, source_type, status, created_at
        FROM events WHERE {where}
        ORDER BY CASE WHEN level='L0' THEN 0 WHEN level='L1' THEN 1 ELSE 2 END, created_at DESC
        LIMIT :limit
        """,
        params,
    )
    return {"source": "events", "symbol": ts_code, "level": level, "alerts": alerts}


def detect_market_anomalies(symbol: str | None = None) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    symbols = [ts_code] if ts_code else [row["symbol"] for row in get_my_universe().get("symbols", [])]
    anomalies = []
    for symbol_code in symbols:
        latest = _latest_bar(symbol_code)
        alerts = get_recent_alerts(symbol=symbol_code, limit=3).get("alerts", [])
        if latest and latest.get("change_pct") is not None and abs(float(latest["change_pct"])) >= 5:
            anomalies.append({"symbol": symbol_code, "type": "price_move", "level": "medium", "reason": f"最近K线涨跌幅 {latest['change_pct']}%"})
        for alert in alerts:
            if alert.get("level") in {"L0", "L1"}:
                anomalies.append({"symbol": symbol_code, "type": "alert", "level": alert.get("level"), "reason": alert.get("title")})
    return {"source": "daily_bars+events", "symbol": ts_code, "anomalies": anomalies[:30]}


def get_symbol_reports(symbol: str, limit: int = 8) -> dict:
    ts_code = _normalize_symbol(symbol)
    if not ts_code:
        return {"error": f"无法识别标的: {symbol}"}
    reports = _query(
        """
        SELECT report_id, agent_name, symbol, title, conclusion, reasoning, counter_arguments,
               risk_points, action_suggestion, created_at
        FROM agent_reports WHERE symbol = :symbol
        ORDER BY created_at DESC LIMIT :limit
        """,
        {"symbol": ts_code, "limit": limit},
    )
    return {"source": "agent_reports", "symbol": ts_code, "reports": reports}


def get_symbol_documents(symbol: str, limit: int = 8) -> dict:
    ts_code = _normalize_symbol(symbol)
    if not ts_code:
        return {"error": f"无法识别标的: {symbol}"}
    documents = _query(
        """
        SELECT d.id, d.doc_type, d.title, d.summary, d.source, d.source_url, d.published_at,
               d.trust_level, ds.relevance, ds.reason
        FROM documents d
        JOIN document_symbols ds ON ds.doc_id = d.id
        WHERE ds.symbol = :symbol
        ORDER BY COALESCE(d.published_at, d.fetched_at) DESC
        LIMIT :limit
        """,
        {"symbol": ts_code, "limit": limit},
    )
    return {"source": "documents+document_symbols", "symbol": ts_code, "documents": documents}


def get_research_gaps(symbol: str | None = None) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    symbols = [ts_code] if ts_code else [row["symbol"] for row in get_my_universe().get("symbols", [])]
    gaps = []
    for symbol_code in symbols:
        reports = get_symbol_reports(symbol_code, limit=1).get("reports", [])
        docs = get_symbol_documents(symbol_code, limit=1).get("documents", [])
        bars = _query("SELECT trade_date FROM daily_bars WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1", {"symbol": symbol_code})
        if not reports:
            gaps.append({"symbol": symbol_code, "reason": "缺少近期 Agent 研究报告"})
        if not docs:
            gaps.append({"symbol": symbol_code, "reason": "缺少关联文档/外部证据"})
        if not bars:
            gaps.append({"symbol": symbol_code, "reason": "缺少本地K线缓存"})
    return {"source": "agent_reports+documents+daily_bars", "symbol": ts_code, "gaps": gaps}


def get_intelligence_digest(query: str | None = None, symbol: str | None = None, limit: int = 8) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    topic = query or "A股 公告 新闻 政策 行业 研报 风险"
    if ts_code:
        topic = f"{ts_code} {topic}"
    evidence = get_recent_internal_evidence(symbol=ts_code, limit=limit)
    documents = get_symbol_documents(ts_code, limit=limit).get("documents", []) if ts_code else _recent_documents(limit)
    external = _safe_stock_tool(
        "search_market_intel",
        {"query": topic, "symbol": ts_code, "max_results": min(limit, 8)},
    )
    return {
        "source": "events+agent_reports+risk_checks+documents+tavily",
        "symbol": ts_code,
        "query": topic,
        "internal_events": evidence.get("events", []),
        "internal_reports": evidence.get("reports", []),
        "internal_risks": evidence.get("risks", []),
        "local_documents": documents,
        "external_results": external.get("results", []) if "error" not in external else [],
        "external_error": external.get("error"),
        "rule": "内部事件/报告/风险优先；外部搜索只作为待核验情报，不直接当作事实结论。",
    }


def rank_intelligence_sources(query: str | None = None, symbol: str | None = None, limit: int = 8) -> dict:
    digest = get_intelligence_digest(query=query, symbol=symbol, limit=limit)
    trusted: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for item in digest.get("internal_events", []):
        trusted.append({"type": "internal_event", "confidence": 0.9, "symbol": item.get("symbol"), "title": item.get("title"), "reason": "系统内部事件，优先级高于外部文本。"})
    for item in digest.get("internal_reports", []):
        trusted.append({"type": "agent_report", "confidence": 0.86, "symbol": item.get("symbol"), "title": item.get("title"), "reason": "内部 Agent 报告，可追溯到系统任务。"})
    for item in digest.get("internal_risks", []):
        trusted.append({"type": "risk_check", "confidence": 0.84, "symbol": item.get("symbol"), "title": item.get("finding"), "reason": "内部风控记录，需要优先核验处理。"})
    for item in digest.get("local_documents", []):
        trusted.append({"type": f"document:{item.get('source')}", "confidence": 0.74, "symbol": digest.get("symbol"), "title": item.get("title"), "url": item.get("source_url"), "reason": "系统已入库文档，但仍需核对原文和发布时间。"})
    for item in digest.get("external_results", []):
        unverified.append({"type": "external_search", "confidence": min(0.65, 0.35 + (_safe_float(item.get("score")) or 0) * 0.3), "title": item.get("title"), "url": item.get("url"), "reason": "Tavily 外部搜索结果，必须回到公告/新闻原文核验。"})
    return {
        "source": digest["source"],
        "symbol": digest.get("symbol"),
        "query": digest.get("query"),
        "trusted": sorted(trusted, key=lambda row: row.get("confidence", 0), reverse=True)[:limit],
        "unverified": sorted(unverified, key=lambda row: row.get("confidence", 0), reverse=True)[:limit],
        "external_error": digest.get("external_error"),
    }


def run_quant_signal_snapshot(symbol: str) -> dict:
    ts_code = _normalize_symbol(symbol)
    if not ts_code:
        return {"error": f"无法识别标的: {symbol}"}
    return {
        "source": "tushare+local_context",
        "symbol": ts_code,
        "local_context": _safe_stock_tool("get_analysis_context", {"symbol": ts_code, "limit": 30}),
        "trend": _safe_stock_tool("analyze_trend", {"symbol": ts_code}),
        "ma": _safe_stock_tool("calculate_ma", {"symbol": ts_code, "periods": "5,10,20,30,60,120", "limit": 160}),
        "volume": _safe_stock_tool("get_volume_analysis", {"symbol": ts_code, "days": 30}),
        "pattern": _safe_stock_tool("analyze_pattern", {"symbol": ts_code, "days": 60}),
        "capital_flow": _safe_stock_tool("get_capital_flow", {"symbol": ts_code, "days": 10}),
        "chip": _safe_stock_tool("get_chip_distribution", {"symbol": ts_code}),
        "universe_context": get_symbol_context(ts_code),
        "rule": "量化工具只给统计信号，不直接生成买卖指令。",
    }


def compare_universe_quant_factors(limit: int = 20) -> dict:
    universe = get_my_universe().get("symbols", [])[:limit]
    items: list[dict[str, Any]] = []
    for row in universe:
        symbol = row.get("symbol")
        if not symbol:
            continue
        bars = _local_recent_bars(symbol, 30)
        alerts = get_recent_alerts(symbol=symbol, limit=5).get("alerts", [])
        if len(bars) < 2:
            items.append({"symbol": symbol, "name": row.get("name"), "in_position": row.get("in_position"), "in_watchlist": row.get("in_watchlist"), "status": "missing_local_bars", "alert_count": len(alerts), "score": len(alerts) * 2})
            continue
        latest = bars[-1]
        pct_5d = _pct_change(bars, 5)
        pct_20d = _pct_change(bars, 20)
        volumes = [_safe_float(item.get("volume")) or 0 for item in bars]
        avg_volume_5 = _mean_number(volumes[-6:-1])
        latest_volume = _safe_float(latest.get("volume")) or 0
        volume_ratio_5d = latest_volume / avg_volume_5 if avg_volume_5 else None
        score = (pct_5d or 0) + ((volume_ratio_5d or 1) - 1) * 8 + _alert_score(alerts)
        items.append(
            {
                "symbol": symbol,
                "name": row.get("name"),
                "in_position": row.get("in_position"),
                "in_watchlist": row.get("in_watchlist"),
                "trade_date": latest.get("trade_date"),
                "close": latest.get("close"),
                "pct_5d": round(pct_5d, 2) if pct_5d is not None else None,
                "pct_20d": round(pct_20d, 2) if pct_20d is not None else None,
                "latest_change_pct": latest.get("change_pct"),
                "volume_ratio_5d": round(volume_ratio_5d, 2) if volume_ratio_5d is not None else None,
                "alert_count": len(alerts),
                "score": round(score, 2),
            }
        )
    return {"source": "positions+watchlist_items+local.daily_bars+events", "items": sorted(items, key=lambda item: item.get("score", -999), reverse=True), "note": "横向比较只使用本地已缓存K线；缺失时需要先由问股工具拉取历史K线。"}


def get_macro_policy_digest(query: str | None = None, limit: int = 8) -> dict:
    topic = query or "A股 宏观 政策 利率 汇率 外盘 行业轮动 风险偏好"
    external = _safe_stock_tool("search_market_intel", {"query": topic, "max_results": min(limit, 8)})
    events = _query(
        """
        SELECT event_id, level, event_type, symbol, title, summary, source_type, created_at
        FROM events
        WHERE title LIKE '%政策%' OR title LIKE '%宏观%' OR title LIKE '%行业%' OR event_type IN ('policy', 'macro', 'sector')
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return {"source": "documents+events+tavily", "query": topic, "local_documents": _recent_documents(limit), "policy_events": events, "external_results": external.get("results", []) if "error" not in external else [], "external_error": external.get("error"), "rule": "宏观结论必须落到当前持仓/自选池的影响链条，不能停留在泛泛叙事。"}


def assess_macro_market_backdrop() -> dict:
    indices = _query(
        """
        SELECT i.symbol, i.name, i.type, b.trade_date, b.close, b.change_pct, b.volume, b.amount
        FROM instruments i
        LEFT JOIN (
            SELECT symbol, MAX(trade_date) AS trade_date
            FROM daily_bars
            GROUP BY symbol
        ) latest ON latest.symbol = i.symbol
        LEFT JOIN daily_bars b ON b.symbol = latest.symbol AND b.trade_date = latest.trade_date
        WHERE i.type = 'index' OR i.symbol IN ('000300.SH', '399006.SZ', '000001.SH')
        ORDER BY i.symbol
        """
    )
    universe = get_my_universe()
    return {"source": "instruments+daily_bars+positions+watchlist_items+events", "indices": indices, "positions": universe.get("positions", []), "watchlist": universe.get("watchlist", []), "recent_alerts": get_recent_alerts(limit=12).get("alerts", []), "macro_sensitive_symbols": universe.get("symbols", []), "rule": "先看指数和风险偏好，再映射到持仓/自选池，不做脱离组合的宏观空谈。"}


def scan_short_term_opportunities(limit: int = 10) -> dict:
    candidates: list[dict[str, Any]] = []
    for row in get_my_universe().get("symbols", []):
        symbol = row.get("symbol")
        if not symbol:
            continue
        bars = _local_recent_bars(symbol, 15)
        alerts = get_recent_alerts(symbol=symbol, limit=5).get("alerts", [])
        latest = bars[-1] if bars else {}
        volumes = [_safe_float(item.get("volume")) or 0 for item in bars]
        avg_volume_5 = _mean_number(volumes[-6:-1])
        latest_volume = _safe_float(latest.get("volume")) or 0
        volume_ratio_5d = latest_volume / avg_volume_5 if avg_volume_5 else None
        latest_change = _safe_float(latest.get("change_pct")) or 0
        score = latest_change + ((volume_ratio_5d or 1) - 1) * 10 + _alert_score(alerts)
        if score <= 0 and not alerts:
            continue
        candidates.append({"symbol": symbol, "name": row.get("name"), "in_position": row.get("in_position"), "in_watchlist": row.get("in_watchlist"), "trade_date": latest.get("trade_date"), "latest_change_pct": latest.get("change_pct"), "volume_ratio_5d": round(volume_ratio_5d, 2) if volume_ratio_5d is not None else None, "alert_levels": [item.get("level") for item in alerts], "score": round(score, 2), "discipline": "若追高、涨幅过大或无止损计划，必须进入冷静观察，不输出直接买入。"})
    return {"source": "positions+watchlist_items+local.daily_bars+events", "candidates": sorted(candidates, key=lambda item: item.get("score", -999), reverse=True)[:limit], "note": "短线扫描只做候选排序，必须叠加仓位、止损和冷静期约束。"}


def get_hotspot_momentum(query: str | None = None, symbol: str | None = None) -> dict:
    ts_code = _normalize_symbol(symbol) if symbol else None
    topic = query or "A股 热点 题材 情绪 涨停 龙虎榜"
    if ts_code:
        topic = f"{ts_code} {topic}"
    external = _safe_stock_tool("search_market_intel", {"query": f"{topic} 短线 情绪 周期", "symbol": ts_code, "max_results": 6})
    payload: dict[str, Any] = {"source": "tavily+tushare+local", "symbol": ts_code, "query": topic, "external_results": external.get("results", []) if "error" not in external else [], "external_error": external.get("error")}
    if ts_code:
        payload.update({"quote": _safe_stock_tool("get_realtime_quote", {"symbol": ts_code}), "volume": _safe_stock_tool("get_volume_analysis", {"symbol": ts_code, "days": 30}), "pattern": _safe_stock_tool("analyze_pattern", {"symbol": ts_code, "days": 60}), "alerts": get_recent_alerts(symbol=ts_code, limit=5).get("alerts", [])})
    payload["rule"] = "热点必须验证是否有板块共振、量能配合和可执行的风险位。"
    return payload


def _role_code(profile_code: str | None) -> str:
    return profile_code if profile_code in ROLE_SKILLS else DEFAULT_ROLE_CODE


def _stock_tool_names() -> set[str]:
    return {schema["function"]["name"] for schema in STOCK_TOOL_SCHEMAS}


def _stock_schemas_for(selected: set[str]) -> list[dict[str, Any]]:
    return [schema for schema in STOCK_TOOL_SCHEMAS if schema["function"]["name"] in selected]


def _query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [{key: _serialize(value) for key, value in row.items()} for row in result.mappings()]


def _one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, bool):
        return bool(value)
    return value


def _normalize_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    value = normalize_a_share_symbol(str(symbol))
    return value if "." in value else value.upper()


def _latest_bar(symbol: str) -> dict[str, Any] | None:
    return _one(
        """
        SELECT trade_date, close, change_pct, volume, amount, source
        FROM daily_bars WHERE symbol = :symbol ORDER BY trade_date DESC LIMIT 1
        """,
        {"symbol": symbol},
    )


def _local_recent_bars(symbol: str, limit: int) -> list[dict[str, Any]]:
    rows = _query(
        """
        SELECT trade_date, close, change_pct, volume, amount, source
        FROM daily_bars
        WHERE symbol = :symbol
        ORDER BY trade_date DESC
        LIMIT :limit
        """,
        {"symbol": symbol, "limit": max(1, min(limit, 120))},
    )
    return list(reversed(rows))


def _recent_documents(limit: int) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT id, doc_type, title, summary, source, source_url, published_at,
               fetched_at, trust_level, is_external_untrusted
        FROM documents
        ORDER BY COALESCE(published_at, fetched_at) DESC
        LIMIT :limit
        """,
        {"limit": max(1, min(limit, 50))},
    )


def _safe_stock_tool(name: str, args: dict[str, Any]) -> dict:
    try:
        result = execute_stock_tool(name, args)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{name} 调用失败: {str(exc)[:300]}"}
    return result if isinstance(result, dict) else {"result": result}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean_number(values: list[float]) -> float | None:
    clean = [item for item in values if item is not None]
    return sum(clean) / len(clean) if clean else None


def _pct_change(bars: list[dict[str, Any]], days: int) -> float | None:
    if len(bars) < 2:
        return None
    latest = _safe_float(bars[-1].get("close"))
    base_index = max(0, len(bars) - 1 - days)
    base = _safe_float(bars[base_index].get("close"))
    if latest is None or not base:
        return None
    return (latest / base - 1) * 100


def _alert_score(alerts: list[dict[str, Any]]) -> float:
    score = 0.0
    for item in alerts:
        level = str(item.get("level") or "").upper()
        if level == "L0":
            score += 6
        elif level == "L1":
            score += 4
        else:
            score += 1
    return score


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_arg(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))
