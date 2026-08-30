"""Alpha-factor discovery, scoring, and IC/IR evaluation tools."""

from backend.app.quant import tushare_factor_service
from backend.app.quant.factor_service import (
    benchmark_factors,
    get_factor,
    list_factors,
    score_factor,
)


FACTOR_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_factor_alphas",
            "description": "浏览 Hermes 的 462 个预构建量化因子，可按因子库、主题和市场过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "zoo": {
                        "type": "string",
                        "enum": ["alpha101", "gtja191", "qlib158", "academic", "fundamental", "tushare"],
                    },
                    "theme": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_factor_alpha",
            "description": "查看一个因子的公式、数据要求、预热周期、适用市场和研究说明。",
            "parameters": {
                "type": "object",
                "properties": {"alpha_id": {"type": "string"}},
                "required": ["alpha_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_factor_universe",
            "description": (
                "在指定 A 股股票池计算一个因子的最新横截面原始值和百分位排名。"
                "结果不是买卖建议，方向必须经过 benchmark 验证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alpha_id": {"type": "string"},
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 5,
                        "maxItems": 50,
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD"},
                    "end_date": {"type": "string", "description": "YYYYMMDD"},
                    "refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "本地数据不足时是否逐只拉取 TuShare 日线。",
                    },
                },
                "required": ["alpha_id", "symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "benchmark_factor_alphas",
            "description": (
                "在指定 A 股股票池评估最多 20 个因子的下一日横截面 IC、IR、正 IC 比例、"
                "秩稳定性(RRE，约高换手越低)、train/test 分窗 IC 与 alive/reversed/dead 状态；"
                "可传 dedup_threshold 做低相关贪心去重，返回保留/剔除清单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alpha_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 5,
                        "maxItems": 50,
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD"},
                    "end_date": {"type": "string", "description": "YYYYMMDD"},
                    "refresh": {"type": "boolean", "default": False},
                    "dedup_threshold": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "可选：0~1 的截面相关阈值，传了就按 IR 降序贪心剔除与已选集相关的因子",
                    },
                },
                "required": ["alpha_ids", "symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tushare_factors",
            "description": (
                "查询 TuShare 官方因子库（付费 212 因子）在本地缓存的数据："
                "不传 factor_name 返回因子目录（含各因子缓存行数）；"
                "传 factor_name + symbols 返回这些股票已缓存的因子值历史。"
                "只读本地缓存，不实时外呼；全市场横截面排序请用 score_factor_universe。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "factor_name": {
                        "type": "string",
                        "description": "TuShare 因子名（不带 tushare: 前缀），如 roe_ttm；留空则返回目录",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                        "description": "股票代码列表；查询因子值时建议提供",
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD"},
                    "end_date": {"type": "string", "description": "YYYYMMDD"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 100,
                        "description": "最多返回的因子值行数",
                    },
                },
            },
        },
    },
]


SKILL = {
    "name": "factor_research",
    "display_name": "量化因子研究",
    "description": "462 个 Alpha 因子与 TuShare 官方因子库的检索、横截面评分与 IC/IR 基准评估。",
    "version": "1.1.0",
    "chat_modes": ["research"],
    "roles": ["quant-analyst", "research-analyst", "stock-pm"],
    "tools": FACTOR_TOOL_SCHEMAS,
    "executors": {
        "list_factor_alphas": lambda a: list_factors(
            zoo=a.get("zoo"),
            theme=a.get("theme"),
            query=a.get("query"),
            limit=int(a.get("limit") or 30),
        ),
        "get_factor_alpha": lambda a: get_factor(str(a.get("alpha_id") or "")),
        "score_factor_universe": lambda a: score_factor(
            str(a.get("alpha_id") or ""),
            list(a.get("symbols") or []),
            start_date=a.get("start_date"),
            end_date=a.get("end_date"),
            refresh=bool(a.get("refresh")),
        ),
        "benchmark_factor_alphas": lambda a: benchmark_factors(
            list(a.get("alpha_ids") or []),
            list(a.get("symbols") or []),
            start_date=a.get("start_date"),
            end_date=a.get("end_date"),
            refresh=bool(a.get("refresh")),
            dedup_threshold=a.get("dedup_threshold"),
        ),
        "get_tushare_factors": lambda a: tushare_factor_service.get_tushare_factors(
            a.get("factor_name"),
            symbols=list(a.get("symbols") or []) or None,
            start_date=a.get("start_date"),
            end_date=a.get("end_date"),
            limit=int(a.get("limit") or 100),
        ),
    },
    "prompt_instructions": [
        "因子高分不等于买入信号；先在同一股票池做样本外 IC/IR 评估，再解释方向。",
        "判断因子是否可信：rank_stability 高（换手低）、train_ic 与 test_ic 同号且幅度接近、ic_positive_ratio 明显偏离 0.5。",
        "因子研究至少使用 5 只股票，优先使用 20 只以上横截面，并明确时间窗口和数据覆盖。",
        "报告必须披露交易成本、停牌涨跌停、行业市值中性化尚未纳入当前快速评估。",
        "TuShare 官方因子值来自本地缓存（get_tushare_factors），引用时必须说明对应 trade_date 与缓存窗口，不得当作实时数据。",
    ],
}

