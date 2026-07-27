from backend.app.market.company_research import get_company_research_bundle


COMPANY_RESEARCH_BUNDLE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_company_research_bundle",
        "description": (
            "生成单一A股公司的标准研究底稿，一次并行取得最新行情、历史K线、"
            "涨跌停和异常时间线、公司资料、三张财务报表、估值、公告新闻、"
            "资金流、两融、筹码、龙虎榜和内部研究材料，并返回七项研究覆盖状态。"
            "公司深研、全面分析、完整复盘时应优先调用本工具，再按缺口补查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "A股代码或 ts_code，例如 600984.SH。",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "行情和事件回看天数，默认365，范围90至730。",
                },
                "horizon": {
                    "type": "string",
                    "description": "研究期限，例如短期、中期、长期。",
                },
            },
            "required": ["symbol"],
        },
    },
}


SKILL = {
    "name": "company_research",
    "display_name": "公司研究底稿",
    "description": "并行生成公司行情、财务、估值、事件、技术、资金和反方视角底稿",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    "roles": [],
    "tools": [COMPANY_RESEARCH_BUNDLE_SCHEMA],
    "executors": {
        "get_company_research_bundle": lambda args: get_company_research_bundle(
            str(args.get("symbol") or ""),
            lookback_days=int(args.get("lookback_days") or 365),
            horizon=str(args.get("horizon") or "中期"),
        )
    },
    "prompt_instructions": [
        "公司深研、全面分析或完整复盘必须优先调用 get_company_research_bundle，"
        "先取得标准底稿，再根据用户问题对必要维度做针对性补查；"
        "数据源暂不可用时在报告的数据局限中自然说明，不输出系统验收或完成度术语。"
    ],
}
