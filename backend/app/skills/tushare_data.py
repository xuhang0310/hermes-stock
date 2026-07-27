"""技能：TuShare 数据 —— 通用透传 + 目录查询 + 39 个语义封装（聚焦 A 股核心约 150 接口）。

Phase 2：registry 接管分发；prompt_instructions 补齐。
"""

from backend.app.market.stock_tools import (
    MAX_MINUTE_ROWS,
    STOCK_TOOL_SCHEMAS,
    _int_arg,
    _optional_str,
    get_anns,
    get_balancesheet,
    get_block_trade,
    get_cashflow,
    get_ccass_hold,
    get_cn_cpi,
    get_daily_info,
    get_disclosure_date,
    get_dividend,
    get_express,
    get_fina_mainbz,
    get_forecast,
    get_hm_detail,
    get_holdertrade,
    get_holdernumber,
    get_income,
    get_index_daily,
    get_index_member,
    get_index_weight,
    get_limit_list,
    get_limit_step,
    get_margin,
    get_margin_detail,
    get_moneyflow_hsgt,
    get_namechange,
    get_pledge_stat,
    get_repurchase,
    get_report_rc,
    get_share_float,
    get_stk_factor,
    get_stk_limit,
    get_suspend,
    get_sw_daily,
    get_top10_float_holders,
    get_top10_holders,
    get_top_inst,
    get_top_list,
    get_trade_cal,
    get_tushare_minutes,
    list_tushare_apis,
    tushare_query,
)

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in STOCK_TOOL_SCHEMAS}


def _pick(*names: str) -> list[dict]:
    """从 STOCK_TOOL_SCHEMAS 按工具名取出对应 schema，保持单一来源不漂移。"""
    return [_SCHEMA_BY_NAME[n] for n in names]


SKILL = {
    "name": "tushare_data",
    "display_name": "TuShare 数据",
    "description": "TuShare 通用透传 + 接口目录 + 39 个语义封装（交易日历/停复牌/涨跌停/龙虎榜/游资/两融/北向/解禁/大宗/十大股东/增减持/回购/质押/三大表/业绩预告快报/分红/主营/披露/指数/申万/市场统计/券商预测/公告/技术因子等）",
    "version": "1.2.0",
    "chat_modes": ["stock", "research"],
    "roles": [],
    "tools": _pick(
        "tushare_query", "list_tushare_apis",
        # 首批（Phase 1a）
        "get_trade_cal", "get_suspend", "get_stk_limit",
        "get_top_list", "get_hm_detail", "get_margin", "get_moneyflow_hsgt",
        "get_forecast", "get_income", "get_index_daily", "get_sw_daily",
        "get_cn_cpi", "get_tushare_minutes",
        # 二批（Phase 1b）
        "get_top_inst", "get_limit_list", "get_limit_step", "get_margin_detail",
        "get_share_float", "get_block_trade", "get_top10_holders",
        "get_top10_float_holders", "get_holdertrade", "get_holdernumber",
        "get_repurchase", "get_pledge_stat", "get_express", "get_dividend",
        "get_balancesheet", "get_cashflow", "get_fina_mainbz",
        "get_disclosure_date", "get_index_weight", "get_index_member",
        "get_daily_info", "get_report_rc", "get_anns", "get_namechange",
        "get_stk_factor", "get_ccass_hold",
    ),
    "executors": {
        "tushare_query": lambda a: tushare_query(
            str(a.get("api_name") or ""),
            params=a.get("params") if isinstance(a.get("params"), dict) else None,
            fields=a.get("fields"),
            limit=a.get("limit") if a.get("limit") is not None else 300,
        ),
        "list_tushare_apis": lambda a: list_tushare_apis(
            category=(str(a.get("category")) if a.get("category") else None),
            scope=(str(a.get("scope")) if a.get("scope") else None),
        ),
        # 首批
        "get_trade_cal": lambda a: get_trade_cal(
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
            exchange=_optional_str(a.get("exchange")),
        ),
        "get_suspend": lambda a: get_suspend(
            ts_code=_optional_str(a.get("symbol")),
            trade_date=_optional_str(a.get("trade_date")),
            resume_date=_optional_str(a.get("resume_date")),
        ),
        "get_stk_limit": lambda a: get_stk_limit(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_top_list": lambda a: get_top_list(
            trade_date=_optional_str(a.get("trade_date")),
            ts_code=_optional_str(a.get("symbol")),
        ),
        "get_hm_detail": lambda a: get_hm_detail(
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_margin": lambda a: get_margin(
            trade_date=_optional_str(a.get("trade_date")),
            exchange_id=_optional_str(a.get("exchange_id")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_moneyflow_hsgt": lambda a: get_moneyflow_hsgt(
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_forecast": lambda a: get_forecast(
            ts_code=_optional_str(a.get("symbol")),
            period=_optional_str(a.get("period")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_income": lambda a: get_income(
            str(a.get("symbol") or ""),
            period=_optional_str(a.get("period")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_index_daily": lambda a: get_index_daily(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_sw_daily": lambda a: get_sw_daily(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_cn_cpi": lambda a: get_cn_cpi(
            start_m=_optional_str(a.get("start_m")),
            end_m=_optional_str(a.get("end_m")),
        ),
        "get_tushare_minutes": lambda a: get_tushare_minutes(
            str(a.get("symbol") or ""),
            freq=str(a.get("freq") or "5min"),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
            limit=_int_arg(a.get("limit"), 800, MAX_MINUTE_ROWS),
        ),
        # 二批
        "get_top_inst": lambda a: get_top_inst(
            trade_date=_optional_str(a.get("trade_date")),
            ts_code=_optional_str(a.get("symbol")),
        ),
        "get_limit_list": lambda a: get_limit_list(
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
            ts_code=_optional_str(a.get("symbol")),
        ),
        "get_limit_step": lambda a: get_limit_step(trade_date=_optional_str(a.get("trade_date"))),
        "get_margin_detail": lambda a: get_margin_detail(
            ts_code=_optional_str(a.get("symbol")),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_share_float": lambda a: get_share_float(
            str(a.get("symbol") or ""),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_block_trade": lambda a: get_block_trade(
            trade_date=_optional_str(a.get("trade_date")),
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_top10_holders": lambda a: get_top10_holders(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_top10_float_holders": lambda a: get_top10_float_holders(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_holdertrade": lambda a: get_holdertrade(
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_holdernumber": lambda a: get_holdernumber(
            ts_code=_optional_str(a.get("symbol")),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_repurchase": lambda a: get_repurchase(
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_pledge_stat": lambda a: get_pledge_stat(
            str(a.get("symbol") or ""),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_express": lambda a: get_express(
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_dividend": lambda a: get_dividend(str(a.get("symbol") or "")),
        "get_balancesheet": lambda a: get_balancesheet(
            str(a.get("symbol") or ""),
            period=_optional_str(a.get("period")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_cashflow": lambda a: get_cashflow(
            str(a.get("symbol") or ""),
            period=_optional_str(a.get("period")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_fina_mainbz": lambda a: get_fina_mainbz(
            str(a.get("symbol") or ""),
            period=_optional_str(a.get("period")),
            bz_type=_optional_str(a.get("bz_type")),
        ),
        "get_disclosure_date": lambda a: get_disclosure_date(
            ts_code=_optional_str(a.get("symbol")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_index_weight": lambda a: get_index_weight(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_index_member": lambda a: get_index_member(
            str(a.get("symbol") or ""),
            level=_optional_str(a.get("level")),
        ),
        "get_daily_info": lambda a: get_daily_info(
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_report_rc": lambda a: get_report_rc(
            ts_code=_optional_str(a.get("symbol")),
            report_date=_optional_str(a.get("report_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_anns": lambda a: get_anns(
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_namechange": lambda a: get_namechange(
            ts_code=_optional_str(a.get("symbol")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_stk_factor": lambda a: get_stk_factor(
            str(a.get("symbol") or ""),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
        "get_ccass_hold": lambda a: get_ccass_hold(
            ts_code=_optional_str(a.get("symbol")),
            trade_date=_optional_str(a.get("trade_date")),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
        ),
    },
    # 留空：TuShare 优先、先查目录再透传等已在基础问股/研究提示词覆盖，避免二次注入。
    "prompt_instructions": [],
}
