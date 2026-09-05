from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from backend.app.core.api_keys import TAVILY_POOL_PROVIDER, merge_api_keys
from backend.app.core.database import engine
from backend.app.core.settings import settings
from backend.app.market.tushare_catalog import (
    TUSHARE_API_SET,
    by_api_name,
    by_category_scope as catalog_by_category_scope,
    category_index as catalog_category_index,
)


TUSHARE_URL = "https://api.tushare.pro"
TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_DAILY_ROWS = 180
MAX_MINUTE_ROWS = 8000
_STOCK_BASIC_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": []}
_TAVILY_KEY_LOCK = threading.Lock()
_TAVILY_ACTIVE_KEY_INDEX = 0
_TAVILY_RETRYABLE_STATUS_CODES = frozenset(
    {401, 402, 403, 429, 432, 433, 500, 502, 503, 504}
)


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
            "name": "get_latest_price",
            "description": "Get the latest price of one A-share stock from the stock_latest_prices DB cache (auto-refreshed from TuShare during market hours; cache-only when market is closed). MUST be called first before answering any question about a specific stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code or ts_code, e.g. 600519.SH."},
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
    {
        "type": "function",
        "function": {
            "name": "tushare_query",
            "description": "通用 TuShare 接口透传：调用聚焦 A 股核心的任一已收录接口（约 150 个，覆盖股票/行情/财务/参考/特色/两融/资金流/打板/指数/宏观/语料/ETF/可转债）。先 list_tushare_apis 查可用接口与参数，再调用本工具。返回行数内部截断到 300 行并标记 truncated。若某接口有专用语义工具（见目录 semantic_tool 字段），优先用专用工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "api_name": {"type": "string", "description": "TuShare 接口标识，如 top_list / margin / share_float / index_daily / cn_cpi。必须是 list_tushare_apis 返回的值。"},
                    "params": {"type": "object", "description": "接口参数对象，如 {\"ts_code\":\"600519.SH\",\"trade_date\":\"20260711\"}。仅支持标量值。"},
                    "fields": {"type": "string", "description": "返回字段，逗号分隔，如 \"trade_date,close,pct_chg\"。留空返回全部字段。"},
                    "limit": {"type": "integer", "description": "最大返回行数，默认 300，上限 500（内部硬截断 300）。"},
                },
                "required": ["api_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tushare_apis",
            "description": "列出可用 TuShare 接口目录。无参时返回分类索引；指定 category 时返回该分类下接口的 api_name/中文名/参数/是否有专用语义工具。用于在调用 tushare_query 前确认接口名与参数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "分类，如 基础数据/行情数据/财务数据/参考数据/特色数据/两融及转融通/资金流向数据/打板专题数据/指数专题/国内宏观/国际宏观/大模型语料/ETF专题/可转债。留空返回分类索引。"},
                    "scope": {"type": "string", "description": "可选语义过滤：stock/market/finance/macro/news/etf/cb。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_cal",
            "description": "TuShare 交易日历（trade_cal）：开市/休市标记，判断某日是否交易日、上下一个交易日。时间轴操作基座。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 120 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                    "exchange": {"type": "string", "description": "交易所，如 SSE/SZSE/CFFEX，留空返回全市场。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_suspend",
            "description": "TuShare 每日停复牌信息（suspend_d）：停牌是交易红线条件，停牌期间行情工具返回空或无意义数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码或 ts_code，如 600519。"},
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "resume_date": {"type": "string", "description": "复牌日 YYYYMMDD。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stk_limit",
            "description": "TuShare 每日涨跌停价（stk_limit）：科创/创业±20、北交±30、ST±5，计算目标价/止损价用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码或 ts_code。"},
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_list",
            "description": "TuShare 龙虎榜每日明细（top_list）：上榜股票、买卖额、净额。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD，不传则取最近。"},
                    "symbol": {"type": "string", "description": "可选 A 股代码或 ts_code，查该股上榜记录。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hm_detail",
            "description": "TuShare 游资交易每日明细（hm_detail）：游资席位买卖明细，配龙虎榜分析。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 10 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_margin",
            "description": "TuShare 融资融券交易汇总（margin）：融资余额/融券余额等。trade_date 与 start/end 二选一。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "exchange_id": {"type": "string", "description": "交易所代码。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 30 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_moneyflow_hsgt",
            "description": "TuShare 沪深港通资金流向（moneyflow_hsgt）：北向资金净流入，大势/外资判断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 30 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "TuShare 业绩预告（forecast）：预告类型、变动幅度、净利区间、摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码或 ts_code。"},
                    "period": {"type": "string", "description": "报告期 YYYYMMDD，如 20251231。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 540 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_income",
            "description": "TuShare 利润表（income）：营收/净利润/EBIT/所得税等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码或 ts_code。"},
                    "period": {"type": "string", "description": "报告期 YYYYMMDD，如 20251231。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 540 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_index_daily",
            "description": "TuShare 指数日线行情（index_daily）：ts_code 为指数代码，如 000001.SH 上证综指、399001.SZ 深证成指。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "指数 ts_code，如 000001.SH。"},
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 240 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sw_daily",
            "description": "TuShare 申万行业日线行情（sw_daily）：ts_code 为申万指数代码，如 801010.SI 综合、801080.SI 电子。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "申万指数 ts_code，如 801010.SI。"},
                    "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD，默认今日前推 240 天。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD，默认今日。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cn_cpi",
            "description": "TuShare 居民消费价格指数（cn_cpi）：同比/环比，宏观通胀判断。start_m/end_m 为 YYYYMM 月份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_m": {"type": "string", "description": "起始月份 YYYYMM，默认今日前推 12 个月。"},
                    "end_m": {"type": "string", "description": "结束月份 YYYYMM，默认本月。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tushare_minutes",
            "description": "TuShare 历史分钟行情（stk_mins）：1/5/15/30/60min 频率，盘中结构分析用。需较高积分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码或 ts_code。"},
                    "freq": {"type": "string", "description": "频率：1min/5min/15min/30min/60min，默认 5min。"},
                    "start_date": {"type": "string", "description": "起始 YYYYMMDD HH:MM:SS 或 YYYYMMDD。"},
                    "end_date": {"type": "string", "description": "结束 YYYYMMDD HH:MM:SS 或 YYYYMMDD。"},
                    "limit": {"type": "integer", "description": "最大行数，默认 800，上限 8000。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_inst",
            "description": "TuShare 龙虎榜机构交易单（top_inst）：机构席位买卖明细、净买额。",
            "parameters": {"type": "object", "properties": {"trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "symbol": {"type": "string", "description": "可选 A 股代码或 ts_code。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_limit_list",
            "description": "TuShare 涨跌停和炸板数据（limit_list_d）：涨跌停状态、炸板次数、首封/最后封板时间。查询具体股票时必须传 symbol，避免全市场结果截断。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "可选 A 股代码或 ts_code；分析具体股票涨停时必须传。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_limit_step",
            "description": "TuShare 涨停股票连板天梯（limit_step）：连板高度、连板次数。",
            "parameters": {"type": "object", "properties": {"trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_margin_detail",
            "description": "TuShare 融资融券交易明细（margin_detail）：个股融资余额/融券余额。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_share_float",
            "description": "TuShare 限售股解禁（share_float）：解禁日期/股数/金额/股东。默认查近 180 天。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_block_trade",
            "description": "TuShare 大宗交易（block_trade）：成交价/量/额/买卖方。",
            "parameters": {"type": "object", "properties": {"trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top10_holders",
            "description": "TuShare 前十大股东（top10_holders）：股东名/类型/持股数/比例。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top10_float_holders",
            "description": "TuShare 前十大流通股东（top10_floatholders）。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_holdertrade",
            "description": "TuShare 股东增减持（stk_holdertrade）：变动量/比例、变动后持股。默认近 180 天。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_holdernumber",
            "description": "TuShare 股东人数（stk_holdernumber）：股东人数及变化。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repurchase",
            "description": "TuShare 股票回购（repurchase）：回购进程/金额/数量/价格区间。默认近 365 天。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pledge_stat",
            "description": "TuShare 股权质押统计（pledge_stat）：质押次数/比例/未平仓。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "end_date": {"type": "string", "description": "截止 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_express",
            "description": "TuShare 业绩快报（express）：营收/净利润/同比/EPS。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dividend",
            "description": "TuShare 分红送股（dividend）：送转/现金分红/除权登记派息日。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balancesheet",
            "description": "TuShare 资产负债表（balancesheet）：总资产/负债/货币资金/存货等。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "period": {"type": "string", "description": "报告期 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cashflow",
            "description": "TuShare 现金流量表（cashflow）：经营/投资/筹资现金流、自由现金流。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "period": {"type": "string", "description": "报告期 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fina_mainbz",
            "description": "TuShare 主营业务构成（fina_mainbz）：按产品/地区拆分收入利润。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "period": {"type": "string", "description": "报告期 YYYYMMDD。"}, "bz_type": {"type": "string", "description": "类型，P 产品/D 地区。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_disclosure_date",
            "description": "TuShare 财报披露日期表（disclosure_date）：预约/实际/修改披露日。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "end_date": {"type": "string", "description": "报告期 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_index_weight",
            "description": "TuShare 指数成分和权重（index_weight）：symbol 为指数代码如 000300.SH。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "指数代码，如 000300.SH。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_index_member",
            "description": "TuShare 申万行业成分（index_member_all）：symbol 为申万指数代码如 801010.SI。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "申万指数代码，如 801010.SI。"}, "level": {"type": "string", "description": "行业分级，如 L1/L2/L3。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_info",
            "description": "TuShare 沪深市场每日交易统计（daily_info）：总市值/成交/平均 PE/PB。",
            "parameters": {"type": "object", "properties": {"trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report_rc",
            "description": "TuShare 券商盈利预测（report_rc）：分析师/预测值/评级/机构。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "report_date": {"type": "string", "description": "报告日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_anns",
            "description": "TuShare 上市公司公告（anns_d）：标题/类型/披露时间。默认近 30 天。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_namechange",
            "description": "TuShare 股票曾用名（namechange）：曾用名/更名日期/原因。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stk_factor",
            "description": "TuShare 股票技术面因子（stk_factor）：MACD/RSI/布林/CCI/KDJ。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": ["symbol"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ccass_hold",
            "description": "TuShare 中央结算系统持股统计（ccass_hold）：港股通持股集中度。",
            "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "A 股代码或 ts_code。"}, "trade_date": {"type": "string", "description": "交易日 YYYYMMDD。"}, "start_date": {"type": "string", "description": "起始 YYYYMMDD。"}, "end_date": {"type": "string", "description": "结束 YYYYMMDD。"}}, "required": []},
        },
    },
]


def stock_system_prompt(extra_context: str | None = None) -> str:
    context = f"\n\n当前用户与数字人上下文:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    shanghai_now = _sh_now()
    market_state = "开盘中" if _in_market_window(shanghai_now) else "非交易时段"
    return f"""你是 SixRabbits Stock 的问股助手——既是有温度、会聊天的人，也是严谨的A股问股专家，采用 ReAct 工具调用方式工作。当前上海时间: {shanghai_now.isoformat(timespec="minutes")}（{market_state}）

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


def stock_system_prompt_with_skills(extra_context: str | None = None, skills: list[str] | None = None, memory_context: str = "") -> str:
    context = f"\n\n当前用户与数字人上下文:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    memory_block = f"\n\n{memory_context.strip()}" if memory_context.strip() else ""
    selected_ids = set(skills or [])
    selected = [item for item in STOCK_STRATEGIES if item["id"] in selected_ids]
    strategy_block = (
        "\n".join(f"- {item['name']}({item['id']}): {item['instructions']}" for item in selected[:4])
        if selected
        else "- 通用分析: 未激活特定策略，按行情、技术、消息、风险的通用问股框架分析。"
    )
    # 技能补充规则：registry 汇总问股模式下各激活技能的行为规范（如工作区防注入）。
    # 懒加载 registry 避免与 skills 包的 import 环；基础规则已覆盖的技能其 prompt_instructions 留空。
    from backend.app.skills.registry import skill_registry
    extra_rules = skill_registry.get_prompt_instructions(chat_mode="stock")
    skill_rules_block = ("\n\n技能补充规则:\n" + "\n".join(f"- {r}" for r in extra_rules)) if extra_rules else ""
    return f"""你是 SixRabbits Stock 的问股助手——既是有温度、会聊天的人，也是严谨的A股问股专家，采用 ReAct 工具调用方式工作。今天日期: {date.today().isoformat()}

可用数据源:
- TuShare: A股基础信息、最近交易日行情、历史K线、筹码、资金流，只能通过股票工具调用获取。
- Tavily Search: 新闻、公告、市场叙事、行业政策和催化信息，只能通过搜索工具调用获取。

当前激活策略:
{strategy_block}
{memory_block}
对话原则（每次先判断）:
- 遇到寒暄、闲聊、情绪倾诉、生活话题或与股票无关的问题：就像朋友一样自然、亲切地直接回应，不查行情、不套问股框架、不调用任何工具，简短有人情味，可以适度共情、开个得体的玩笑。
- 只有遇到个股、行情、消息面、技术面、风险等专业问题时，才切到专业模式，按下面的工作规则用工具和证据严谨作答。
- 拿不准是闲聊还是提问时：先温和回应，再问一句对方想了解哪只票或什么问题。始终做“懂行又好相处的朋友”，不是冷冰冰的查询终端。

工作规则（仅适用于专业问股问题）:
1. 不要编造行情、新闻、财务或估值数据；缺少 Token 或数据源失败时必须明确说明。
2. 如果用户只给股票名称或不完整代码，先调用 resolve_stock。
3. 【必做】凡回答涉及具体股票的问题，先调用 get_latest_price 拿该标的最新价格（数据库缓存，开盘时段自动刷新、收盘后走缓存），回答中标注价格与所属交易日，绝不引用记忆里的旧价。
4. 参考 daily_stock_analysis 的问股路径：先 get_analysis_context，再 get_realtime_quote / get_daily_history，然后 analyze_trend / calculate_ma / get_volume_analysis / analyze_pattern；需要筹码、资金、基本面时继续调用 get_chip_distribution / get_capital_flow / get_stock_info。
5. 用户问消息面、催化、利空、近期发生了什么时，调用 search_stock_news；需要行业、政策、公告、研报、风险的综合情报时调用 search_comprehensive_intel。
6. 用户询问“今日/当前涨停、跌停、炸板、连板或异动”时，必须调用 get_limit_list，并同时传 symbol 与目标 trade_date。不能用上一交易日收盘价否定用户描述的当日盘中事件。
7. 若行情结果 stale=true、intraday_unavailable=true，或 trade_date 早于开盘中的当前交易日，只能说“当前数据源尚未覆盖今日盘中，无法核实”，严禁断言“今天没有涨停/没有异动”。
8. 最终回答用中文 Markdown，固定包含：标的识别、数据来源、技术/价格结构、筹码/资金/基本面、消息面、风险、观察动作。
9. 只能做研究辅助，不给保证收益、自动下单或无条件买卖结论。{skill_rules_block}{context}"""


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
    """Phase 2：转发到 skill_registry 执行。

    原 16 分支 if/elif 已由 backend/app/skills/ 下各技能文件的 executor 接管——它们调用
    同一批 stock_tools 函数、用同一套参数抽取（_optional_str / _int_arg / MAX_DAILY_ROWS），
    结果与原分发逐字一致。run_sql / get_schema 等基础工具不经此函数，仍在
    stock_agent.run_agent_events 内联处理，只读沙箱与 agent_audit.log 留痕不变。

    此处 lazy import 是为避免低层 stock_tools 在 import 期反向依赖 skills 包（skills 在
    架构上位于 stock_tools 之上）。首次调用后 importlib 会走 sys.modules 缓存，无开销。
    """
    from backend.app.skills.registry import skill_registry

    if skill_registry.has_tool(name):
        return skill_registry.execute(name, args)
    return {"error": f"Unknown stock tool: {name}"}

def resolve_stock(query: str) -> dict:
    key = query.strip()
    if not key:
        return {"error": "query is empty"}

    normalized = normalize_a_share_symbol(key)
    candidates: list[dict[str, Any]] = []
    local_candidates = _resolve_stock_from_local(key, normalized)
    key_lower = key.lower()
    exact_local = [
        item
        for item in local_candidates
        if (
            str(item.get("ts_code") or "") == normalized
            or str(item.get("symbol") or "") == key
            or str(item.get("name") or "").strip().lower() == key_lower
        )
    ]
    remote_candidates: list[dict[str, Any]] = []
    for row in _stock_basic_rows():
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
            remote_candidates.append(
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

    exact_remote = [
        item
        for item in remote_candidates
        if (
            str(item.get("ts_code") or "") == normalized
            or str(item.get("symbol") or "") == key
            or str(item.get("name") or "").strip().lower() == key_lower
        )
    ]
    if exact_remote:
        # Position payloads used to be able to overwrite instruments.name, so
        # an exact local name can be stale or poisoned.  The listed-security
        # catalog is authoritative whenever it has an exact match.
        candidates.extend(exact_remote)
    elif exact_local:
        candidates.extend(exact_local)
    else:
        candidates.extend(local_candidates)
        candidates.extend(remote_candidates)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        ts_code = str(item.get("ts_code") or item.get("symbol") or "")
        if ts_code and ts_code not in seen:
            seen.add(ts_code)
            deduped.append(item)
    return {
        "query": key,
        "normalized": normalized,
        "candidates": deduped[:8],
        "source": "tushare" if exact_remote else ("local" if exact_local else "local+tushare"),
    }


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


PRICE_CACHE_TTL_OPEN_MINUTES = 10       # 开盘时段缓存有效期，超时才实取 TuShare
PRICE_CACHE_CLOSED_RETRY_MINUTES = 30   # 收盘后缓存缺当日收盘价时的补取限频


def _sh_now() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in settings.stock_holidays


def _in_market_window(now: datetime) -> bool:
    """A股盘中窗口（含集合竞价与收盘缓冲）：交易日 09:15-15:05。"""
    if not _is_trading_day(now.date()):
        return False
    return (9, 15) <= (now.hour, now.minute) <= (15, 5)


def _latest_completed_trade_date(now: datetime) -> str:
    """最近一个已收盘的交易日（YYYYMMDD）：今天已收盘则为今天，否则回溯上一交易日。"""
    d = now.date()
    if _is_trading_day(d) and (now.hour, now.minute) >= (15, 5):
        return d.strftime("%Y%m%d")
    d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def get_latest_price(symbol: str) -> dict:
    """带数据库缓存的最新价查询（stock_latest_prices 表，全体 Agent 共用）。

    - 开盘时段（交易日 09:15-15:05）：缓存超过 TTL 才实取 TuShare 刷新。
    - 非开盘时间：缓存已含最近收盘交易日 → 直接用缓存不外呼；缺当日收盘价才限频补取一次。
    """
    ts_code = normalize_a_share_symbol(symbol)
    if "." not in ts_code:
        return {"error": f"无法识别的A股代码: {symbol}"}
    now = _sh_now()
    market_open = _in_market_window(now)

    row: dict | None = None
    try:
        with engine.connect() as conn:
            r = conn.execute(
                text(
                    "SELECT symbol, name, price, pre_close, pct_chg, trade_date, fetched_at "
                    "FROM stock_latest_prices WHERE symbol = :s"
                ),
                {"s": ts_code},
            ).mappings().first()
            row = dict(r) if r else None
    except Exception:
        row = None

    if not _price_needs_refresh(row, now, market_open):
        return _latest_price_out(row, market_open=market_open, from_cache=True)

    daily = get_tushare_daily(ts_code, limit=2)
    bars = daily.get("bars") or [] if isinstance(daily, dict) else []
    if "error" in daily or not bars:
        if row:  # 实取失败回退缓存，标注 stale
            out = _latest_price_out(row, market_open=market_open, from_cache=True)
            out["stale"] = True
            out["refresh_error"] = str(daily.get("error") or "无行情数据")[:200]
            return out
        return {"error": f"获取最新价失败: {daily.get('error') or '无行情数据'}"}

    latest, previous = bars[-1], (bars[-2] if len(bars) >= 2 else {})
    info = _stock_basic_item(ts_code) or {}
    price = latest.get("close")
    pre_close = latest.get("pre_close") or previous.get("close")
    pct_chg = latest.get("pct_chg")
    if pct_chg is None and pre_close and price is not None:
        pct_chg = _pct(float(price), float(pre_close))
    fresh = {
        "symbol": ts_code,
        "name": info.get("name") or (row or {}).get("name"),
        "price": price,
        "pre_close": pre_close,
        "pct_chg": pct_chg,
        "trade_date": str(latest.get("trade_date") or ""),
        "fetched_at": now,
    }
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO stock_latest_prices (symbol, name, price, pre_close, pct_chg, trade_date, source, fetched_at) "
                    "VALUES (:symbol, :name, :price, :pre_close, :pct_chg, :trade_date, 'tushare', :fetched_at) "
                    "ON DUPLICATE KEY UPDATE name = VALUES(name), price = VALUES(price), pre_close = VALUES(pre_close), "
                    "pct_chg = VALUES(pct_chg), trade_date = VALUES(trade_date), source = VALUES(source), fetched_at = VALUES(fetched_at)"
                ),
                fresh,
            )
    except Exception:
        pass  # 缓存写入失败不影响本次返回
    return _latest_price_out(fresh, market_open=market_open, from_cache=False)


def _price_needs_refresh(row: dict | None, now: datetime, market_open: bool) -> bool:
    if not row or row.get("price") is None:
        return True
    fetched_at = row.get("fetched_at")
    if not isinstance(fetched_at, datetime):
        return True
    if market_open:
        return (now - fetched_at) > timedelta(minutes=PRICE_CACHE_TTL_OPEN_MINUTES)
    if str(row.get("trade_date") or "") >= _latest_completed_trade_date(now):
        return False  # 非开盘时间且已有最近收盘价：不外呼
    return (now - fetched_at) > timedelta(minutes=PRICE_CACHE_CLOSED_RETRY_MINUTES)


def _latest_price_out(row: dict, *, market_open: bool, from_cache: bool) -> dict:
    fetched_at = row.get("fetched_at")
    now = _sh_now()
    expected_trade_date = (
        now.strftime("%Y%m%d")
        if market_open
        else _latest_completed_trade_date(now)
    )
    trade_date = str(row.get("trade_date") or "")
    stale = not trade_date or trade_date < expected_trade_date
    intraday_unavailable = bool(market_open and stale)
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "price": row.get("price"),
        "pre_close": row.get("pre_close"),
        "pct_chg": row.get("pct_chg"),
        "trade_date": trade_date,
        "fetched_at": fetched_at.isoformat(timespec="seconds") if isinstance(fetched_at, datetime) else str(fetched_at or ""),
        "market_open": market_open,
        "from_cache": from_cache,
        "expected_trade_date": expected_trade_date,
        "stale": stale,
        "intraday_unavailable": intraday_unavailable,
        "freshness_note": (
            f"当前处于交易时段，但数据仅到 {trade_date or '未知日期'}；"
            "不能据此判断今日盘中是否涨停、跌停、炸板或发生异动。"
            if intraday_unavailable
            else (
                f"行情日期早于最近应有交易日 {expected_trade_date}。"
                if stale
                else "行情日期已覆盖当前应有交易日。"
            )
        ),
        "source": "stock_latest_prices(tushare)",
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
    now = _sh_now()
    market_open = _in_market_window(now)
    trade_date = str(latest.get("trade_date") or "")
    expected_trade_date = (
        now.strftime("%Y%m%d")
        if market_open
        else _latest_completed_trade_date(now)
    )
    stale = not trade_date or trade_date < expected_trade_date
    intraday_unavailable = bool(market_open and stale)
    return {
        "symbol": ts_code,
        "name": info.get("name"),
        "trade_date": trade_date,
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
        "market_open": market_open,
        "expected_trade_date": expected_trade_date,
        "stale": stale,
        "intraday_unavailable": intraday_unavailable,
        "freshness_note": (
            f"当前处于交易时段，但该快照仅到 {trade_date or '未知日期'}，"
            "不是今日盘中实时行情，不能用于否定用户描述的当日涨跌停或异动。"
            if intraday_unavailable
            else "TuShare 当前接入为最近交易日快照，不保证盘中实时。"
        ),
    }


def get_realtime_quotes(symbols: list[str] | tuple[str, ...]) -> dict:
    """批量获取 A 股盘中最新价，并刷新全局最新价缓存。

    TuShare ``rt_k`` 支持一次查询多个代码。持仓分钟同步使用此函数，避免按账户、
    按标的重复请求。部分标的缺失时返回 ``missing_symbols``，整批请求失败时返回
    ``error``，调用方可保留上一轮价格而不写入空值。
    """
    requested: list[str] = []
    for raw_symbol in symbols:
        symbol = normalize_a_share_symbol(str(raw_symbol or ""))
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) and symbol not in requested:
            requested.append(symbol)
    if not requested:
        return {
            "source": "tushare.rt_k",
            "fetched_at": _sh_now().isoformat(timespec="seconds"),
            "requested_count": 0,
            "quote_count": 0,
            "quotes": {},
            "rows": [],
            "missing_symbols": [],
            "ms": 0,
        }

    fields = (
        "ts_code,name,pre_close,high,open,low,close,vol,amount,num,"
        "ask_price1,ask_volume1,bid_price1,bid_volume1,trade_time"
    )
    started = time.monotonic()
    raw_rows: list[dict[str, Any]] = []
    for offset in range(0, len(requested), 100):
        chunk = requested[offset : offset + 100]
        result = _tushare_request("rt_k", {"ts_code": ",".join(chunk)}, fields)
        if "error" in result:
            return {
                "error": result["error"],
                "source": "tushare.rt_k",
                "requested_count": len(requested),
                "quote_count": 0,
                "missing_symbols": requested,
                "ms": int((time.monotonic() - started) * 1000),
            }
        raw_rows.extend(result.get("rows") or [])

    fetched_at = _sh_now()
    quotes: dict[str, dict[str, Any]] = {}
    cache_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
        price = _float(row.get("close"))
        if symbol not in requested or price is None or price <= 0:
            continue
        pre_close = _float(row.get("pre_close"))
        pct_chg = _pct(price, pre_close) if pre_close else None
        trade_time = str(row.get("trade_time") or "").strip()
        trade_date = (
            trade_time[:10].replace("-", "")
            if re.match(r"^\d{4}-\d{2}-\d{2}", trade_time)
            else fetched_at.strftime("%Y%m%d")
        )
        quote = {
            "symbol": symbol,
            "name": row.get("name"),
            "trade_date": trade_date,
            "trade_time": trade_time,
            "price": price,
            "pre_close": pre_close,
            "pct_chg": pct_chg,
            "open": _float(row.get("open")),
            "high": _float(row.get("high")),
            "low": _float(row.get("low")),
            "volume": _float(row.get("vol")),
            "amount": _float(row.get("amount")),
            "source": "tushare.rt_k",
            "is_realtime": True,
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
        }
        quotes[symbol] = quote
        cache_rows.append(
            {
                "symbol": symbol,
                "name": row.get("name"),
                "price": price,
                "pre_close": pre_close,
                "pct_chg": pct_chg,
                "trade_date": trade_date,
                "source": "tushare.rt_k",
                "fetched_at": fetched_at,
            }
        )

    if cache_rows:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO stock_latest_prices "
                        "(symbol, name, price, pre_close, pct_chg, trade_date, source, fetched_at) "
                        "VALUES (:symbol, :name, :price, :pre_close, :pct_chg, :trade_date, :source, :fetched_at) "
                        "ON DUPLICATE KEY UPDATE name = VALUES(name), price = VALUES(price), "
                        "pre_close = VALUES(pre_close), pct_chg = VALUES(pct_chg), "
                        "trade_date = VALUES(trade_date), source = VALUES(source), "
                        "fetched_at = VALUES(fetched_at), updated_at = VALUES(fetched_at)"
                    ),
                    cache_rows,
                )
        except Exception as exc:  # noqa: BLE001
            return {
                "error": f"实时行情已获取，但最新价缓存写入失败: {str(exc)[:300]}",
                "source": "tushare.rt_k",
                "requested_count": len(requested),
                "quote_count": len(quotes),
                "missing_symbols": [symbol for symbol in requested if symbol not in quotes],
                "ms": int((time.monotonic() - started) * 1000),
            }

    missing_symbols = [symbol for symbol in requested if symbol not in quotes]
    return {
        "source": "tushare.rt_k",
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "requested_count": len(requested),
        "quote_count": len(quotes),
        "quotes": quotes,
        "rows": list(quotes.values()),
        "missing_symbols": missing_symbols,
        "ms": int((time.monotonic() - started) * 1000),
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
    # 查询改写+多路扩展：口语问句改写成多角度检索句，各自搜索后按 URL 去重合并，提高召回。
    # 懒加载避免 import 环；关闭/失败时 plan 退化为单查询，走与改造前一致的单次搜索路径。
    from backend.app.ai.query_rewrite import rewrite_and_expand

    plan = rewrite_and_expand(query)
    # 实体前缀/情报后缀只解析一次，逐条查询复用
    prefix, suffix = "", " A股 行业 板块 政策 公告 研报 风险"
    if symbol:
        normalized = normalize_a_share_symbol(symbol)
        info = _stock_basic_item(normalized) or {}
        prefix = f"{normalized} {info.get('name') or normalized} "
        suffix = " 行业 板块 政策 公告 研报 风险"

    if not plan.expanded:  # 单查询：与改造前逐字一致，省掉合并开销
        only = plan.queries[0] if plan.queries else query.strip()
        return search_stock_news(f"{prefix}{only}{suffix}", symbol=None, max_results=max_results)

    # 多查询：每条各搜 → 按 URL（退化用标题）去重合并，总量仍约束在 max_results
    per_query = max(2, max_results // len(plan.queries) + 1)
    merged: list[dict] = []
    seen: set[str] = set()
    last_error: str | None = None
    ms_total = 0
    for sub_query in plan.queries:
        res = search_stock_news(f"{prefix}{sub_query}{suffix}", symbol=None, max_results=per_query)
        if "error" in res:
            last_error = res["error"]
            continue
        ms_total += res.get("ms", 0)
        for item in res.get("results", []):
            key = item.get("url") or item.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if not merged and last_error:
        return {"error": last_error}
    return {
        "query": query,
        "queries": plan.queries,
        "source": "multi.search",
        "results": merged[:max_results],
        "ms": ms_total,
    }


def search_stock_news(query: str, symbol: str | None = None, max_results: int = 5) -> dict:
    keyword = query.strip()
    if symbol:
        keyword = f"{normalize_a_share_symbol(symbol)} {keyword}"
    if not keyword:
        return {"error": "query is empty"}
    resolved = _resolve_search_source()
    provider: str | None = None
    results: list[dict] = []
    primary_error: str | None = None
    started = time.monotonic()
    if resolved:
        provider, api_key, url = resolved
        try:
            if provider == "brave":
                results = _brave_search(url, api_key, keyword, max_results)
            else:
                results = _tavily_search(url, api_key, keyword, max_results)
        except Exception as exc:  # noqa: BLE001
            label = "Brave Search" if provider == "brave" else "Tavily Search"
            primary_error = f"{label} 调用失败: {str(exc)[:500]}"

    # 降级：主源未配置/调用失败/无结果时，走免密钥国内源 akshare
    if not results:
        try:
            results = _akshare_search(keyword, symbol, max_results)
        except Exception as exc:  # noqa: BLE001
            if primary_error is None:
                primary_error = f"akshare 降级失败: {str(exc)[:300]}"
        else:
            if results:
                provider = "akshare"

    if not results:
        return {
            "error": primary_error
            or "未配置联网搜索源（Tavily 或 Brave），且 akshare 降级无结果。请在数据源配置页填写 API Key 并启用。"
        }

    saved = _cache_documents(results, symbol=normalize_a_share_symbol(symbol) if symbol else None)
    return {
        "query": keyword,
        "source": f"{provider}.search",
        "results": results,
        "saved_rows": saved,
        "fetched_at": _sh_now().isoformat(timespec="seconds"),
        "ms": int((time.monotonic() - started) * 1000),
    }


def _resolve_search_source() -> tuple[str, str | tuple[str, ...], str] | None:
    """选出当前联网搜索源，返回 (provider, api_key, url)。

    优先级：启用且已配 Key 的 Brave > 启用且已配 Key 的 Tavily；都没有则返回 None。
    在数据源配置页禁用某个源（status≠enabled）时 _data_source_api_key 返回空，会自动回退到下一个。
    """
    brave_key = _data_source_api_key("brave", settings.brave_api_key)
    if brave_key:
        return "brave", brave_key, _data_source_base_url("brave", BRAVE_URL)
    tavily_keys = _data_source_api_keys("tavily", settings.tavily_api_keys)
    if tavily_keys:
        return "tavily", tavily_keys, _data_source_base_url("tavily", TAVILY_URL)
    return None


def _tavily_search(
    url: str,
    api_keys: str | tuple[str, ...] | list[str],
    keyword: str,
    max_results: int,
) -> list[dict]:
    """Search Tavily with sticky failover across the configured key pool."""
    keys = merge_api_keys(api_keys)
    if not keys:
        raise RuntimeError("Tavily API Key 未配置")

    global _TAVILY_ACTIVE_KEY_INDEX
    with _TAVILY_KEY_LOCK:
        start_index = _TAVILY_ACTIVE_KEY_INDEX % len(keys)
    ordered = tuple((start_index + offset) % len(keys) for offset in range(len(keys)))
    last_error: Exception | None = None

    for key_index in ordered:
        api_key = keys[key_index]
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
        except httpx.HTTPError:
            last_error = RuntimeError("Tavily Search 网络请求失败")
            continue

        if response.status_code in _TAVILY_RETRYABLE_STATUS_CODES:
            last_error = RuntimeError(f"Tavily Search 暂不可用（HTTP {response.status_code}）")
            continue
        response.raise_for_status()
        body = response.json()
        with _TAVILY_KEY_LOCK:
            _TAVILY_ACTIVE_KEY_INDEX = key_index
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
            for item in body.get("results") or []
        ]

    raise last_error or RuntimeError("Tavily Search 调用失败")


def _brave_search(url: str, api_key: str, keyword: str, max_results: int) -> list[dict]:
    count = max(1, min(int(max_results), 20))
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = client.get(
            url,
            params={"q": keyword, "count": count},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        response.raise_for_status()
        body = response.json()
    results: list[dict] = []
    for bucket in ("web", "news"):
        for item in (body.get(bucket) or {}).get("results") or []:
            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": _strip_html_tags(item.get("description") or item.get("snippet") or ""),
                    "score": None,
                }
            )
    return results[:count]


def _strip_html_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


_AKSHARE_STOP_TOKENS = {
    "A股", "行业", "板块", "政策", "公告", "研报", "风险", "股票",
    "今日", "重要", "新闻", "市场", "动态", "消息",
}


def _akshare_filter_tokens(keyword: str) -> list[str]:
    tokens = []
    for token in re.split(r"[\s,，、;；()（）]+", keyword or ""):
        token = token.strip()
        if len(token) < 2 or token in _AKSHARE_STOP_TOKENS:
            continue
        if re.fullmatch(r"\d{6}(\.(SH|SZ|BJ))?", token):
            continue
        tokens.append(token)
    return tokens


def _akshare_row_to_item(row: dict, title_key: str, content_key: str, url_key: str) -> dict:
    published = str(row.get("发布时间") or "").strip()
    content = str(row.get(content_key) or "").strip()
    return {
        "title": str(row.get(title_key) or "").strip(),
        "url": str(row.get(url_key) or ""),
        "content": f"[{published}] {content}" if published else content,
        "score": None,
    }


def _akshare_search(keyword: str, symbol: str | None, max_results: int) -> list[dict]:
    """降级搜索源（免密钥、国内直连）：Brave/Tavily 不可用或无结果时启用。

    带股票代码走东方财富个股新闻；否则取财联社电报流按关键词过滤
    （过滤无命中时返回最新电报，保证降级仍有国内财经新闻）。
    """
    import akshare as ak

    count = max(1, min(int(max_results), 20))
    code_match = re.search(r"\d{6}", symbol or keyword or "")
    if code_match:
        frame = ak.stock_news_em(symbol=code_match.group(0))
        records = frame.to_dict("records") if frame is not None else []
        items = [
            _akshare_row_to_item(row, "新闻标题", "新闻内容", "新闻链接")
            for row in records
        ]
    else:
        frame = ak.stock_info_global_cls(symbol="全部")
        records = frame.to_dict("records") if frame is not None else []
        items = [
            _akshare_row_to_item(row, "标题", "内容", "链接")
            for row in records
        ]
    tokens = _akshare_filter_tokens(keyword)
    if tokens:
        filtered = [i for i in items if any(t in i["title"] + i["content"] for t in tokens)]
        if filtered:
            items = filtered
    items = [i for i in items if i["title"] or i["content"]]
    return items[:count]


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


_TUSHARE_AUDIT_LOG = Path(__file__).resolve().parents[2] / "agent_audit.log"
_TUSHARE_FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_TUSHARE_QUERY_MAX_LIMIT = 500
_TUSHARE_QUERY_CUT = 300


def _tushare_audit(record: dict) -> None:
    """通用透传 tushare_query 的审计留痕，写同一 agent_audit.log（与 stock_agent._audit 同路径）。

    stock_tools 不能 import stock_agent（反向依赖），故在此独立实现；parents[2] 同样指向 backend/。
    """
    try:
        record["at"] = datetime.now().isoformat(timespec="seconds")
        with _TUSHARE_AUDIT_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def tushare_query(
    api_name: str,
    params: dict | None = None,
    fields: str | None = None,
    limit: int = 300,
) -> dict:
    """通用 TuShare 接口透传：白名单（TUSHARE_API_SET）+ 字段名校验 + 行数截断 + 审计。

    聚焦 A 股核心的任一已收录接口都可调用；未收录接口直接拒绝，避免 LLM 乱调。
    """
    api_name = (api_name or "").strip()
    if api_name not in TUSHARE_API_SET:
        return {
            "error": (
                f"未收录的 TuShare 接口: {api_name}"
                "（仅允许调用目录内聚焦 A 股核心的接口，先 list_tushare_apis 查可用接口）"
            )
        }
    entry = by_api_name(api_name) or {}

    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"error": "params 必须是对象/字典"}
    clean_params: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean_params[key] = value
        else:
            return {"error": f"params 字段 {key} 的值类型不被允许（仅支持标量）"}

    field_list: list[str] = []
    if fields:
        for chunk in re.split(r"\s*,\s*", str(fields)):
            name = chunk.strip()
            if not name:
                continue
            if not _TUSHARE_FIELD_RE.fullmatch(name):
                return {"error": f"字段名非法: {name}"}
            field_list.append(name)
    field_str = ",".join(field_list) if field_list else str(entry.get("default_fields") or "")

    try:
        requested = int(limit) if limit is not None else _TUSHARE_QUERY_CUT
    except (TypeError, ValueError):
        requested = _TUSHARE_QUERY_CUT
    cut = max(1, min(requested, _TUSHARE_QUERY_CUT))
    started = time.monotonic()
    response = _tushare_request(api_name, clean_params, field_str)
    ms = int((time.monotonic() - started) * 1000)

    if "error" in response:
        _tushare_audit({
            "tool": "tushare_query", "api_name": api_name, "params": clean_params,
            "fields": field_str, "ms": ms, "error": response["error"],
        })
        response["api_name"] = api_name
        response["name_cn"] = entry.get("name_cn")
        return response

    rows = response.get("rows") or []
    total = len(rows)
    truncated = total > cut
    result = {
        "api_name": api_name,
        "name_cn": entry.get("name_cn"),
        "params": clean_params,
        "fields": field_str,
        "count": total,
        "truncated": truncated,
        "rows": rows[:cut],
        "ms": ms,
        "source": f"tushare.{api_name}",
    }
    _tushare_audit({
        "tool": "tushare_query", "api_name": api_name, "params": clean_params,
        "fields": field_str, "rows": total, "truncated": truncated, "ms": ms,
    })
    return result


def list_tushare_apis(category: str | None = None, scope: str | None = None) -> dict:
    """列出 TuShare 接口目录：无参返回分类索引；指定 category 返回该分类精简条目。

    三层分类避免一次返回全量（~150 条）被 OBSERVATION_CHAR_LIMIT 7000 截成碎片。
    semantic_tool 字段运行时按 skill_registry 已注册工具过滤，避免标注未实现工具。
    """
    try:
        from backend.app.skills.registry import skill_registry
        is_registered = skill_registry.has_tool
    except Exception:  # noqa: BLE001
        def is_registered(_name: str) -> bool:
            return False

    if not category:
        return {
            "categories": catalog_category_index(),
            "total": len(TUSHARE_API_SET),
            "tip": "先用 list_tushare_apis(category=...) 查看某分类的接口；"
            "高价值接口可用专用语义工具（semantic_tool），其余用 tushare_query 通用透传。",
        }

    entries = catalog_by_category_scope(category, scope)
    items = []
    for entry in entries:
        semantic = entry.get("semantic_tool")
        if semantic and not is_registered(semantic):
            semantic = None
        items.append({
            "api_name": entry["api_name"],
            "name_cn": entry["name_cn"],
            "scope": entry["scope"],
            "params": entry.get("params") or [],
            "semantic_tool": semantic,
            "desc": entry.get("desc") or "",
        })
    return {
        "category": category,
        "scope": scope,
        "count": len(items),
        "items": items,
    }


def _valid_yyyymm(raw: str | None) -> str | None:
    """校验 YYYYMM 月份串（宏观接口 cn_cpi/cn_gdp/cn_ppi 等用 start_m/end_m）。"""
    if not raw:
        return None
    s = str(raw).strip()
    return s if re.fullmatch(r"\d{6}", s) else None


def _semantic_tushare(
    api_name: str,
    params: dict[str, Any],
    fields: str,
    limit: int = 200,
    name_cn: str | None = None,
    tool_name: str | None = None,
) -> dict:
    """语义封装共用底座：调 _tushare_request + 行数截断 + 审计 + 统一返回结构。

    各 get_xxx 封装只负责拼 params 与选 fields，截断/审计/错误透传由此处统一处理。
    """
    started = time.monotonic()
    response = _tushare_request(api_name, params, fields)
    ms = int((time.monotonic() - started) * 1000)
    if "error" in response:
        return response
    rows = response.get("rows") or []
    truncated = len(rows) > limit
    _tushare_audit({
        "tool": tool_name or api_name, "api_name": api_name, "params": params,
        "rows": len(rows), "truncated": truncated, "ms": ms,
    })
    return {
        "source": f"tushare.{api_name}",
        "name_cn": name_cn,
        "params": params,
        "count": len(rows),
        "truncated": truncated,
        "rows": rows[:limit],
        "ms": ms,
    }


# -------------------------------------------------------------------- 首批语义封装
# 每个封装：固定 api_name + 参数校验（个股走 normalize_a_share_symbol、日期 _valid_yyyymmdd、
# 月份 _valid_yyyymm）+ 默认字段 + 行数截断。字段名取核心通用列，运行时若 tushare 报字段
# 不存在再据实修正。

def get_trade_cal(start_date: str | None = None, end_date: str | None = None,
                  exchange: str | None = None) -> dict:
    """交易日历：开市/休市标记，时间轴操作基座。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=120)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    if exchange:
        params["exchange"] = str(exchange).upper()
    fields = "exchange,cal_date,is_open,pretrade_date"
    return _semantic_tushare("trade_cal", params, fields, limit=400, name_cn="交易日历", tool_name="get_trade_cal")


def get_suspend(ts_code: str | None = None, trade_date: str | None = None,
                resume_date: str | None = None) -> dict:
    """每日停复牌信息：交易红线条件。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    rd = _valid_yyyymmdd(resume_date)
    if rd:
        params["resume_date"] = rd
    fields = "ts_code,trade_date,suspend_reason,resume_date,ann_date"
    return _semantic_tushare("suspend_d", params, fields, name_cn="每日停复牌信息", tool_name="get_suspend")


def get_stk_limit(ts_code: str, trade_date: str | None = None,
                  start_date: str | None = None, end_date: str | None = None) -> dict:
    """每日涨跌停价：科创/创业±20、北交±30、ST±5。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    sd = _valid_yyyymmdd(start_date)
    if sd:
        params["start_date"] = sd
    ed = _valid_yyyymmdd(end_date)
    if ed:
        params["end_date"] = ed
    fields = "trade_date,ts_code,pre_close,up_limit,down_limit"
    return _semantic_tushare("stk_limit", params, fields, name_cn="每日涨跌停价格", tool_name="get_stk_limit")


def get_top_list(trade_date: str | None = None, ts_code: str | None = None) -> dict:
    """龙虎榜每日明细。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "trade_date,ts_code,name,close,pct_chg,change,amount,turnover_rate,net_amount,buy_amount,sell_amount"
    return _semantic_tushare("top_list", params, fields, name_cn="龙虎榜每日明细", tool_name="get_top_list")


def get_hm_detail(trade_date: str | None = None, start_date: str | None = None,
                  end_date: str | None = None) -> dict:
    """游资交易每日明细（席位买卖，配龙虎榜）。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=10)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    fields = "trade_date,ts_code,name,close,pct_chg,amount,reason,sm_name,sm_date,buy_amount,sell_amount,net_amount"
    return _semantic_tushare("hm_detail", params, fields, name_cn="游资交易每日明细", tool_name="get_hm_detail")


def get_margin(trade_date: str | None = None, exchange_id: str | None = None,
               start_date: str | None = None, end_date: str | None = None) -> dict:
    """融资融券交易汇总。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    if exchange_id:
        params["exchange_id"] = str(exchange_id)
    fields = "trade_date,exchange_id,rzye,rqye,rzmre,rqmcl,rzrqye"
    return _semantic_tushare("margin", params, fields, name_cn="融资融券交易汇总", tool_name="get_margin")


def get_moneyflow_hsgt(trade_date: str | None = None, start_date: str | None = None,
                       end_date: str | None = None) -> dict:
    """沪深港通资金流向（北向资金）。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "trade_date,ggt_ss,ggt_sz,ggt_total,hgt_ss,hgt_sz,hgt_total,north_money,south_money"
    return _semantic_tushare("moneyflow_hsgt", params, fields, name_cn="沪深港通资金流向", tool_name="get_moneyflow_hsgt")


def get_forecast(ts_code: str | None = None, period: str | None = None,
                 start_date: str | None = None, end_date: str | None = None) -> dict:
    """业绩预告。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    if period:
        params["period"] = str(period)
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=540)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,ann_date,end_date,type,p_change,net_profit,parent_netprofit,summary,change_reason"
    return _semantic_tushare("forecast", params, fields, name_cn="业绩预告", tool_name="get_forecast")


def get_income(ts_code: str, period: str | None = None, start_date: str | None = None,
               end_date: str | None = None) -> dict:
    """利润表。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    if period:
        params["period"] = str(period)
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=540)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,ann_date,end_date,revenue,n_income,n_income_parent,net_profit_excl_min_int_inc,ebit,ebitda,total_profit,income_tax"
    return _semantic_tushare("income", params, fields, limit=20, name_cn="利润表", tool_name="get_income")


def get_index_daily(ts_code: str, trade_date: str | None = None,
                    start_date: str | None = None, end_date: str | None = None,
                    limit: int = 90) -> dict:
    """指数日线行情（ts_code 为指数代码，如 000001.SH 上证综指）。结果自动缓存到 daily_bars。"""
    params: dict[str, Any] = {"ts_code": str(ts_code).strip().upper()}
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=240)).strftime("%Y%m%d")
    params["start_date"] = start
    params["end_date"] = end
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
    response = _semantic_tushare("index_daily", params, fields, limit=limit * 2, name_cn="指数日线行情", tool_name="get_index_daily")
    if "error" in response:
        return response
    rows = response.get("rows", [])
    bars = [_normalize_daily_row(row) for row in rows]
    bars = sorted((row for row in bars if row), key=lambda row: row["trade_date"])
    bars = bars[-limit:]
    saved = _cache_daily_bars(params["ts_code"], bars)
    return {
        "symbol": params["ts_code"],
        "source": "tushare.index_daily",
        "start_date": start,
        "end_date": end,
        "bars": bars,
        "rows": bars,  # 向后兼容：portfolio_backtest/backtest_engine 用 rows 读取
        "saved_rows": saved,
    }


def get_sw_daily(ts_code: str, trade_date: str | None = None,
                 start_date: str | None = None, end_date: str | None = None) -> dict:
    """申万行业日线行情（ts_code 为申万指数代码，如 801010.SI）。"""
    params: dict[str, Any] = {"ts_code": str(ts_code).strip().upper()}
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=240)).strftime("%Y%m%d")
    params["start_date"] = start
    params["end_date"] = end
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    fields = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
    return _semantic_tushare("sw_daily", params, fields, name_cn="申万日线行情", tool_name="get_sw_daily")


def get_cn_cpi(start_m: str | None = None, end_m: str | None = None) -> dict:
    """居民消费价格指数 CPI（start_m/end_m 为 YYYYMM 月份）。"""
    today = date.today()
    end = _valid_yyyymm(end_m) or today.strftime("%Y%m")
    start = _valid_yyyymm(start_m) or (today - timedelta(days=365)).strftime("%Y%m")
    params: dict[str, Any] = {"start_m": start, "end_m": end}
    fields = "month,nt_yoy,nt_mom,np_yoy,np_mom"
    return _semantic_tushare("cn_cpi", params, fields, name_cn="居民消费价格指数CPI", tool_name="get_cn_cpi")


# -------------------------------------------------------------------- 二批语义封装
# 个股/财务/参考/特色/指数/语料等高价值接口的语义封装；字段取核心通用列，运行时若 tushare
# 报字段不存在再据实修正。个股类走 normalize_a_share_symbol，指数/申万类直接传 ts_code。

def get_top_inst(trade_date: str | None = None, ts_code: str | None = None) -> dict:
    """龙虎榜机构交易单。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "trade_date,ts_code,exlain,side,buy_amount,buy_rate,sell_amount,sell_rate,net_buy_amount"
    return _semantic_tushare("top_inst", params, fields, name_cn="龙虎榜机构交易单", tool_name="get_top_inst")


def get_limit_list(
    trade_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    ts_code: str | None = None,
) -> dict:
    """涨跌停和炸板数据。"""
    params: dict[str, Any] = {}
    normalized_symbol = normalize_a_share_symbol(ts_code) if ts_code else None
    if normalized_symbol:
        params["ts_code"] = normalized_symbol
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=10)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "trade_date,ts_code,name,close,pct_chg,up_limit,down_limit,limit_status,first_break_time,last_break_time,open_times"
    result = _semantic_tushare(
        "limit_list_d",
        params,
        fields,
        name_cn="涨跌停和炸板数据",
        tool_name="get_limit_list",
    )
    if "error" in result:
        return result

    rows = result.get("rows") or []
    latest_trade_date = max(
        (str(row.get("trade_date") or "") for row in rows),
        default=None,
    )
    now = _sh_now()
    market_open = _in_market_window(now)
    requested_end = td or str(params.get("end_date") or "")
    current_intraday = bool(
        market_open and requested_end >= now.strftime("%Y%m%d")
    )
    return {
        **result,
        "symbol": normalized_symbol,
        "latest_trade_date": latest_trade_date,
        "market_open": market_open,
        "is_final": not current_intraday,
        "absence_is_conclusive": not current_intraday,
        "freshness_note": (
            "当前为交易时段，结果仅代表查询时点快照；空结果不能证明今日没有涨跌停，"
            "已有涨停记录也可能随后炸板。"
            if current_intraday
            else "查询区间已经结束，结果可按历史收盘口径解释。"
        ),
    }


def get_limit_step(trade_date: str | None = None) -> dict:
    """涨停股票连板天梯。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    fields = "trade_date,ts_code,name,close,pct_chg,limit_times,up_stat,limit_times_max"
    return _semantic_tushare("limit_step", params, fields, name_cn="涨停股票连板天梯", tool_name="get_limit_step")


def get_margin_detail(ts_code: str | None = None, trade_date: str | None = None,
                      start_date: str | None = None, end_date: str | None = None) -> dict:
    """融资融券交易明细。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,rzye,rqye,rzmre,rqmcl,rzrqye"
    return _semantic_tushare("margin_detail", params, fields, name_cn="融资融券交易明细", tool_name="get_margin_detail")


def get_share_float(ts_code: str, start_date: str | None = None,
                    end_date: str | None = None) -> dict:
    """限售股解禁。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code), "start_date": start, "end_date": end}
    fields = "ts_code,float_date,float_share,float_amount,holder_name,holder_type"
    return _semantic_tushare("share_float", params, fields, name_cn="限售股解禁", tool_name="get_share_float")


def get_block_trade(trade_date: str | None = None, ts_code: str | None = None,
                    start_date: str | None = None, end_date: str | None = None) -> dict:
    """大宗交易。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    if not td:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "trade_date,ts_code,name,price,vol,amount,buyer,seller"
    return _semantic_tushare("block_trade", params, fields, name_cn="大宗交易", tool_name="get_block_trade")


def get_top10_holders(ts_code: str, trade_date: str | None = None,
                      start_date: str | None = None, end_date: str | None = None) -> dict:
    """前十大股东。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,holder_name,holder_type,hold_amount,hold_ratio"
    return _semantic_tushare("top10_holders", params, fields, name_cn="前十大股东", tool_name="get_top10_holders")


def get_top10_float_holders(ts_code: str, trade_date: str | None = None,
                             start_date: str | None = None, end_date: str | None = None) -> dict:
    """前十大流通股东。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,holder_name,hold_amount,hold_ratio"
    return _semantic_tushare("top10_floatholders", params, fields, name_cn="前十大流通股东", tool_name="get_top10_float_holders")


def get_holdertrade(ts_code: str | None = None, start_date: str | None = None,
                    end_date: str | None = None) -> dict:
    """股东增减持。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "ts_code,trade_date,holder_name,holder_type,change_amount,change_ratio,after_share,after_ratio"
    return _semantic_tushare("stk_holdertrade", params, fields, name_cn="股东增减持", tool_name="get_holdertrade")


def get_holdernumber(ts_code: str | None = None, trade_date: str | None = None,
                     start_date: str | None = None, end_date: str | None = None) -> dict:
    """股东人数。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,holder_num,holder_num_change"
    return _semantic_tushare("stk_holdernumber", params, fields, name_cn="股东人数", tool_name="get_holdernumber")


def get_repurchase(ts_code: str | None = None, start_date: str | None = None,
                   end_date: str | None = None) -> dict:
    """股票回购。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=365)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "ts_code,ann_date,proc,amount,volume,price_range_low,price_range_high"
    return _semantic_tushare("repurchase", params, fields, name_cn="股票回购", tool_name="get_repurchase")


def get_pledge_stat(ts_code: str, end_date: str | None = None) -> dict:
    """股权质押统计。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    ed = _valid_yyyymmdd(end_date)
    if ed:
        params["end_date"] = ed
    fields = "ts_code,end_date,pledge_count,pledge_amount,pledge_ratio,unrest_pledge_count,unrest_pledge_amount"
    return _semantic_tushare("pledge_stat", params, fields, name_cn="股权质押统计", tool_name="get_pledge_stat")


def get_express(ts_code: str | None = None, start_date: str | None = None,
                end_date: str | None = None) -> dict:
    """业绩快报。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=540)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,ann_date,end_date,revenue,n_income,total_profit,yoy_sales,yoy_profit,bps,diluted_eps"
    return _semantic_tushare("express", params, fields, limit=20, name_cn="业绩快报", tool_name="get_express")


def get_dividend(ts_code: str) -> dict:
    """分红送股。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    fields = "ts_code,end_date,ann_date,div_proc,stk_div,stk_boars,cash_div,cash_div_tax,ex_date,record_date,pay_date"
    return _semantic_tushare("dividend", params, fields, limit=30, name_cn="分红送股", tool_name="get_dividend")


def get_balancesheet(ts_code: str, period: str | None = None, start_date: str | None = None,
                     end_date: str | None = None) -> dict:
    """资产负债表。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    if period:
        params["period"] = str(period)
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=540)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,ann_date,end_date,total_assets,total_liab,total_cur_assets,total_cur_liab,total_nca,total_ncl,money_cap,accounts_rece,inventories"
    return _semantic_tushare("balancesheet", params, fields, limit=20, name_cn="资产负债表", tool_name="get_balancesheet")


def get_cashflow(ts_code: str, period: str | None = None, start_date: str | None = None,
                 end_date: str | None = None) -> dict:
    """现金流量表。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    if period:
        params["period"] = str(period)
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=540)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,ann_date,end_date,n_cashflow_act,n_cashflow_inv,n_cash_flows_finance,c_cash_equ_end_period,free_cashflow"
    return _semantic_tushare("cashflow", params, fields, limit=20, name_cn="现金流量表", tool_name="get_cashflow")


def get_fina_mainbz(ts_code: str, period: str | None = None, bz_type: str | None = None) -> dict:
    """主营业务构成（产品/地区）。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    if period:
        params["period"] = str(period)
    if bz_type:
        params["type"] = str(bz_type)
    fields = "ts_code,period,type,mainbz,bz_profit,bz_item"
    return _semantic_tushare("fina_mainbz", params, fields, limit=40, name_cn="主营业务构成", tool_name="get_fina_mainbz")


def get_disclosure_date(ts_code: str | None = None, end_date: str | None = None) -> dict:
    """财报披露日期表。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    if end_date:
        params["end_date"] = str(end_date)
    fields = "ts_code,name,end_date,pre_date,actual_date,modify_date"
    return _semantic_tushare("disclosure_date", params, fields, limit=30, name_cn="财报披露日期表", tool_name="get_disclosure_date")


def get_index_weight(ts_code: str, trade_date: str | None = None,
                     start_date: str | None = None, end_date: str | None = None) -> dict:
    """指数成分和权重。"""
    params: dict[str, Any] = {"index_code": str(ts_code).strip().upper()}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=180)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "index_code,con_code,trade_date,weight"
    return _semantic_tushare("index_weight", params, fields, name_cn="指数成分和权重", tool_name="get_index_weight")


def get_index_member(ts_code: str, level: str | None = None) -> dict:
    """申万行业成分（分级）。"""
    params: dict[str, Any] = {"index_code": str(ts_code).strip().upper()}
    if level:
        params["level"] = str(level)
    fields = "index_code,index_name,member_code,member_name,weight,level"
    return _semantic_tushare("index_member_all", params, fields, name_cn="申万行业成分", tool_name="get_index_member")


def get_daily_info(trade_date: str | None = None, start_date: str | None = None,
                   end_date: str | None = None) -> dict:
    """沪深市场每日交易统计。"""
    params: dict[str, Any] = {}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "trade_date,total_share,total_value,total_value_trans,avg_pe,avg_pb"
    return _semantic_tushare("daily_info", params, fields, name_cn="沪深市场每日交易统计", tool_name="get_daily_info")


def get_report_rc(ts_code: str | None = None, report_date: str | None = None,
                   start_date: str | None = None, end_date: str | None = None) -> dict:
    """券商盈利预测数据。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    rd = _valid_yyyymmdd(report_date)
    if rd:
        params["report_date"] = rd
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=365)).strftime("%Y%m%d")
    params.setdefault("start_date", start)
    params.setdefault("end_date", end)
    fields = "ts_code,report_date,analyst,forecast_value,forecast_type,org_name,rating"
    return _semantic_tushare("report_rc", params, fields, name_cn="券商盈利预测数据", tool_name="get_report_rc")


def get_anns(ts_code: str | None = None, start_date: str | None = None,
             end_date: str | None = None) -> dict:
    """上市公司公告（标题/摘要）。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "ts_code,ann_date,ann_title,ann_type,realtime"
    return _semantic_tushare("anns_d", params, fields, name_cn="上市公司公告", tool_name="get_anns")


def get_namechange(ts_code: str | None = None, start_date: str | None = None,
                   end_date: str | None = None) -> dict:
    """股票曾用名/更名。"""
    today = date.today()
    end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
    start = _valid_yyyymmdd(start_date) or (today - timedelta(days=365)).strftime("%Y%m%d")
    params: dict[str, Any] = {"start_date": start, "end_date": end}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    fields = "ts_code,name,start_date,end_date,change_reason"
    return _semantic_tushare("namechange", params, fields, name_cn="股票曾用名", tool_name="get_namechange")


def get_stk_factor(ts_code: str, trade_date: str | None = None,
                   start_date: str | None = None, end_date: str | None = None) -> dict:
    """股票技术面因子（量化因子）。"""
    params: dict[str, Any] = {"ts_code": normalize_a_share_symbol(ts_code)}
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=60)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,macd_d,rsi_5,rsi_10,rsi_20,boll_upper,boll_mid,boll_lower,cci,kdj_k,kdj_d,kdj_j"
    return _semantic_tushare("stk_factor", params, fields, name_cn="股票技术面因子", tool_name="get_stk_factor")


def get_ccass_hold(ts_code: str | None = None, trade_date: str | None = None,
                   start_date: str | None = None, end_date: str | None = None) -> dict:
    """中央结算系统持股统计。"""
    params: dict[str, Any] = {}
    if ts_code:
        params["ts_code"] = normalize_a_share_symbol(ts_code)
    td = _valid_yyyymmdd(trade_date)
    if td:
        params["trade_date"] = td
    else:
        today = date.today()
        end = _valid_yyyymmdd(end_date) or today.strftime("%Y%m%d")
        start = _valid_yyyymmdd(start_date) or (today - timedelta(days=30)).strftime("%Y%m%d")
        params["start_date"] = start
        params["end_date"] = end
    fields = "ts_code,trade_date,name,his_cap,share_rate"
    return _semantic_tushare("ccass_hold", params, fields, name_cn="中央结算系统持股统计", tool_name="get_ccass_hold")


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
    keys = _data_source_api_keys(provider, env_value)
    return keys[0] if keys else ""


def _data_source_api_keys(
    provider: str,
    env_values: str | tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    row = _data_source_row(provider)
    if row and str(row.get("status") or "enabled") != "enabled":
        return ()
    db_keys = str(row.get("api_key") or "").strip() if row else ""
    pool_keys = ""
    if provider == "tavily":
        pool_row = _data_source_row(TAVILY_POOL_PROVIDER)
        if pool_row and str(pool_row.get("status") or "enabled") == "enabled":
            pool_keys = str(pool_row.get("api_key") or "").strip()
    return merge_api_keys(db_keys, pool_keys, env_values)


def _data_source_base_url(provider: str, default_url: str) -> str:
    row = _data_source_row(provider)
    if row and str(row.get("base_url") or "").strip():
        return str(row["base_url"]).strip()
    return default_url


def _stock_basic_rows() -> list[dict[str, Any]]:
    now = time.monotonic()
    if _STOCK_BASIC_CACHE["expires_at"] > now:
        return list(_STOCK_BASIC_CACHE["rows"])
    fields = "ts_code,symbol,name,fullname,market,exchange,industry,list_date"
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


def lookup_a_share_instrument(ts_code: str) -> dict[str, Any]:
    """Look up one listed A-share in the authoritative TuShare stock catalog."""
    normalized = normalize_a_share_symbol(ts_code)
    rows = _stock_basic_rows()
    if not rows:
        return {
            "available": False,
            "normalized_symbol": normalized,
            "instrument": None,
        }
    item = next(
        (row for row in rows if str(row.get("ts_code") or "") == normalized),
        None,
    )
    return {
        "available": True,
        "normalized_symbol": normalized,
        "instrument": item,
    }


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
    exchange = {"BJ": "BSE"}.get(exchange, exchange)  # 北交所统一记 BSE，与前端过滤一致
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
    "get_latest_price": "查询最新价格",
    "tushare_query": "TuShare 数据查询",
    "list_tushare_apis": "查询数据接口目录",
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
    # Agent 工作区（registry 技能）：展示名的单一来源，聊天双模式 + 能力页共用
    "ws_read_file": "读工作区文件",
    "ws_write_file": "写工作区文件",
    "ws_edit_file": "改工作区文件",
    "ws_glob": "查找工作区文件",
    "ws_grep": "搜索工作区内容",
    "ws_run_python": "执行工作区脚本",
}


def stock_tool_display_name(name: str) -> str:
    return _STOCK_TOOL_DISPLAY_NAMES.get(name, name)


def stock_observation_preview(name: str, result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "get_latest_price":
        origin = "缓存" if result.get("from_cache") else "TuShare 实取"
        preview = (
            f"{result.get('name') or result.get('symbol')} 最新价 {result.get('price')}"
            f"（交易日 {result.get('trade_date')}，涨跌 {result.get('pct_chg')}%，{origin}）"
        )
        if result.get("intraday_unavailable"):
            preview += (
                f"。⚠️ 当前开盘中，但行情仅到 {result.get('trade_date') or '未知日期'}；"
                "不能据此否认今日盘中涨停、跌停或异动。"
            )
        elif result.get("stale"):
            preview += f"。⚠️ 数据落后于应有交易日 {result.get('expected_trade_date')}。"
        return preview
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
        preview = (
            f"完成行情快照：{result.get('symbol', '-')} {result.get('name', '')} "
            f"{result.get('trade_date', '-')} 价格 {result.get('price', '-')}, "
            f"涨跌幅 {result.get('pct_chg', '-')}%, 量比 {result.get('volume_ratio', '-')}"
        )
        if result.get("intraday_unavailable"):
            preview += "。⚠️ 这不是今日盘中实时行情，不能用于否定用户描述的当日异动。"
        return preview
    if name == "get_limit_list":
        rows = result.get("rows") or []
        symbol = result.get("symbol")
        if rows:
            labels = "、".join(
                f"{row.get('trade_date', '-')} {row.get('ts_code', '-')} "
                f"{row.get('name', '')} {row.get('pct_chg', '-')}%"
                for row in rows[:6]
            )
            suffix = "（盘中快照，状态可能变化）" if not result.get("is_final", True) else ""
            return f"涨跌停查询返回 {len(rows)} 条：{labels}{suffix}"
        if symbol and not result.get("absence_is_conclusive", True):
            return (
                f"涨跌停盘中接口暂未返回 {symbol}；空结果不能证明该股今日没有涨停或炸板。"
            )
        return f"涨跌停查询返回 0 条{f'（{symbol}）' if symbol else ''}。"
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
    if name == "tushare_query":
        rows = result.get("rows") or []
        head = rows[0] if rows else {}
        head_keys = "/".join(list(head.keys())[:4]) if head else "-"
        return (
            f"TuShare {result.get('api_name', '')}({result.get('name_cn', '')}) 返回 "
            f"{result.get('count', 0)} 行{'（已截断）' if result.get('truncated') else ''}，"
            f"{result.get('ms', '-')}ms，字段概览: {head_keys}"
        )
    if name == "list_tushare_apis":
        if result.get("categories"):
            cats = "、".join(f"{c['category']}({c['count']})" for c in result["categories"][:14])
            return f"TuShare 目录共 {result.get('total', 0)} 个接口，分类: {cats}"
        items = result.get("items") or []
        names = "、".join(f"{i['api_name']}({i['name_cn']})" for i in items[:8])
        return f"{result.get('category', '')} 分类下 {len(items)} 个接口: {names}"
    if name == "get_tushare_minutes":
        bars = result.get("bars") or []
        return f"TuShare 分钟线 {result.get('freq', '-')} 返回 {len(bars)} 条，{result.get('symbol', '-')}"
    if str(result.get("source", "")).startswith("tushare.") and "count" in result:
        rows = result.get("rows") or []
        head = rows[0] if rows else {}
        head_keys = "/".join(list(head.keys())[:4]) if head else "-"
        return (
            f"TuShare {result.get('name_cn', name)} 返回 {result.get('count', 0)} 行"
            f"{'（已截断）' if result.get('truncated') else ''}，{result.get('ms', '-')}ms，字段: {head_keys}"
        )
    return "工具调用完成。"
