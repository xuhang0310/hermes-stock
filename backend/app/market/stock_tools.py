from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from backend.app.core.database import engine
from backend.app.core.settings import settings


TUSHARE_URL = "https://api.tushare.pro"
TAVILY_URL = "https://api.tavily.com/search"
MAX_DAILY_ROWS = 180
MAX_MINUTE_ROWS = 8000
_STOCK_BASIC_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": []}


STOCK_STRATEGIES: list[dict[str, str]] = [
    {
        "id": "bull_trend",
        "name": "默认多头趋势",
        "description": "优先检查 MA5/MA10/MA20 多头排列、乖离率、量能和回踩位置。",
        "instructions": "使用 analyze_daily_bars、calculate_ma、get_volume_analysis 判断趋势是否向上、是否追高、是否量价配合。核心约束：不追高，偏离 MA5 超过 5% 必须提示等待回踩。",
    },
    {
        "id": "ma_golden_cross",
        "name": "均线金叉",
        "description": "检查 MA5/MA10/MA20 金叉、MACD 金叉和量能确认。",
        "instructions": "重点寻找 MA5 上穿 MA10、MA10 上穿 MA20、MACD 金叉或零轴上金叉；必须结合成交量确认，量能不足时只能给观察结论。",
    },
    {
        "id": "volume_breakout",
        "name": "放量突破",
        "description": "关注放量突破 20 日高点、关键压力位和量价配合。",
        "instructions": "调用 get_volume_analysis 和 analyze_pattern，确认是否放量突破区间上沿或 20 日高点；若放量下跌或假突破，要明确降级。",
    },
    {
        "id": "hot_theme",
        "name": "热点题材",
        "description": "跟踪政策、产业和市场热点，判断题材强度、板块扩散和个股相对强弱。",
        "instructions": "调用 search_comprehensive_intel、search_stock_news、get_realtime_quote 和 analyze_trend，判断热点阶段是启动、扩散、分化还是退潮；区分实质受益和蹭概念，若高位消息密集或仅概念关联，要降低评分并提示兑现风险。",
    },
    {
        "id": "shrink_pullback",
        "name": "缩量回踩",
        "description": "寻找趋势内缩量回踩 MA5/MA10/MA20 后的低吸观察点。",
        "instructions": "先确认中期趋势未破坏，再判断回踩是否缩量、是否靠近 MA5/MA10 支撑；跌破 MA20 或放量下跌时不得给偏多结论。",
    },
    {
        "id": "box_oscillation",
        "name": "箱体震荡",
        "description": "识别近 10-20 日价格区间、箱体上沿/下沿和突破条件。",
        "instructions": "调用 analyze_pattern 识别箱体震荡，给出箱体上沿、下沿、突破确认和跌破止损；箱体内避免追涨杀跌。",
    },
    {
        "id": "growth_quality",
        "name": "成长质量",
        "description": "结合收入利润增长、ROE、现金流和行业空间，识别高质量成长股与成长失速风险。",
        "instructions": "调用 get_stock_info、search_stock_news、get_realtime_quote 和 analyze_trend，判断公司是高质量成长、成长验证中、成长放缓还是成长证伪；关注 ROE、现金流、估值承受力和行业景气，基本面未被价格确认时只给观察条件。",
    },
    {
        "id": "bottom_volume",
        "name": "底部放量",
        "description": "识别低位放量、双底、锤子线、早晨之星等底部反转信号。",
        "instructions": "调用 analyze_pattern 和 get_volume_analysis，重点检查底部形态、放量阳线、低位换手；若均线仍空头排列，要标注只是反弹观察。",
    },
    {
        "id": "expectation_repricing",
        "name": "预期重估",
        "description": "分析业绩预期、政策预期和估值预期的变化，寻找预期差修复或预期过热后的回落风险。",
        "instructions": "调用 search_stock_news、search_comprehensive_intel、get_stock_info、get_realtime_quote 和 analyze_trend，识别当前是正向预期差、预期兑现、负向预期差还是预期不明；区分公告财报订单等硬信息和传闻观点等软信息。",
    },
    {
        "id": "chan_theory",
        "name": "缠论",
        "description": "从中枢、趋势段、背驰、买卖点角度组织分析。",
        "instructions": "基于 get_tushare_daily、analyze_daily_bars 的高低点和 MACD，解释是否处于中枢震荡、离开段、背驰或一二三买卖点；如果日线数据不足，必须说明缠论级别不足。",
    },
    {
        "id": "wave_theory",
        "name": "波浪理论",
        "description": "用推动浪/调整浪/浪级位置描述当前结构。",
        "instructions": "基于近期高低点、涨跌幅和均线趋势，推断可能的推动浪或调整浪阶段；必须避免把单一浪型当成确定结论。",
    },
    {
        "id": "dragon_head",
        "name": "龙头策略",
        "description": "板块轮动中识别龙头股，适用于板块启动或行业催化出现时。",
        "instructions": "调用 search_comprehensive_intel、search_stock_news、get_realtime_quote 和 analyze_trend，检查板块催化、换手率、量比、相对强度和乖离率；若只是个股异动、没有板块共振，不能按龙头确认。",
    },
    {
        "id": "event_driven",
        "name": "事件驱动",
        "description": "把新闻、公告、业绩、政策变化与技术面进行交叉验证。",
        "instructions": "必须调用 search_stock_news，区分利好、利空、时效和可信度；事件与技术面冲突时以风险提示优先。",
    },
    {
        "id": "emotion_cycle",
        "name": "情绪周期",
        "description": "关注热点扩散、题材持续性、情绪退潮和拥挤度。",
        "instructions": "结合 search_stock_news 和量价变化判断是否处于启动、发酵、高潮、退潮；放量滞涨和高位消息密集时要提示情绪退潮风险。",
    },
    {
        "id": "one_yang_three_yin",
        "name": "一阳夹三阴",
        "description": "检测一阳夹三阴 K 线整理形态，作为趋势延续观察信号。",
        "instructions": "调用 get_daily_history 和 analyze_trend，检查最近 5 个交易日是否为大阳线、三根缩量阴线或小K线、再阳线突破；形态成立但趋势不明只能小幅加分，止损参考第1日开盘价下方。",
    },
]


STOCK_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_stock",
            "description": "Resolve an A-share stock code or company name to TuShare ts_code candidates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Stock code, ts_code, short name, or company name, for example 600519 or 贵州茅台.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tushare_daily",
            "description": "Fetch recent A-share daily OHLCV bars from TuShare and cache them into daily_bars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code, for example 600519.SH."},
                    "start_date": {"type": "string", "description": "Optional start date in YYYYMMDD."},
                    "end_date": {"type": "string", "description": "Optional end date in YYYYMMDD."},
                    "limit": {"type": "integer", "description": "Maximum rows to return, default 90, max 180."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_history",
            "description": "Alias of get_tushare_daily. Fetch recent A-share daily OHLCV history from TuShare.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "days": {"type": "integer", "description": "Trading days to return, default 90, max 180."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_realtime_quote",
            "description": "Get latest A-share quote snapshot from TuShare daily and daily_basic data, including price, change%, volume ratio, turnover, PE/PB and market cap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_daily_bars",
            "description": "Analyze TuShare daily bars: MA alignment, bias, MACD, RSI, volume, support/resistance, and signal score.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "start_date": {"type": "string", "description": "Optional start date in YYYYMMDD."},
                    "end_date": {"type": "string", "description": "Optional end date in YYYYMMDD."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_trend",
            "description": "DSA-compatible alias of analyze_daily_bars. Analyze technical trend, MA/MACD/RSI, bias, support/resistance and signal score.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "start_date": {"type": "string", "description": "Optional start date in YYYYMMDD."},
                    "end_date": {"type": "string", "description": "Optional end date in YYYYMMDD."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_ma",
            "description": "Calculate moving averages and price bias for custom periods such as MA5/10/20/60/120/250.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "periods": {"type": "string", "description": "Comma-separated periods, default 5,10,20,30,60,120,250."},
                    "limit": {"type": "integer", "description": "History rows to fetch, default 160, max 180."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_volume_analysis",
            "description": "Analyze recent volume-price relationship, volume ratios, expanding/shrinking volume, and divergence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "days": {"type": "integer", "description": "Recent trading days to analyze, default 30."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chip_distribution",
            "description": "Analyze chip/cost distribution from TuShare cyq_perf: winner rate, average cost, 5/15/50/85/95 percent cost bands and concentration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_capital_flow",
            "description": "Analyze recent main-force capital flow from TuShare moneyflow, including latest and 5/10 day net flow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "days": {"type": "integer", "description": "Recent trading days, default 10."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "Get A-share basic and fundamental context from TuShare: industry, market, list date, valuation, market cap and key financial indicators.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analysis_context",
            "description": "Read existing local analysis context for a stock from SixRabbits cached daily_bars, including latest cached bars and data coverage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "limit": {"type": "integer", "description": "Cached rows to return, default 20, max 60."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_pattern",
            "description": "Detect candlestick/chart patterns: doji, hammer, engulfing, double bottom, box, breakout, bottom volume.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code."},
                    "days": {"type": "integer", "description": "Recent trading days to scan, default 60."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_stock_news",
            "description": "Search recent market/news/catalyst information with Tavily Search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Chinese search query."},
                    "symbol": {"type": "string", "description": "Optional A-share code or ts_code."},
                    "max_results": {"type": "integer", "description": "Maximum results, default 5, max 8."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_market_intel",
            "description": "Search comprehensive market intelligence with Tavily: sector, policy, announcements, research narratives and risk events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Chinese search query."},
                    "symbol": {"type": "string", "description": "Optional A-share code or ts_code."},
                    "max_results": {"type": "integer", "description": "Maximum results, default 6, max 8."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_comprehensive_intel",
            "description": "DSA-compatible alias of search_market_intel. Search comprehensive intelligence with Tavily: sector, policy, announcements, research narratives and risks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Chinese search query."},
                    "symbol": {"type": "string", "description": "Optional A-share code or ts_code."},
                    "max_results": {"type": "integer", "description": "Maximum results, default 6, max 8."},
                },
                "required": ["query"],
            },
        },
    },
]


def stock_system_prompt(extra_context: str | None = None) -> str:
    context = f"\n\n当前用户与数字人上下文:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    return f"""你是 SixRabbits Stock 的问股助手，采用 ReAct 工具调用方式工作。今天日期: {date.today().isoformat()}

可用数据源:
- TuShare: A股基础信息、最近交易日行情、历史K线、筹码、资金流，只能通过股票工具调用获取。
- Tavily Search: 新闻、公告、市场叙事、行业政策和催化信息，只能通过搜索工具调用获取。

工作规则:
1. 不要编造行情、新闻、财务或估值数据；缺少 Token 或数据源失败时必须明确说明。
2. 如果用户只给股票名称或不完整代码，先调用 resolve_stock。
3. 参考 daily_stock_analysis 的问股路径：先 get_analysis_context，再 get_realtime_quote / get_daily_history，然后 analyze_trend / calculate_ma / get_volume_analysis / analyze_pattern；需要筹码、资金、基本面时继续调用 get_chip_distribution / get_capital_flow / get_stock_info。
4. 用户问消息面、催化、利空、近期发生了什么时，调用 search_stock_news；需要行业、政策、公告、研报、风险的综合情报时调用 search_comprehensive_intel。
5. 最终回答用中文 Markdown，固定包含：标的识别、数据来源、技术/价格结构、筹码/资金/基本面、消息面、风险、观察动作。
6. 只能做研究辅助，不给保证收益、自动下单或无条件买卖结论。{context}"""


def stock_strategy_payload() -> dict:
    return {
        "skills": [
            {"id": item["id"], "name": item["name"], "description": item["description"]}
            for item in STOCK_STRATEGIES
        ],
        "default_skill_id": "bull_trend",
    }


def stock_system_prompt_with_skills(extra_context: str | None = None, skills: list[str] | None = None) -> str:
    context = f"\n\n当前用户与数字人上下文:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    selected_ids = set(skills or [])
    selected = [item for item in STOCK_STRATEGIES if item["id"] in selected_ids]
    strategy_block = (
        "\n".join(f"- {item['name']}({item['id']}): {item['instructions']}" for item in selected[:4])
        if selected
        else "- 通用分析: 未激活特定策略，按行情、技术、消息、风险的通用问股框架分析。"
    )
    return f"""你是 SixRabbits Stock 的问股助手，采用 ReAct 工具调用方式工作。今天日期: {date.today().isoformat()}

可用数据源:
- TuShare: A股基础信息、最近交易日行情、历史K线、筹码、资金流，只能通过股票工具调用获取。
- Tavily Search: 新闻、公告、市场叙事、行业政策和催化信息，只能通过搜索工具调用获取。

当前激活策略:
{strategy_block}

工作规则:
1. 不要编造行情、新闻、财务或估值数据；缺少 Token 或数据源失败时必须明确说明。
2. 如果用户只给股票名称或不完整代码，先调用 resolve_stock。
3. 参考 daily_stock_analysis 的问股路径：先 get_analysis_context，再 get_realtime_quote / get_daily_history，然后 analyze_trend / calculate_ma / get_volume_analysis / analyze_pattern；需要筹码、资金、基本面时继续调用 get_chip_distribution / get_capital_flow / get_stock_info。
4. 用户问消息面、催化、利空、近期发生了什么时，调用 search_stock_news；需要行业、政策、公告、研报、风险的综合情报时调用 search_comprehensive_intel。
5. 最终回答用中文 Markdown，固定包含：标的识别、数据来源、技术/价格结构、筹码/资金/基本面、消息面、风险、观察动作。
6. 只能做研究辅助，不给保证收益、自动下单或无条件买卖结论。{context}"""


def normalize_a_share_symbol(raw: str) -> str:
    value = raw.strip().upper().replace("_", ".")
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\d{6}", digits):
        return value
    if digits.startswith(("60", "68", "90")):
        return f"{digits}.SH"
    if digits.startswith(("00", "20", "30")):
        return f"{digits}.SZ"
    if digits.startswith(("43", "83", "87", "88", "92")):
        return f"{digits}.BJ"
    return digits


def execute_stock_tool(name: str, args: dict[str, Any]) -> dict:
    if name == "resolve_stock":
        return resolve_stock(str(args.get("query") or ""))
    if name == "get_tushare_daily":
        return get_tushare_daily(
            str(args.get("symbol") or ""),
            start_date=_optional_str(args.get("start_date")),
            end_date=_optional_str(args.get("end_date")),
            limit=_int_arg(args.get("limit"), default=90, maximum=MAX_DAILY_ROWS),
        )
    if name == "get_daily_history":
        return get_tushare_daily(
            str(args.get("symbol") or ""),
            limit=_int_arg(args.get("days"), default=90, maximum=MAX_DAILY_ROWS),
        )
    if name == "get_realtime_quote":
        return get_realtime_quote(str(args.get("symbol") or ""))
    if name == "analyze_daily_bars":
        return analyze_daily_bars(
            str(args.get("symbol") or ""),
            start_date=_optional_str(args.get("start_date")),
            end_date=_optional_str(args.get("end_date")),
        )
    if name == "analyze_trend":
        return analyze_daily_bars(
            str(args.get("symbol") or ""),
            start_date=_optional_str(args.get("start_date")),
            end_date=_optional_str(args.get("end_date")),
        )
    if name == "calculate_ma":
        return calculate_ma(
            str(args.get("symbol") or ""),
            periods=_optional_str(args.get("periods")),
            limit=_int_arg(args.get("limit"), default=160, maximum=MAX_DAILY_ROWS),
        )
    if name == "get_volume_analysis":
        return get_volume_analysis(
            str(args.get("symbol") or ""),
            days=_int_arg(args.get("days"), default=30, maximum=120),
        )
    if name == "get_chip_distribution":
        return get_chip_distribution(str(args.get("symbol") or ""))
    if name == "get_capital_flow":
        return get_capital_flow(
            str(args.get("symbol") or ""),
            days=_int_arg(args.get("days"), default=10, maximum=60),
        )
    if name == "get_stock_info":
        return get_stock_info(str(args.get("symbol") or ""))
    if name == "get_analysis_context":
        return get_analysis_context(
            str(args.get("symbol") or ""),
            limit=_int_arg(args.get("limit"), default=20, maximum=60),
        )
    if name == "analyze_pattern":
        return analyze_pattern(
            str(args.get("symbol") or ""),
            days=_int_arg(args.get("days"), default=60, maximum=120),
        )
    if name == "search_stock_news":
        return search_stock_news(
            str(args.get("query") or ""),
            symbol=_optional_str(args.get("symbol")),
            max_results=_int_arg(args.get("max_results"), default=5, maximum=8),
        )
    if name == "search_market_intel":
        return search_market_intel(
            str(args.get("query") or ""),
            symbol=_optional_str(args.get("symbol")),
            max_results=_int_arg(args.get("max_results"), default=6, maximum=8),
        )
    if name == "search_comprehensive_intel":
        return search_market_intel(
            str(args.get("query") or ""),
            symbol=_optional_str(args.get("symbol")),
            max_results=_int_arg(args.get("max_results"), default=6, maximum=8),
        )
    return {"error": f"Unknown stock tool: {name}"}

def resolve_stock(query: str) -> dict:
    key = query.strip()
    if not key:
        return {"error": "query is empty"}

    normalized = normalize_a_share_symbol(key)
    candidates: list[dict[str, Any]] = []
    candidates.extend(_resolve_stock_from_local(key, normalized))

    if len(candidates) < 8:
        remote = _stock_basic_rows()
        key_lower = key.lower()
        for row in remote:
            ts_code = str(row.get("ts_code") or "")
            symbol = str(row.get("symbol") or "")
            name = str(row.get("name") or "")
            fullname = str(row.get("fullname") or "")
            if (
                normalized == ts_code
                or key == symbol
                or key_lower in name.lower()
                or (fullname and key_lower in fullname.lower())
            ):
                candidates.append(
                    {
                        "ts_code": ts_code,
                        "symbol": symbol,
                        "name": name,
                        "market": row.get("market"),
                        "exchange": row.get("exchange"),
                        "list_date": row.get("list_date"),
                        "source": "tushare",
                    }
                )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        ts_code = str(item.get("ts_code") or item.get("symbol") or "")
        if ts_code and ts_code not in seen:
            seen.add(ts_code)
            deduped.append(item)
    return {"query": key, "normalized": normalized, "candidates": deduped[:8], "source": "local+tushare"}


def get_tushare_daily(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 90,
) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=240)).strftime("%Y%m%d")
    fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
    response = _tushare_request("daily", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
    if "error" in response:
        return response
    rows = response.get("rows", [])
    bars = [_normalize_daily_row(row) for row in rows]
    bars = sorted((row for row in bars if row), key=lambda row: row["trade_date"])
    bars = bars[-limit:]
    saved = _cache_daily_bars(ts_code, bars)
    return {
        "symbol": ts_code,
        "source": "tushare.daily",
        "start_date": start,
        "end_date": end,
        "bars": bars,
        "saved_rows": saved,
    }


def get_tushare_minutes(
    symbol: str,
    freq: str = "5min",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 800,
) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    normalized_freq = freq.strip().lower()
    if normalized_freq not in {"1min", "5min", "15min", "30min", "60min"}:
        return {"error": f"不支持的分钟周期: {freq}"}

    now = datetime.now()
    lookback_days = {
        "1min": 10,
        "5min": 30,
        "15min": 60,
        "30min": 120,
        "60min": 180,
    }[normalized_freq]
    end = _valid_minute_datetime(end_date) or now.strftime("%Y-%m-%d %H:%M:%S")
    start = _valid_minute_datetime(start_date) or (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d 09:00:00")
    fields = "ts_code,trade_time,open,close,high,low,vol,amount"
    response = _tushare_request(
        "stk_mins",
        {"ts_code": ts_code, "freq": normalized_freq, "start_date": start, "end_date": end},
        fields,
    )
    if "error" in response:
        return response
    rows = response.get("rows", [])
    bars = [_normalize_minute_row(row) for row in rows]
    bars = sorted((row for row in bars if row), key=lambda row: row["trade_time"])
    safe_limit = max(30, min(int(limit or 800), MAX_MINUTE_ROWS))
    return {
        "symbol": ts_code,
        "source": f"tushare.stk_mins.{normalized_freq}",
        "freq": normalized_freq,
        "start_date": start,
        "end_date": end,
        "bars": bars[-safe_limit:],
    }


def _analyze_daily_bars_legacy(symbol: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    payload = get_tushare_daily(symbol, start_date=start_date, end_date=end_date, limit=120)
    if "error" in payload:
        return payload
    bars = payload.get("bars") or []
    if len(bars) < 20:
        return {"error": f"日线数据不足，至少需要 20 条，当前 {len(bars)} 条", "bars": bars}

    closes = [float(row["close"]) for row in bars if row.get("close") is not None]
    volumes = [float(row.get("vol") or 0) for row in bars]
    latest = bars[-1]
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60) if len(closes) >= 60 else None
    latest_close = closes[-1]
    volume_ratio = None
    if len(volumes) >= 6 and _mean(volumes[-6:-1]):
        volume_ratio = volumes[-1] / _mean(volumes[-6:-1])
    high20 = max(float(row["high"]) for row in bars[-20:] if row.get("high") is not None)
    low20 = min(float(row["low"]) for row in bars[-20:] if row.get("low") is not None)
    high60 = max(float(row["high"]) for row in bars[-60:] if row.get("high") is not None)
    drawdown = (latest_close / high60 - 1) * 100 if high60 else None
    pct_5 = (latest_close / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else None
    pct_20 = (latest_close / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else None

    short_term = "偏强" if latest_close >= ma5 >= ma10 else "转弱" if latest_close < ma10 else "震荡"
    mid_term = "多头排列" if ma5 >= ma10 >= ma20 else "空头或修复中" if latest_close < ma20 else "中性"
    volume_state = "放量" if volume_ratio and volume_ratio >= 1.35 else "缩量" if volume_ratio and volume_ratio <= 0.75 else "正常"
    risk_flags = []
    if drawdown is not None and drawdown < -12:
        risk_flags.append("距60日高点回撤超过12%")
    if latest_close < ma20:
        risk_flags.append("收盘价低于20日均线")
    if volume_ratio and volume_ratio > 2.2 and latest_close < (bars[-2].get("close") or latest_close):
        risk_flags.append("放量下跌")

    return {
        "symbol": payload["symbol"],
        "source": payload["source"],
        "latest": latest,
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        "trend": {
            "short_term": short_term,
            "mid_term": mid_term,
            "volume_state": volume_state,
            "volume_ratio": volume_ratio,
            "pct_5d": pct_5,
            "pct_20d": pct_20,
            "drawdown_from_60d_high_pct": drawdown,
        },
        "levels": {"support_20d": low20, "resistance_20d": high20, "high_60d": high60},
        "risk_flags": risk_flags,
        "bar_count": len(bars),
        "saved_rows": payload.get("saved_rows", 0),
    }


def analyze_daily_bars(symbol: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    payload = get_tushare_daily(symbol, start_date=start_date, end_date=end_date, limit=120)
    if "error" in payload:
        return payload
    bars = payload.get("bars") or []
    if len(bars) < 20:
        return {"error": f"日线数据不足，至少需要 20 条，当前 {len(bars)} 条", "bars": bars}

    closes = [float(row["close"]) for row in bars if row.get("close") is not None]
    volumes = [float(row.get("vol") or 0) for row in bars]
    latest = bars[-1]
    latest_close = closes[-1]
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60) if len(closes) >= 60 else None
    volume_ratio = None
    if len(volumes) >= 6 and _mean(volumes[-6:-1]):
        volume_ratio = volumes[-1] / _mean(volumes[-6:-1])
    high20 = max(float(row["high"]) for row in bars[-20:] if row.get("high") is not None)
    low20 = min(float(row["low"]) for row in bars[-20:] if row.get("low") is not None)
    high60 = max(float(row["high"]) for row in bars[-60:] if row.get("high") is not None)
    drawdown = (latest_close / high60 - 1) * 100 if high60 else None
    pct_5 = (latest_close / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else None
    pct_20 = (latest_close / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else None
    short_term = "偏强" if latest_close >= ma5 >= ma10 else "转弱" if latest_close < ma10 else "震荡"
    mid_term = "多头排列" if ma5 >= ma10 >= ma20 else "空头或修复中" if latest_close < ma20 else "中性"
    volume_state = "放量" if volume_ratio and volume_ratio >= 1.35 else "缩量" if volume_ratio and volume_ratio <= 0.75 else "正常"
    risk_flags: list[str] = []
    if drawdown is not None and drawdown < -12:
        risk_flags.append("距60日高点回撤超过12%")
    if latest_close < ma20:
        risk_flags.append("收盘价低于20日均线")
    if volume_ratio and volume_ratio > 2.2 and latest_close < (bars[-2].get("close") or latest_close):
        risk_flags.append("放量下跌")

    macd = _macd_summary(closes)
    rsi = _rsi_summary(closes)
    signal = _technical_signal(
        latest_close=latest_close,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        volume_ratio=volume_ratio,
        macd_status=macd["status"],
        rsi_status=rsi["status"],
        risk_flags=risk_flags,
    )

    return {
        "symbol": payload["symbol"],
        "source": payload["source"],
        "latest": latest,
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        "bias": {
            "ma5": _pct(latest_close, ma5),
            "ma10": _pct(latest_close, ma10),
            "ma20": _pct(latest_close, ma20),
            "ma60": _pct(latest_close, ma60) if ma60 else None,
        },
        "macd": macd,
        "rsi": rsi,
        "trend": {
            "short_term": short_term,
            "mid_term": mid_term,
            "ma_alignment": signal["ma_alignment"],
            "trend_strength": signal["trend_strength"],
            "volume_state": volume_state,
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "pct_5d": round(pct_5, 2) if pct_5 is not None else None,
            "pct_20d": round(pct_20, 2) if pct_20 is not None else None,
            "drawdown_from_60d_high_pct": round(drawdown, 2) if drawdown is not None else None,
        },
        "levels": {"support_20d": low20, "resistance_20d": high20, "high_60d": high60},
        "signal": signal,
        "risk_flags": risk_flags,
        "bar_count": len(bars),
        "saved_rows": payload.get("saved_rows", 0),
    }


def calculate_ma(symbol: str, periods: str | None = None, limit: int = 160) -> dict:
    payload = get_tushare_daily(symbol, limit=limit)
    if "error" in payload:
        return payload
    bars = payload.get("bars") or []
    closes = [float(row["close"]) for row in bars if row.get("close") is not None]
    if not closes:
        return {"error": "没有可用收盘价数据"}
    parsed_periods: list[int] = []
    for item in (periods or "5,10,20,30,60,120,250").split(","):
        try:
            period = int(item.strip())
        except ValueError:
            continue
        if 1 <= period <= 250 and period not in parsed_periods:
            parsed_periods.append(period)
    current = closes[-1]
    ma_result: dict[str, Any] = {}
    above_count = 0
    valid_count = 0
    for period in sorted(parsed_periods):
        key = f"ma{period}"
        if len(closes) < period:
            ma_result[key] = None
            continue
        value = _ma(closes, period)
        price_above = current >= value
        valid_count += 1
        above_count += 1 if price_above else 0
        ma_result[key] = {"value": value, "bias_pct": _pct(current, value), "price_above": price_above}
    alignment = (
        "多头排列" if valid_count and above_count == valid_count else
        "空头排列" if valid_count and above_count == 0 else
        f"混合排列({above_count}/{valid_count} 条均线上方)"
    )
    return {
        "symbol": payload["symbol"],
        "source": payload["source"],
        "current_price": current,
        "ma": ma_result,
        "above_ma_count": above_count,
        "total_ma_count": valid_count,
        "ma_alignment": alignment,
        "bar_count": len(bars),
    }


def get_volume_analysis(symbol: str, days: int = 30) -> dict:
    payload = get_tushare_daily(symbol, limit=max(days + 20, 60))
    if "error" in payload:
        return payload
    bars = (payload.get("bars") or [])[-days:]
    if len(bars) < 5:
        return {"error": f"量价分析数据不足，至少需要 5 条，当前 {len(bars)} 条"}
    closes = [float(row["close"]) for row in bars if row.get("close") is not None]
    volumes = [float(row.get("vol") or 0) for row in bars]
    latest_volume = volumes[-1]
    avg_5 = _mean(volumes[-5:])
    avg_10 = _mean(volumes[-10:]) if len(volumes) >= 10 else avg_5
    avg_20 = _mean(volumes[-20:]) if len(volumes) >= 20 else avg_10
    up_volumes = [volumes[index] for index in range(1, len(closes)) if closes[index] > closes[index - 1]]
    down_volumes = [volumes[index] for index in range(1, len(closes)) if closes[index] <= closes[index - 1]]
    volume_trend_pct = 0.0
    if len(volumes) >= 10:
        prior = _mean(volumes[-10:-5])
        recent = _mean(volumes[-5:])
        volume_trend_pct = ((recent - prior) / prior * 100) if prior else 0.0
    volume_trend = "放量" if volume_trend_pct > 20 else "缩量" if volume_trend_pct < -20 else "量能平稳"
    ratio_5 = latest_volume / avg_5 if avg_5 else None
    ratio_20 = latest_volume / avg_20 if avg_20 else None
    avg_up = _mean(up_volumes)
    avg_down = _mean(down_volumes)
    if avg_up and avg_down and avg_up > avg_down * 1.3:
        pattern = "量价配合良好，上涨日放量"
    elif avg_up and avg_down and avg_down > avg_up * 1.3:
        pattern = "量价背离，下跌日放量"
    elif ratio_5 and ratio_5 > 1.5:
        pattern = "近期明显放量"
    elif ratio_5 and ratio_5 < 0.6:
        pattern = "近期明显缩量"
    else:
        pattern = "量价关系中性"
    return {
        "symbol": payload["symbol"],
        "source": payload["source"],
        "period_days": len(bars),
        "latest_volume": latest_volume,
        "avg_volume_5d": round(avg_5, 2),
        "avg_volume_20d": round(avg_20, 2),
        "volume_ratio_vs_5d": round(ratio_5, 2) if ratio_5 is not None else None,
        "volume_ratio_vs_20d": round(ratio_20, 2) if ratio_20 is not None else None,
        "avg_up_day_volume": round(avg_up, 2),
        "avg_down_day_volume": round(avg_down, 2),
        "volume_trend": volume_trend,
        "volume_trend_pct": round(volume_trend_pct, 2),
        "volume_price_corr": _pearson(volumes, closes),
        "pattern": pattern,
    }


def analyze_pattern(symbol: str, days: int = 60) -> dict:
    payload = get_tushare_daily(symbol, limit=max(days, 120))
    if "error" in payload:
        return payload
    bars = (payload.get("bars") or [])[-days:]
    if len(bars) < 10:
        return {"error": f"形态识别数据不足，至少需要 10 条，当前 {len(bars)} 条"}
    patterns: list[dict[str, Any]] = []
    bodies = [abs(float(row["close"]) - float(row["open"])) for row in bars]
    avg_body = _mean(bodies) or 1
    for offset, row in enumerate(bars[-3:]):
        open_price = float(row["open"])
        close_price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        body = abs(close_price - open_price)
        upper_shadow = high - max(open_price, close_price)
        lower_shadow = min(open_price, close_price) - low
        day_offset = offset - 2
        if body < avg_body * 0.15 and (upper_shadow + lower_shadow) > body * 3:
            patterns.append({"pattern": "十字星", "type": "reversal_signal", "day_offset": day_offset, "strength": "弱", "desc": "多空平衡，等待方向选择"})
        if lower_shadow > max(body, 0.01) * 2 and upper_shadow < max(body, 0.01):
            patterns.append({"pattern": "锤子线", "type": "bullish_reversal", "day_offset": day_offset, "strength": "中", "desc": "下影线较长，低位时有反转含义"})
        if upper_shadow > max(body, 0.01) * 2 and lower_shadow < max(body, 0.01):
            patterns.append({"pattern": "射击之星", "type": "bearish_signal", "day_offset": day_offset, "strength": "中", "desc": "上影线较长，高位时需防回落"})
    prev = bars[-2]
    last = bars[-1]
    if float(last["close"]) > float(last["open"]) and float(prev["close"]) < float(prev["open"]):
        if float(last["open"]) <= float(prev["close"]) and float(last["close"]) >= float(prev["open"]):
            patterns.append({"pattern": "看涨吞没", "type": "bullish_reversal", "day_offset": 0, "strength": "强", "desc": "阳线覆盖前一阴线"})
    if float(last["close"]) < float(last["open"]) and float(prev["close"]) > float(prev["open"]):
        if float(last["open"]) >= float(prev["close"]) and float(last["close"]) <= float(prev["open"]):
            patterns.append({"pattern": "看跌吞没", "type": "bearish_reversal", "day_offset": 0, "strength": "强", "desc": "阴线覆盖前一阳线"})
    highs = [float(row["high"]) for row in bars]
    lows = [float(row["low"]) for row in bars]
    closes = [float(row["close"]) for row in bars]
    volumes = [float(row.get("vol") or 0) for row in bars]
    if len(bars) >= 21:
        high_20 = max(highs[-21:-1])
        avg_volume = _mean(volumes[-6:-1])
        if closes[-1] > high_20 and (not avg_volume or volumes[-1] > avg_volume * 1.4):
            patterns.append({"pattern": "放量突破20日高点", "type": "bullish_breakout", "day_offset": 0, "strength": "强", "desc": "价格突破近20日高点且量能配合"})
    if len(bars) >= 10:
        recent_high = max(highs[-10:])
        recent_low = min(lows[-10:])
        box_range_pct = (recent_high - recent_low) / recent_low * 100 if recent_low else 0
        if box_range_pct < 8:
            patterns.append({"pattern": "箱体震荡", "type": "consolidation", "day_offset": 0, "strength": "中", "desc": f"近10日波幅 {box_range_pct:.1f}%"})
    low_indices = sorted(range(len(lows)), key=lambda index: lows[index])[:5]
    if len(low_indices) >= 2:
        left, right = sorted(low_indices[:2])
        if right - left >= 5 and abs(lows[left] - lows[right]) / max(lows[left], lows[right]) < 0.03:
            patterns.append({"pattern": "双底", "type": "bullish_reversal", "day_offset": -(len(lows) - 1 - right), "strength": "强", "desc": "两个低点接近，存在底部结构"})
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in reversed(patterns):
        if item["pattern"] in seen:
            continue
        seen.add(item["pattern"])
        deduped.append(item)
    deduped.reverse()
    return {
        "symbol": payload["symbol"],
        "source": payload["source"],
        "period_days": len(bars),
        "current_price": closes[-1],
        "patterns_count": len(deduped),
        "patterns": deduped,
        "summary": "、".join(item["pattern"] for item in deduped) if deduped else "未发现明显形态",
    }


def get_realtime_quote(symbol: str) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    daily = get_tushare_daily(ts_code, limit=2)
    if "error" in daily:
        return daily
    bars = daily.get("bars") or []
    if not bars:
        return {"error": f"没有可用行情快照: {symbol}"}
    latest = bars[-1]
    previous = bars[-2] if len(bars) >= 2 else {}
    basic = _latest_daily_basic(ts_code, end_date=latest.get("trade_date"))
    pct_chg = latest.get("pct_chg")
    if pct_chg is None and previous.get("close"):
        pct_chg = _pct(float(latest["close"]), float(previous["close"]))
    info = _stock_basic_item(ts_code) or {}
    return {
        "symbol": ts_code,
        "name": info.get("name"),
        "trade_date": latest.get("trade_date"),
        "price": latest.get("close"),
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "pre_close": latest.get("pre_close") or previous.get("close"),
        "change": latest.get("change"),
        "pct_chg": pct_chg,
        "volume": latest.get("vol"),
        "amount": latest.get("amount"),
        "turnover_rate": basic.get("turnover_rate"),
        "volume_ratio": basic.get("volume_ratio"),
        "pe": basic.get("pe"),
        "pb": basic.get("pb"),
        "total_mv": basic.get("total_mv"),
        "circ_mv": basic.get("circ_mv"),
        "source": "tushare.daily+tushare.daily_basic",
        "is_realtime": False,
        "freshness_note": "TuShare 当前接入为最近交易日快照，不保证盘中实时。",
    }


def get_chip_distribution(symbol: str) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=180)).strftime("%Y%m%d")
    fields = "ts_code,trade_date,his_low,his_high,cost_5pct,cost_15pct,cost_50pct,cost_85pct,cost_95pct,weight_avg,winner_rate"
    response = _tushare_request("cyq_perf", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
    if "error" in response:
        return {**response, "note": "筹码分布依赖 TuShare cyq_perf 权限，若账号无权限会返回失败。"}
    rows = sorted(response.get("rows") or [], key=lambda row: str(row.get("trade_date") or ""))
    if not rows:
        return {"error": f"TuShare 未返回筹码分布数据: {ts_code}"}
    latest = rows[-1]
    return {
        "symbol": ts_code,
        "source": "tushare.cyq_perf",
        "trade_date": latest.get("trade_date"),
        "his_low": _float(latest.get("his_low")),
        "his_high": _float(latest.get("his_high")),
        "cost_5pct": _float(latest.get("cost_5pct")),
        "cost_15pct": _float(latest.get("cost_15pct")),
        "cost_50pct": _float(latest.get("cost_50pct")),
        "cost_85pct": _float(latest.get("cost_85pct")),
        "cost_95pct": _float(latest.get("cost_95pct")),
        "avg_cost": _float(latest.get("weight_avg")),
        "winner_rate": _float(latest.get("winner_rate")),
        "rows": rows[-20:],
    }


def get_capital_flow(symbol: str, days: int = 10) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=max(days * 3, 45))).strftime("%Y%m%d")
    fields = (
        "ts_code,trade_date,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,"
        "net_mf_amount,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount"
    )
    response = _tushare_request("moneyflow", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
    if "error" in response:
        return {**response, "note": "资金流依赖 TuShare moneyflow 权限，若账号无权限会返回失败。"}
    rows = sorted(response.get("rows") or [], key=lambda row: str(row.get("trade_date") or ""))[-days:]
    if not rows:
        return {"error": f"TuShare 未返回资金流数据: {ts_code}"}

    def main_net(row: dict[str, Any]) -> float:
        buy = (_float(row.get("buy_lg_amount")) or 0) + (_float(row.get("buy_elg_amount")) or 0)
        sell = (_float(row.get("sell_lg_amount")) or 0) + (_float(row.get("sell_elg_amount")) or 0)
        return round(buy - sell, 2)

    latest = rows[-1]
    main_values = [main_net(row) for row in rows]
    net_values = [_float(row.get("net_mf_amount")) or 0 for row in rows]
    return {
        "symbol": ts_code,
        "source": "tushare.moneyflow",
        "trade_date": latest.get("trade_date"),
        "main_net_amount": main_values[-1],
        "main_net_amount_5d": round(sum(main_values[-5:]), 2),
        "main_net_amount_10d": round(sum(main_values[-10:]), 2),
        "net_mf_amount": round(net_values[-1], 2),
        "net_mf_amount_5d": round(sum(net_values[-5:]), 2),
        "net_mf_amount_10d": round(sum(net_values[-10:]), 2),
        "rows": [{**row, "main_net_amount": main_net(row)} for row in rows],
    }


def get_stock_info(symbol: str) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}
    info = _stock_basic_item(ts_code) or {}
    basic = _latest_daily_basic(ts_code)
    financial = _latest_fina_indicator(ts_code)
    return {
        "symbol": ts_code,
        "name": info.get("name"),
        "industry": info.get("industry"),
        "market": info.get("market"),
        "exchange": info.get("exchange"),
        "list_date": info.get("list_date"),
        "source": "tushare.stock_basic+daily_basic+fina_indicator",
        "trade_date": basic.get("trade_date"),
        "close": basic.get("close"),
        "turnover_rate": basic.get("turnover_rate"),
        "volume_ratio": basic.get("volume_ratio"),
        "pe": basic.get("pe"),
        "pb": basic.get("pb"),
        "ps": basic.get("ps"),
        "total_mv": basic.get("total_mv"),
        "circ_mv": basic.get("circ_mv"),
        "financial": financial,
    }


def get_analysis_context(symbol: str, limit: int = 20) -> dict:
    ts_code = normalize_a_share_symbol(symbol)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}
    rows: list[dict[str, Any]] = []
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    "SELECT trade_date, open, high, low, close, pre_close, change_pct, volume, amount, source, created_at "
                    "FROM daily_bars WHERE symbol = :symbol "
                    "ORDER BY trade_date DESC LIMIT :limit"
                ),
                {"symbol": ts_code, "limit": max(1, min(limit, 60))},
            )
            for row in result.mappings():
                rows.append(
                    {
                        "trade_date": row.get("trade_date"),
                        "open": _float(row.get("open")),
                        "high": _float(row.get("high")),
                        "low": _float(row.get("low")),
                        "close": _float(row.get("close")),
                        "pre_close": _float(row.get("pre_close")),
                        "pct_chg": _float(row.get("change_pct")),
                        "vol": _float(row.get("volume")),
                        "amount": _float(row.get("amount")),
                        "source": row.get("source"),
                        "cached_at": str(row.get("created_at")) if row.get("created_at") else None,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取本地分析上下文失败: {exc}"}
    rows = list(reversed(rows))
    latest = rows[-1] if rows else None
    info = _stock_basic_item(ts_code) or {}
    return {
        "symbol": ts_code,
        "name": info.get("name"),
        "source": "local.daily_bars",
        "has_context": bool(rows),
        "bar_count": len(rows),
        "start_trade_date": rows[0].get("trade_date") if rows else None,
        "end_trade_date": latest.get("trade_date") if latest else None,
        "latest": latest,
        "bars": rows,
        "note": "本工具只读取 SixRabbits 本地已缓存的历史K线；若为空，后续应调用 get_daily_history 从 TuShare 获取。",
    }


def search_market_intel(query: str, symbol: str | None = None, max_results: int = 6) -> dict:
    enriched = query.strip()
    if symbol:
        normalized = normalize_a_share_symbol(symbol)
        info = _stock_basic_item(normalized) or {}
        name = info.get("name") or normalized
        enriched = f"{normalized} {name} {enriched} 行业 板块 政策 公告 研报 风险"
    else:
        enriched = f"{enriched} A股 行业 板块 政策 公告 研报 风险"
    return search_stock_news(enriched, symbol=None, max_results=max_results)


def search_stock_news(query: str, symbol: str | None = None, max_results: int = 5) -> dict:
    keyword = query.strip()
    if symbol:
        keyword = f"{normalize_a_share_symbol(symbol)} {keyword}"
    if not keyword:
        return {"error": "query is empty"}
    api_key = _data_source_api_key("tavily", settings.tavily_api_key)
    if not api_key:
        return {"error": "未配置 STOCK_TAVILY_API_KEY，无法使用 Tavily Search。"}
    url = _data_source_base_url("tavily", TAVILY_URL)

    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.post(
                url,
                json={
                    "api_key": api_key,
                    "query": keyword,
                    "search_depth": "basic",
                    "include_answer": False,
                    "max_results": max_results,
                },
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Tavily Search 调用失败: {str(exc)[:500]}"}

    results = []
    for item in body.get("results") or []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
        )
    saved = _cache_documents(results, symbol=normalize_a_share_symbol(symbol) if symbol else None)
    return {
        "query": keyword,
        "source": "tavily.search",
        "results": results,
        "saved_rows": saved,
        "ms": int((time.monotonic() - started) * 1000),
    }


def _tushare_request(api_name: str, params: dict[str, Any], fields: str) -> dict:
    api_key = _data_source_api_key("tushare", settings.tushare_token)
    if not api_key:
        return {"error": "未配置 STOCK_TUSHARE_TOKEN，无法调用 TuShare。"}
    url = _data_source_base_url("tushare", TUSHARE_URL)
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = client.post(
                url,
                json={"api_name": api_name, "token": api_key, "params": params, "fields": fields},
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"TuShare 调用失败: {str(exc)[:500]}"}
    if body.get("code") != 0:
        return {"error": f"TuShare 返回错误: {body.get('msg') or body}"}
    data = body.get("data") or {}
    field_names = data.get("fields") or []
    rows = [dict(zip(field_names, item)) for item in data.get("items") or []]
    return {"rows": rows, "ms": int((time.monotonic() - started) * 1000)}


def _data_source_row(provider: str) -> dict[str, Any] | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT provider, base_url, api_key, status "
                    "FROM data_source_configs WHERE provider = :provider LIMIT 1"
                ),
                {"provider": provider},
            ).mappings().first()
            return dict(row) if row else None
    except Exception:
        return None


def _data_source_api_key(provider: str, env_value: str) -> str:
    row = _data_source_row(provider)
    if row and str(row.get("status") or "enabled") != "enabled":
        return ""
    db_key = str(row.get("api_key") or "").strip() if row else ""
    return db_key or env_value


def _data_source_base_url(provider: str, default_url: str) -> str:
    row = _data_source_row(provider)
    if row and str(row.get("base_url") or "").strip():
        return str(row["base_url"]).strip()
    return default_url


def _stock_basic_rows() -> list[dict[str, Any]]:
    now = time.monotonic()
    if _STOCK_BASIC_CACHE["expires_at"] > now:
        return list(_STOCK_BASIC_CACHE["rows"])
    fields = "ts_code,symbol,name,fullname,market,exchange,list_date"
    response = _tushare_request("stock_basic", {"list_status": "L"}, fields)
    if "error" in response:
        return []
    rows = response.get("rows") or []
    _STOCK_BASIC_CACHE["rows"] = rows
    _STOCK_BASIC_CACHE["expires_at"] = now + 86400
    return list(rows)


def _stock_basic_item(ts_code: str) -> dict[str, Any] | None:
    normalized = normalize_a_share_symbol(ts_code)
    for row in _stock_basic_rows():
        if str(row.get("ts_code") or "") == normalized:
            return row
    return None


def _latest_daily_basic(ts_code: str, end_date: str | None = None) -> dict[str, Any]:
    end = _valid_yyyymmdd(end_date) or date.today().strftime("%Y%m%d")
    start = (datetime.strptime(end, "%Y%m%d").date() - timedelta(days=45)).strftime("%Y%m%d")
    fields = "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pb,ps,total_mv,circ_mv"
    response = _tushare_request("daily_basic", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
    if "error" in response:
        return {}
    rows = sorted(response.get("rows") or [], key=lambda row: str(row.get("trade_date") or ""))
    return rows[-1] if rows else {}


def _latest_fina_indicator(ts_code: str) -> dict[str, Any]:
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=540)).strftime("%Y%m%d")
    fields = "ts_code,end_date,eps,dt_eps,roe,roa,grossprofit_margin,netprofit_margin,assets_turn,current_ratio,quick_ratio"
    response = _tushare_request("fina_indicator", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
    if "error" in response:
        return {"error": response["error"]}
    rows = sorted(response.get("rows") or [], key=lambda row: str(row.get("end_date") or ""))
    return rows[-1] if rows else {}


def _resolve_stock_from_local(query: str, normalized: str) -> list[dict[str, Any]]:
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT symbol, name, market, list_date FROM instruments "
                    "WHERE symbol = :symbol OR symbol = :query OR name LIKE :name "
                    "ORDER BY symbol LIMIT 8"
                ),
                {"symbol": normalized, "query": query, "name": f"%{query}%"},
            ).mappings()
            return [
                {
                    "ts_code": row["symbol"],
                    "symbol": str(row["symbol"]).split(".")[0],
                    "name": row["name"],
                    "market": row["market"],
                    "list_date": row["list_date"],
                    "source": "local.instruments",
                }
                for row in rows
            ]
    except Exception:
        return []


def _normalize_daily_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return {
            "ts_code": row.get("ts_code"),
            "trade_date": str(row.get("trade_date")),
            "open": _float(row.get("open")),
            "high": _float(row.get("high")),
            "low": _float(row.get("low")),
            "close": _float(row.get("close")),
            "pre_close": _float(row.get("pre_close")),
            "change": _float(row.get("change")),
            "pct_chg": _float(row.get("pct_chg")),
            "vol": _float(row.get("vol")),
            "amount": _float(row.get("amount")),
        }
    except (TypeError, ValueError):
        return None


def _normalize_minute_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        trade_time = _valid_minute_datetime(str(row.get("trade_time") or row.get("time") or ""))
        if not trade_time:
            return None
        return {
            "ts_code": row.get("ts_code") or row.get("code"),
            "trade_time": trade_time,
            "open": _float(row.get("open")),
            "high": _float(row.get("high")),
            "low": _float(row.get("low")),
            "close": _float(row.get("close")),
            "vol": _float(row.get("vol")),
            "amount": _float(row.get("amount")),
        }
    except (TypeError, ValueError):
        return None


def _cache_daily_bars(symbol: str, bars: list[dict[str, Any]]) -> int:
    if not bars:
        return 0
    try:
        _ensure_instrument(symbol)
        dialect = engine.dialect.name
        with engine.begin() as conn:
            if dialect == "mysql":
                stmt = text(
                    "INSERT INTO daily_bars "
                    "(symbol, trade_date, open, high, low, close, pre_close, change_pct, volume, amount, source) "
                    "VALUES (:symbol, :trade_date, :open, :high, :low, :close, :pre_close, :change_pct, :volume, :amount, :source) "
                    "ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close), "
                    "pre_close=VALUES(pre_close), change_pct=VALUES(change_pct), volume=VALUES(volume), amount=VALUES(amount)"
                )
            else:
                stmt = text(
                    "INSERT OR REPLACE INTO daily_bars "
                    "(symbol, trade_date, open, high, low, close, pre_close, change_pct, volume, amount, source) "
                    "VALUES (:symbol, :trade_date, :open, :high, :low, :close, :pre_close, :change_pct, :volume, :amount, :source)"
                )
            conn.execute(
                stmt,
                [
                    {
                        "symbol": symbol,
                        "trade_date": row["trade_date"],
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "pre_close": row.get("pre_close"),
                        "change_pct": row.get("pct_chg"),
                        "volume": row.get("vol"),
                        "amount": row.get("amount"),
                        "source": "tushare",
                    }
                    for row in bars
                ],
            )
        return len(bars)
    except Exception:
        return 0


def _ensure_instrument(symbol: str, name: str | None = None) -> None:
    dialect = engine.dialect.name
    exchange = symbol.split(".")[-1] if "." in symbol else "CN"
    try:
        with engine.begin() as conn:
            if dialect == "mysql":
                conn.execute(
                    text(
                        "INSERT INTO instruments (symbol, name, market, type, status) "
                        "VALUES (:symbol, :name, :market, 'stock', 'active') "
                        "ON DUPLICATE KEY UPDATE market=VALUES(market)"
                    ),
                    {"symbol": symbol, "name": name or symbol, "market": exchange},
                )
            else:
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO instruments (symbol, name, market, type, status) "
                        "VALUES (:symbol, :name, :market, 'stock', 'active')"
                    ),
                    {"symbol": symbol, "name": name or symbol, "market": exchange},
                )
    except Exception:
        pass


def _cache_documents(results: list[dict[str, Any]], symbol: str | None = None) -> int:
    if not results:
        return 0
    saved = 0
    try:
        dialect = engine.dialect.name
        with engine.begin() as conn:
            for item in results:
                url = str(item.get("url") or "")
                title = str(item.get("title") or "")[:500]
                content = str(item.get("content") or "")
                if not url or not title:
                    continue
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
                if dialect == "mysql":
                    conn.execute(
                        text(
                            "INSERT INTO documents "
                            "(doc_type, title, content, summary, source, source_url, trust_level, is_external_untrusted, content_hash) "
                            "VALUES ('news', :title, :content, :summary, 'tavily', :url, 'normal', 1, :hash) "
                            "ON DUPLICATE KEY UPDATE fetched_at=CURRENT_TIMESTAMP"
                        ),
                        {"title": title, "content": content, "summary": content[:500], "url": url, "hash": digest},
                    )
                else:
                    conn.execute(
                        text(
                            "INSERT OR IGNORE INTO documents "
                            "(doc_type, title, content, summary, source, source_url, trust_level, is_external_untrusted, content_hash) "
                            "VALUES ('news', :title, :content, :summary, 'tavily', :url, 'normal', 1, :hash)"
                        ),
                        {"title": title, "content": content, "summary": content[:500], "url": url, "hash": digest},
                    )
                saved += 1
    except Exception:
        return saved
    return saved


def _valid_yyyymmdd(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().replace("-", "")
    if not re.fullmatch(r"\d{8}", value):
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return value


def _valid_minute_datetime(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


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


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ma(values: list[float], window: int) -> float:
    return round(_mean(values[-window:]), 4)


def _pct(current: float, base: float | None) -> float | None:
    if not base:
        return None
    return round((current - base) / base * 100, 2)


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _macd_summary(closes: list[float]) -> dict:
    if len(closes) < 26:
        return {"dif": None, "dea": None, "bar": None, "status": "数据不足", "signal": "MACD 至少需要 26 条收盘价"}
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    dif_values = [fast - slow for fast, slow in zip(ema_fast, ema_slow)]
    dea_values = _ema(dif_values, 9)
    bar_values = [(dif - dea) * 2 for dif, dea in zip(dif_values, dea_values)]
    dif = dif_values[-1]
    dea = dea_values[-1]
    bar = bar_values[-1]
    prev_cross = dif_values[-2] - dea_values[-2]
    curr_cross = dif - dea
    if prev_cross <= 0 < curr_cross and dif > 0:
        status = "零轴上金叉"
        signal = "DIF 上穿 DEA 且位于零轴上方，趋势确认较强"
    elif prev_cross <= 0 < curr_cross:
        status = "金叉"
        signal = "DIF 上穿 DEA，趋势有转强迹象"
    elif prev_cross >= 0 > curr_cross:
        status = "死叉"
        signal = "DIF 下穿 DEA，趋势转弱"
    elif dif > 0 and dea > 0:
        status = "多头"
        signal = "DIF/DEA 位于零轴上方，多头延续"
    elif dif < 0 and dea < 0:
        status = "空头"
        signal = "DIF/DEA 位于零轴下方，空头占优"
    elif dif_values[-2] <= 0 < dif:
        status = "上穿零轴"
        signal = "DIF 上穿零轴，趋势修复"
    elif dif_values[-2] >= 0 > dif:
        status = "下穿零轴"
        signal = "DIF 下穿零轴，趋势走弱"
    else:
        status = "中性"
        signal = "MACD 处于中性区域"
    return {
        "dif": round(dif, 4),
        "dea": round(dea, 4),
        "bar": round(bar, 4),
        "status": status,
        "signal": signal,
    }


def _rsi_values(closes: list[float], period: int) -> list[float]:
    if len(closes) < 2:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = _ema(gains, period)
    avg_loss = _ema(losses, period)
    result: list[float] = []
    for gain, loss in zip(avg_gain, avg_loss):
        if loss == 0:
            result.append(100.0 if gain > 0 else 50.0)
        else:
            rs = gain / loss
            result.append(100 - (100 / (1 + rs)))
    return result


def _rsi_summary(closes: list[float]) -> dict:
    if len(closes) < 24:
        return {"rsi_6": None, "rsi_12": None, "rsi_24": None, "status": "数据不足", "signal": "RSI 至少需要 24 条收盘价"}
    rsi_6 = _rsi_values(closes, 6)[-1]
    rsi_12 = _rsi_values(closes, 12)[-1]
    rsi_24 = _rsi_values(closes, 24)[-1]
    if rsi_12 > 70:
        status = "超买"
        signal = f"RSI(12)={rsi_12:.1f}，短期回调风险升高"
    elif rsi_12 > 60:
        status = "强势"
        signal = f"RSI(12)={rsi_12:.1f}，多头动能较强"
    elif rsi_12 >= 40:
        status = "中性"
        signal = f"RSI(12)={rsi_12:.1f}，偏震荡"
    elif rsi_12 >= 30:
        status = "弱势"
        signal = f"RSI(12)={rsi_12:.1f}，关注修复"
    else:
        status = "超卖"
        signal = f"RSI(12)={rsi_12:.1f}，存在反弹可能但需确认"
    return {
        "rsi_6": round(rsi_6, 2),
        "rsi_12": round(rsi_12, 2),
        "rsi_24": round(rsi_24, 2),
        "status": status,
        "signal": signal,
    }


def _technical_signal(
    *,
    latest_close: float,
    ma5: float,
    ma10: float,
    ma20: float,
    volume_ratio: float | None,
    macd_status: str,
    rsi_status: str,
    risk_flags: list[str],
) -> dict:
    score = 50
    reasons: list[str] = []
    risks: list[str] = list(risk_flags)
    if ma5 >= ma10 >= ma20:
        score += 15
        ma_alignment = "多头排列 MA5>=MA10>=MA20"
        reasons.append("均线多头排列")
        trend_strength = 75
    elif latest_close < ma20 and ma5 <= ma10 <= ma20:
        score -= 15
        ma_alignment = "空头排列或弱势下行"
        risks.append("均线空头排列")
        trend_strength = 25
    else:
        ma_alignment = "均线缠绕或修复中"
        trend_strength = 50

    bias_ma5 = _pct(latest_close, ma5) or 0
    if bias_ma5 < 0:
        score += 5
        reasons.append(f"价格低于 MA5 {abs(bias_ma5):.1f}%，存在回踩观察点")
    elif bias_ma5 <= 5:
        score += 4
        reasons.append(f"价格相对 MA5 乖离 {bias_ma5:.1f}%，未明显追高")
    else:
        score -= 10
        risks.append(f"价格相对 MA5 乖离 {bias_ma5:.1f}%，追高风险")

    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            score += 5
            reasons.append(f"近一日量比 {volume_ratio:.2f}，量能放大")
        elif volume_ratio <= 0.7:
            reasons.append(f"近一日量比 {volume_ratio:.2f}，量能收缩")

    if macd_status in {"零轴上金叉", "金叉", "多头", "上穿零轴"}:
        score += 8
        reasons.append(f"MACD {macd_status}")
    elif macd_status in {"死叉", "空头", "下穿零轴"}:
        score -= 8
        risks.append(f"MACD {macd_status}")

    if rsi_status in {"强势", "超卖"}:
        score += 5
        reasons.append(f"RSI {rsi_status}")
    elif rsi_status == "超买":
        score -= 6
        risks.append("RSI 超买")

    score = max(0, min(100, round(score)))
    if score >= 75:
        buy_signal = "偏多观察"
    elif score >= 60:
        buy_signal = "谨慎偏多"
    elif score >= 40:
        buy_signal = "震荡观察"
    elif score >= 25:
        buy_signal = "偏弱防守"
    else:
        buy_signal = "风险优先"
    return {
        "buy_signal": buy_signal,
        "signal_score": score,
        "ma_alignment": ma_alignment,
        "trend_strength": trend_strength,
        "signal_reasons": reasons,
        "risk_factors": risks,
    }


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_den = sum((a - left_mean) ** 2 for a in left) ** 0.5
    right_den = sum((b - right_mean) ** 2 for b in right) ** 0.5
    if not left_den or not right_den:
        return None
    return round(numerator / (left_den * right_den), 3)


_STOCK_TOOL_DISPLAY_NAMES = {
    "resolve_stock": "识别股票标的",
    "get_realtime_quote": "获取实时行情",
    "get_tushare_daily": "获取历史K线",
    "get_daily_history": "获取历史K线",
    "analyze_daily_bars": "分析技术趋势",
    "analyze_trend": "分析技术趋势",
    "calculate_ma": "计算均线系统",
    "get_volume_analysis": "分析量能变化",
    "get_chip_distribution": "分析筹码分布",
    "analyze_pattern": "识别K线形态",
    "search_stock_news": "搜索股票新闻",
    "search_market_intel": "搜索综合情报",
    "search_comprehensive_intel": "搜索综合情报",
    "get_capital_flow": "分析资金流向",
    "get_stock_info": "获取股票基本面",
    "get_analysis_context": "获取分析上下文",
}


def stock_tool_display_name(name: str) -> str:
    return _STOCK_TOOL_DISPLAY_NAMES.get(name, name)


def stock_observation_preview(name: str, result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "resolve_stock":
        items = result.get("candidates") or []
        if not items:
            return "未找到匹配标的。"
        preview = "、".join(f"{item.get('ts_code')} {item.get('name')}" for item in items[:5])
        return f"找到 {len(items)} 个候选：{preview}"
    if name in {"get_tushare_daily", "get_daily_history"}:
        rows = result.get("bars") or []
        latest = rows[-1] if rows else {}
        return f"TuShare 返回 {len(rows)} 条日线，最新交易日 {latest.get('trade_date', '-')}, 收盘 {latest.get('close', '-')}"
    if name == "get_realtime_quote":
        return (
            f"完成行情快照：{result.get('symbol', '-')} {result.get('name', '')} "
            f"{result.get('trade_date', '-')} 价格 {result.get('price', '-')}, "
            f"涨跌幅 {result.get('pct_chg', '-')}%, 量比 {result.get('volume_ratio', '-')}"
        )
    if name in {"analyze_daily_bars", "analyze_trend"}:
        latest = result.get("latest") or {}
        trend = result.get("trend") or {}
        return (
            f"完成技术摘要：最新 {latest.get('trade_date', '-')} 收盘 {latest.get('close', '-')}, "
            f"短期趋势 {trend.get('short_term', '-')}, 量能 {trend.get('volume_state', '-')}"
        )
    if name == "calculate_ma":
        return f"完成均线计算：{result.get('ma_alignment', '-')}，价格在 {result.get('above_ma_count', 0)}/{result.get('total_ma_count', 0)} 条均线上方"
    if name == "get_volume_analysis":
        return f"完成量价分析：{result.get('pattern', '-')}，量能趋势 {result.get('volume_trend', '-')}"
    if name == "get_chip_distribution":
        return (
            f"完成筹码分布：获利比例 {result.get('winner_rate', '-')}, "
            f"平均成本 {result.get('avg_cost', '-')}, 90%成本区间 {result.get('cost_5pct', '-')}-{result.get('cost_95pct', '-')}"
        )
    if name == "get_capital_flow":
        return (
            f"完成资金流分析：最新主力净流 {result.get('main_net_amount', '-')}, "
            f"5日 {result.get('main_net_amount_5d', '-')}, 10日 {result.get('main_net_amount_10d', '-')}"
        )
    if name == "get_stock_info":
        return (
            f"完成基本面获取：{result.get('name', result.get('symbol', '-'))}, "
            f"行业 {result.get('industry', '-')}, PE {result.get('pe', '-')}, PB {result.get('pb', '-')}"
        )
    if name == "get_analysis_context":
        if not result.get("has_context"):
            return "本地暂无已缓存分析上下文，需要继续获取历史K线。"
        return (
            f"读取本地上下文：{result.get('bar_count', 0)} 条K线，"
            f"{result.get('start_trade_date', '-')}-{result.get('end_trade_date', '-')}"
        )
    if name == "analyze_pattern":
        return f"完成形态识别：发现 {result.get('patterns_count', 0)} 个形态，{result.get('summary', '-')}"
    if name in {"search_stock_news", "search_market_intel", "search_comprehensive_intel"}:
        rows = result.get("results") or []
        return f"Tavily 返回 {len(rows)} 条搜索结果。"
    return "工具调用完成。"
