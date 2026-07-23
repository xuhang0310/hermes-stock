"""Alpha-factor discovery, scoring, and IC/IR evaluation tools."""

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
                        "enum": ["alpha101", "gtja191", "qlib158", "academic", "fundamental"],
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
                "在指定 A 股股票池评估最多 20 个因子的下一日横截面 IC、IR、"
                "正 IC 比例和 alive/reversed/dead 状态。"
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
                },
                "required": ["alpha_ids", "symbols"],
            },
        },
    },
]


SKILL = {
    "name": "factor_research",
    "display_name": "量化因子研究",
    "description": "462 个 Alpha 因子的检索、横截面评分与 IC/IR 基准评估。",
    "version": "1.0.0",
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
        ),
    },
    "prompt_instructions": [
        "因子高分不等于买入信号；先在同一股票池做样本外 IC/IR 评估，再解释方向。",
        "因子研究至少使用 5 只股票，优先使用 20 只以上横截面，并明确时间窗口和数据覆盖。",
        "报告必须披露交易成本、停牌涨跌停、行业市值中性化尚未纳入当前快速评估。",
    ],
}

