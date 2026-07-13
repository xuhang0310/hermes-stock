"""TuShare 接口元数据目录 —— 聚焦 A 股研究/风控核心接口的代码化核对表。

数据源自《Tushare全部数据接口核对表_2026-07-12.md》（241 条），按产品边界聚焦：
  - 全收：股票数据、指数专题、宏观经济、大模型语料
  - 选做：ETF 专题（A 股 ETF 轮动相关）、可转债（打板相关）
  - 跳过：期货/期权/港股/美股/外汇/现货黄金/公募基金/纯债/自选组合（超出 A 股研究边界）

每条字段：
  api_name        tushare 接口标识（通用透传 tushare_query 的白名单键）
  name_cn         中文名（核对表"接口中文名称"）
  category        分类（核对表"二级分类"为主，粒度适合 list_tushare_apis 三层查询）
  scope           语义分类：stock|market|finance|macro|news|etf|cb（辅助 LLM 选用）
  points_est      tushare 积分"约"值，仅前端展示参考；运行时以 tushare 实际 msg 为准
  params          常见参数（提示性，非强制）
  default_fields  默认字段；空串=让 tushare 返回全字段
  desc            简述
  doc_id          tushare 文档 ID
  semantic_tool   对应的语义化封装工具名；None=仅走通用透传 tushare_query
  status          状态提示（stopped/legacy/sdk-only 等），正常接口省略

TUSHARE_API_SET 从 TUSHARE_CATALOG 同源生成，避免双源不一致。
"""

from __future__ import annotations


TUSHARE_CATALOG: list[dict] = [
    # ============================================================ 基础数据
    {"api_name": "stock_basic", "name_cn": "股票列表", "category": "基础数据", "scope": "stock", "points_est": "~120", "params": ["ts_code", "list_status", "exchange"], "default_fields": "", "desc": "A股基础信息：代码/名称/上市日期/市场。", "doc_id": 25, "semantic_tool": None},
    {"api_name": "stk_premarket", "name_cn": "每日股本（盘前）", "category": "基础数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "盘前股本情况。", "doc_id": 329, "semantic_tool": None},
    {"api_name": "trade_cal", "name_cn": "交易日历", "category": "基础数据", "scope": "market", "points_est": "~0", "params": ["exchange", "start_date", "end_date", "is_open"], "default_fields": "", "desc": "交易日历：开市/休市标记，时间轴操作基座。", "doc_id": 26, "semantic_tool": "get_trade_cal"},
    {"api_name": "stock_st", "name_cn": "ST股票列表", "category": "基础数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "ST/*ST股票列表。", "doc_id": 397, "semantic_tool": None},
    {"api_name": "st", "name_cn": "ST风险警示板股票", "category": "基础数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "风险警示板股票。", "doc_id": 423, "semantic_tool": None},
    {"api_name": "stock_hsgt", "name_cn": "沪深港通股票列表", "category": "基础数据", "scope": "stock", "params": ["ts_code", "hs_type"], "default_fields": "", "desc": "沪深港通标的名单。", "doc_id": 398, "semantic_tool": None},
    {"api_name": "namechange", "name_cn": "股票曾用名", "category": "基础数据", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "股票曾用名/更名记录。", "doc_id": 100, "semantic_tool": "get_namechange"},
    {"api_name": "stock_company", "name_cn": "上市公司基本信息", "category": "基础数据", "scope": "stock", "params": ["ts_code", "exchange"], "default_fields": "", "desc": "上市公司基本信息：注册地/员工/主营业务。", "doc_id": 112, "semantic_tool": None},
    {"api_name": "stk_managers", "name_cn": "上市公司管理层", "category": "基础数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "管理层姓名/职务/学历。", "doc_id": 193, "semantic_tool": None},
    {"api_name": "stk_rewards", "name_cn": "管理层薪酬和持股", "category": "基础数据", "scope": "stock", "params": ["ts_code", "end_date"], "default_fields": "", "desc": "管理层薪酬与持股变动。", "doc_id": 194, "semantic_tool": None},
    {"api_name": "bse_mapping", "name_cn": "北交所新旧代码对照", "category": "基础数据", "scope": "stock", "params": ["new_code", "old_code"], "default_fields": "", "desc": "北交所新旧代码对照。", "doc_id": 375, "semantic_tool": None},
    {"api_name": "new_share", "name_cn": "IPO新股上市", "category": "基础数据", "scope": "stock", "params": ["start_date", "end_date"], "default_fields": "", "desc": "IPO新股列表。", "doc_id": 123, "semantic_tool": None},
    {"api_name": "bak_basic", "name_cn": "股票历史列表", "category": "基础数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "历史每日股票列表。", "doc_id": 262, "semantic_tool": None},

    # ============================================================ 行情数据
    {"api_name": "daily", "name_cn": "历史日线", "category": "行情数据", "scope": "stock", "points_est": "~0", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "A股日线行情 OHLCV。", "doc_id": 27, "semantic_tool": "get_tushare_daily"},
    {"api_name": "rt_k", "name_cn": "实时日线", "category": "行情数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "A股实时日线。", "doc_id": 372, "semantic_tool": None},
    {"api_name": "stk_mins", "name_cn": "历史分钟", "category": "行情数据", "scope": "stock", "params": ["ts_code", "freq", "start_date", "end_date"], "default_fields": "", "desc": "股票历史分钟行情（1/5/15/30/60min）。", "doc_id": 370, "semantic_tool": "get_tushare_minutes"},
    {"api_name": "rt_min", "name_cn": "实时分钟", "category": "行情数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "A股实时分钟。", "doc_id": 374, "semantic_tool": None},
    {"api_name": "rt_min_daily", "name_cn": "A股实时分钟-日累计", "category": "行情数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "实时分钟日累计。", "doc_id": 457, "semantic_tool": None},
    {"api_name": "weekly", "name_cn": "周线行情", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "周线行情。", "doc_id": 144, "semantic_tool": None},
    {"api_name": "monthly", "name_cn": "月线行情", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "月线行情。", "doc_id": 145, "semantic_tool": None},
    {"api_name": "stk_weekly_monthly", "name_cn": "周/月线行情(每日更新)", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "period"], "default_fields": "", "desc": "每日更新的周/月线。", "doc_id": 336, "semantic_tool": None},
    {"api_name": "stk_week_month_adj", "name_cn": "周/月线复权行情(每日更新)", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "period"], "default_fields": "", "desc": "每日更新的复权周/月线。", "doc_id": 365, "semantic_tool": None},
    {"api_name": "adj_factor", "name_cn": "复权因子", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "复权因子。", "doc_id": 28, "semantic_tool": None},
    {"api_name": "daily_basic", "name_cn": "每日指标", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "每日指标：PE/PB/换手/量比/总市值/流通市值。", "doc_id": 32, "semantic_tool": None},
    {"api_name": "stk_limit", "name_cn": "每日涨跌停价格", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "每日涨跌停价（科创/创业±20、北交±30、ST±5）。", "doc_id": 183, "semantic_tool": "get_stk_limit"},
    {"api_name": "suspend_d", "name_cn": "每日停复牌信息", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date", "resume_date"], "default_fields": "", "desc": "停复牌信息（交易红线条件）。", "doc_id": 214, "semantic_tool": "get_suspend"},
    {"api_name": "hsgt_top10", "name_cn": "沪深股通十大成交股", "category": "行情数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "沪深股通十大成交股。", "doc_id": 48, "semantic_tool": None},
    {"api_name": "ggt_top10", "name_cn": "港股通十大成交股", "category": "行情数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "港股通十大成交股。", "doc_id": 49, "semantic_tool": None},
    {"api_name": "ggt_daily", "name_cn": "港股通每日成交统计", "category": "行情数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "港股通每日成交统计。", "doc_id": 196, "semantic_tool": None},
    {"api_name": "bak_daily", "name_cn": "备用行情", "category": "行情数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "备用行情。", "doc_id": 255, "semantic_tool": None},

    # ============================================================ 财务数据
    {"api_name": "income", "name_cn": "利润表", "category": "财务数据", "scope": "finance", "points_est": "~2000", "params": ["ts_code", "period", "start_date", "end_date"], "default_fields": "", "desc": "利润表。", "doc_id": 33, "semantic_tool": "get_income"},
    {"api_name": "balancesheet", "name_cn": "资产负债表", "category": "财务数据", "scope": "finance", "params": ["ts_code", "period", "start_date", "end_date"], "default_fields": "", "desc": "资产负债表。", "doc_id": 36, "semantic_tool": "get_balancesheet"},
    {"api_name": "cashflow", "name_cn": "现金流量表", "category": "财务数据", "scope": "finance", "params": ["ts_code", "period", "start_date", "end_date"], "default_fields": "", "desc": "现金流量表。", "doc_id": 44, "semantic_tool": "get_cashflow"},
    {"api_name": "forecast", "name_cn": "业绩预告", "category": "财务数据", "scope": "finance", "params": ["ts_code", "period", "start_date", "end_date"], "default_fields": "", "desc": "业绩预告。", "doc_id": 45, "semantic_tool": "get_forecast"},
    {"api_name": "express", "name_cn": "业绩快报", "category": "财务数据", "scope": "finance", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "业绩快报。", "doc_id": 46, "semantic_tool": "get_express"},
    {"api_name": "dividend", "name_cn": "分红送股", "category": "财务数据", "scope": "finance", "params": ["ts_code"], "default_fields": "", "desc": "分红送股数据。", "doc_id": 103, "semantic_tool": "get_dividend"},
    {"api_name": "fina_indicator", "name_cn": "财务指标数据", "category": "财务数据", "scope": "finance", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "财务指标：EPS/ROE/ROA/毛利率。", "doc_id": 79, "semantic_tool": None},
    {"api_name": "fina_audit", "name_cn": "财务审计意见", "category": "财务数据", "scope": "finance", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "财务审计意见。", "doc_id": 80, "semantic_tool": None},
    {"api_name": "fina_mainbz", "name_cn": "主营业务构成", "category": "财务数据", "scope": "finance", "params": ["ts_code", "period", "type"], "default_fields": "", "desc": "主营业务构成（产品/地区）。", "doc_id": 81, "semantic_tool": "get_fina_mainbz"},
    {"api_name": "disclosure_date", "name_cn": "财报披露日期表", "category": "财务数据", "scope": "finance", "params": ["ts_code", "end_date"], "default_fields": "", "desc": "财报披露计划。", "doc_id": 162, "semantic_tool": "get_disclosure_date"},

    # ============================================================ 参考数据
    {"api_name": "stk_shock", "name_cn": "个股异常波动", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "个股异常波动。", "doc_id": 451, "semantic_tool": None},
    {"api_name": "stk_high_shock", "name_cn": "个股严重异常波动", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "个股严重异常波动。", "doc_id": 452, "semantic_tool": None},
    {"api_name": "stk_alert", "name_cn": "交易所重点提示证券", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "交易所重点提示证券。", "doc_id": 453, "semantic_tool": None},
    {"api_name": "top10_holders", "name_cn": "前十大股东", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "前十大股东。", "doc_id": 61, "semantic_tool": "get_top10_holders"},
    {"api_name": "top10_floatholders", "name_cn": "前十大流通股东", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "前十大流通股东。", "doc_id": 62, "semantic_tool": "get_top10_float_holders"},
    {"api_name": "pledge_stat", "name_cn": "股权质押统计", "category": "参考数据", "scope": "stock", "params": ["ts_code", "end_date"], "default_fields": "", "desc": "股权质押统计数据。", "doc_id": 110, "semantic_tool": "get_pledge_stat"},
    {"api_name": "pledge_detail", "name_cn": "股权质押明细", "category": "参考数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "股权质押明细。", "doc_id": 111, "semantic_tool": None},
    {"api_name": "repurchase", "name_cn": "股票回购", "category": "参考数据", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "股票回购。", "doc_id": 124, "semantic_tool": "get_repurchase"},
    {"api_name": "share_float", "name_cn": "限售股解禁", "category": "参考数据", "scope": "stock", "params": ["ts_code", "float_date", "start_date", "end_date"], "default_fields": "", "desc": "限售股解禁。", "doc_id": 160, "semantic_tool": "get_share_float"},
    {"api_name": "block_trade", "name_cn": "大宗交易", "category": "参考数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "大宗交易。", "doc_id": 161, "semantic_tool": "get_block_trade"},
    {"api_name": "stk_account", "name_cn": "股票开户数据（停）", "category": "参考数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "股票账户开户数据（官方已停止更新）。", "doc_id": 164, "semantic_tool": None, "status": "stopped"},
    {"api_name": "stk_account_old", "name_cn": "股票开户数据（旧）", "category": "参考数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "股票账户开户数据（旧版）。", "doc_id": 165, "semantic_tool": None, "status": "legacy"},
    {"api_name": "stk_holdernumber", "name_cn": "股东人数", "category": "参考数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "股东人数。", "doc_id": 166, "semantic_tool": "get_holdernumber"},
    {"api_name": "stk_holdertrade", "name_cn": "股东增减持", "category": "参考数据", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "股东增减持。", "doc_id": 175, "semantic_tool": "get_holdertrade"},

    # ============================================================ 特色数据
    {"api_name": "report_rc", "name_cn": "券商盈利预测数据", "category": "特色数据", "scope": "finance", "params": ["ts_code", "report_date", "start_date", "end_date"], "default_fields": "", "desc": "卖方/券商盈利预测。", "doc_id": 292, "semantic_tool": "get_report_rc"},
    {"api_name": "cyq_perf", "name_cn": "每日筹码及胜率", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "每日筹码及胜率。", "doc_id": 293, "semantic_tool": "get_chip_distribution"},
    {"api_name": "cyq_chips", "name_cn": "每日筹码分布", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "每日筹码分布。", "doc_id": 294, "semantic_tool": None},
    {"api_name": "stk_factor", "name_cn": "股票技术面因子", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "股票技术因子（量化因子）。", "doc_id": 296, "semantic_tool": "get_stk_factor"},
    {"api_name": "stk_factor_pro", "name_cn": "股票技术面因子(专业版)", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "专业版技术因子（需高积分）。", "doc_id": 328, "semantic_tool": None},
    {"api_name": "ccass_hold", "name_cn": "中央结算系统持股统计", "category": "特色数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "中央结算系统持股汇总。", "doc_id": 295, "semantic_tool": "get_ccass_hold"},
    {"api_name": "ccass_hold_detail", "name_cn": "中央结算系统持股明细", "category": "特色数据", "scope": "market", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "中央结算系统持股明细。", "doc_id": 274, "semantic_tool": None},
    {"api_name": "hk_hold", "name_cn": "沪深股通持股明细", "category": "特色数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "沪深港股通持股明细。", "doc_id": 188, "semantic_tool": None},
    {"api_name": "stk_auction_o", "name_cn": "股票开盘集合竞价", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "开盘集合竞价数据。", "doc_id": 353, "semantic_tool": None},
    {"api_name": "stk_auction_c", "name_cn": "股票收盘集合竞价", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "收盘集合竞价数据。", "doc_id": 354, "semantic_tool": None},
    {"api_name": "stk_nineturn", "name_cn": "神奇九转指标", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "神奇九转指标。", "doc_id": 364, "semantic_tool": None},
    {"api_name": "stk_ah_comparison", "name_cn": "AH股比价", "category": "特色数据", "scope": "stock", "params": ["trade_date"], "default_fields": "", "desc": "AH股比价。", "doc_id": 399, "semantic_tool": None},
    {"api_name": "stk_surv", "name_cn": "机构调研数据", "category": "特色数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "机构调研记录。", "doc_id": 275, "semantic_tool": None},
    {"api_name": "broker_recommend", "name_cn": "券商月度金股", "category": "特色数据", "scope": "finance", "params": ["month", "year"], "default_fields": "", "desc": "券商每月荐股。", "doc_id": 267, "semantic_tool": None},

    # ============================================================ 两融及转融通
    {"api_name": "margin", "name_cn": "融资融券交易汇总", "category": "两融及转融通", "scope": "market", "points_est": "~2000", "params": ["trade_date", "exchange_id", "start_date", "end_date"], "default_fields": "", "desc": "融资融券交易汇总。", "doc_id": 58, "semantic_tool": "get_margin"},
    {"api_name": "margin_detail", "name_cn": "融资融券交易明细", "category": "两融及转融通", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "融资融券交易明细。", "doc_id": 59, "semantic_tool": "get_margin_detail"},
    {"api_name": "margin_secs", "name_cn": "融资融券标的（盘前）", "category": "两融及转融通", "scope": "stock", "params": ["trade_date"], "default_fields": "", "desc": "融资融券标的（盘前更新）。", "doc_id": 326, "semantic_tool": None},
    {"api_name": "slb_sec", "name_cn": "转融券交易汇总(停)", "category": "两融及转融通", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "转融券交易汇总（官方已停止更新）。", "doc_id": 332, "semantic_tool": None, "status": "stopped"},
    {"api_name": "slb_len", "name_cn": "转融资交易汇总", "category": "两融及转融通", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "转融资交易汇总。", "doc_id": 331, "semantic_tool": None},
    {"api_name": "slb_sec_detail", "name_cn": "转融券交易明细(停)", "category": "两融及转融通", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "转融券交易明细（官方已停止更新）。", "doc_id": 333, "semantic_tool": None, "status": "stopped"},
    {"api_name": "slb_len_mm", "name_cn": "做市借券交易汇总(停)", "category": "两融及转融通", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "做市借券交易汇总（官方已停止更新）。", "doc_id": 334, "semantic_tool": None, "status": "stopped"},

    # ============================================================ 资金流向数据
    {"api_name": "moneyflow", "name_cn": "个股资金流向", "category": "资金流向数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "个股资金流向。", "doc_id": 170, "semantic_tool": "get_capital_flow"},
    {"api_name": "moneyflow_ths", "name_cn": "个股资金流向（THS）", "category": "资金流向数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "同花顺个股资金流向。", "doc_id": 348, "semantic_tool": None},
    {"api_name": "moneyflow_dc", "name_cn": "个股资金流向（DC）", "category": "资金流向数据", "scope": "stock", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "东财个股资金流向。", "doc_id": 349, "semantic_tool": None},
    {"api_name": "moneyflow_cnt_ths", "name_cn": "板块资金流向（THS）", "category": "资金流向数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "同花顺概念板块资金流向。", "doc_id": 371, "semantic_tool": None},
    {"api_name": "moneyflow_ind_ths", "name_cn": "行业资金流向（THS）", "category": "资金流向数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "同花顺行业资金流向。", "doc_id": 343, "semantic_tool": None},
    {"api_name": "moneyflow_ind_dc", "name_cn": "板块资金流向（DC）", "category": "资金流向数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "东财概念及行业板块资金流向。", "doc_id": 344, "semantic_tool": None},
    {"api_name": "moneyflow_mkt_dc", "name_cn": "大盘资金流向（DC）", "category": "资金流向数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "东财大盘资金流向。", "doc_id": 345, "semantic_tool": None},
    {"api_name": "moneyflow_hsgt", "name_cn": "沪深港通资金流向", "category": "资金流向数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "沪深港通资金流向（北向资金）。", "doc_id": 47, "semantic_tool": "get_moneyflow_hsgt"},

    # ============================================================ 打板专题数据
    {"api_name": "top_list", "name_cn": "龙虎榜每日明细", "category": "打板专题数据", "scope": "market", "points_est": "~2000", "params": ["trade_date", "ts_code"], "default_fields": "", "desc": "龙虎榜每日明细。", "doc_id": 106, "semantic_tool": "get_top_list"},
    {"api_name": "top_inst", "name_cn": "龙虎榜机构交易单", "category": "打板专题数据", "scope": "market", "params": ["trade_date", "ts_code"], "default_fields": "", "desc": "龙虎榜机构明细。", "doc_id": 107, "semantic_tool": "get_top_inst"},
    {"api_name": "limit_list_ths", "name_cn": "THS涨跌停榜单", "category": "打板专题数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "同花顺涨跌停榜单。", "doc_id": 355, "semantic_tool": None},
    {"api_name": "limit_list_d", "name_cn": "涨跌停和炸板数据", "category": "打板专题数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "涨跌停列表（含炸板）。", "doc_id": 298, "semantic_tool": "get_limit_list"},
    {"api_name": "limit_step", "name_cn": "涨停股票连板天梯", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "连板天梯。", "doc_id": 356, "semantic_tool": "get_limit_step"},
    {"api_name": "limit_cpt_list", "name_cn": "涨停最强板块统计", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "最强板块统计。", "doc_id": 357, "semantic_tool": None},
    {"api_name": "ths_index", "name_cn": "THS概念板块分类", "category": "打板专题数据", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "同花顺概念和行业指数。", "doc_id": 259, "semantic_tool": None},
    {"api_name": "ths_daily", "name_cn": "THS概念板块行情", "category": "打板专题数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "同花顺板块指数行情。", "doc_id": 260, "semantic_tool": None},
    {"api_name": "ths_member", "name_cn": "THS概念板块成分", "category": "打板专题数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "同花顺概念板块成分。", "doc_id": 261, "semantic_tool": None},
    {"api_name": "dc_index", "name_cn": "DC概念板块分类", "category": "打板专题数据", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "东财概念板块。", "doc_id": 362, "semantic_tool": None},
    {"api_name": "dc_member", "name_cn": "DC概念板块成分", "category": "打板专题数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "东财板块成分。", "doc_id": 363, "semantic_tool": None},
    {"api_name": "dc_daily", "name_cn": "DC概念板块行情", "category": "打板专题数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "东财概念板块行情。", "doc_id": 382, "semantic_tool": None},
    {"api_name": "stk_auction", "name_cn": "开盘竞价成交(当日)", "category": "打板专题数据", "scope": "stock", "params": ["ts_code", "trade_date"], "default_fields": "", "desc": "当日集合竞价。", "doc_id": 369, "semantic_tool": None},
    {"api_name": "hm_list", "name_cn": "市场游资最全名录", "category": "打板专题数据", "scope": "market", "params": [], "default_fields": "", "desc": "游资名录。", "doc_id": 311, "semantic_tool": None},
    {"api_name": "hm_detail", "name_cn": "游资交易每日明细", "category": "打板专题数据", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "游资每日明细（席位买卖，配龙虎榜）。", "doc_id": 312, "semantic_tool": "get_hm_detail"},
    {"api_name": "ths_hot", "name_cn": "THS热榜", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "同花顺热榜。", "doc_id": 320, "semantic_tool": None},
    {"api_name": "dc_hot", "name_cn": "DC热榜", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "东财热榜。", "doc_id": 321, "semantic_tool": None},
    {"api_name": "tdx_index", "name_cn": "TDX概念板块分类", "category": "打板专题数据", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "通达信板块信息。", "doc_id": 376, "semantic_tool": None},
    {"api_name": "tdx_member", "name_cn": "TDX概念板块成分", "category": "打板专题数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "通达信板块成分。", "doc_id": 377, "semantic_tool": None},
    {"api_name": "tdx_daily", "name_cn": "TDX概念板块行情", "category": "打板专题数据", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "通达信板块行情。", "doc_id": 378, "semantic_tool": None},
    {"api_name": "kpl_list", "name_cn": "榜单数据（KP）", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "开盘啦榜单数据。", "doc_id": 347, "semantic_tool": None},
    {"api_name": "kpl_concept_cons", "name_cn": "题材成分（KP）", "category": "打板专题数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "开盘啦题材成分。", "doc_id": 351, "semantic_tool": None},
    {"api_name": "dc_concept", "name_cn": "题材数据（DC）", "category": "打板专题数据", "scope": "market", "params": ["trade_date"], "default_fields": "", "desc": "东财题材数据。", "doc_id": 421, "semantic_tool": None},
    {"api_name": "dc_concept_cons", "name_cn": "题材成分（DC）", "category": "打板专题数据", "scope": "stock", "params": ["ts_code"], "default_fields": "", "desc": "东财题材成分。", "doc_id": 422, "semantic_tool": None},

    # ============================================================ 指数专题
    {"api_name": "index_basic", "name_cn": "指数基本信息", "category": "指数专题", "scope": "market", "params": ["ts_code", "market"], "default_fields": "", "desc": "指数基本信息。", "doc_id": 94, "semantic_tool": None},
    {"api_name": "index_daily", "name_cn": "指数日线行情", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "指数日线行情。", "doc_id": 95, "semantic_tool": "get_index_daily"},
    {"api_name": "rt_idx_k", "name_cn": "指数实时日线", "category": "指数专题", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "指数实时日线。", "doc_id": 403, "semantic_tool": None},
    {"api_name": "rt_idx_min", "name_cn": "指数实时分钟", "category": "指数专题", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "指数实时分钟。", "doc_id": 420, "semantic_tool": None},
    {"api_name": "index_weekly", "name_cn": "指数周线行情", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "指数周线行情。", "doc_id": 171, "semantic_tool": None},
    {"api_name": "idx_mins", "name_cn": "指数历史分钟", "category": "指数专题", "scope": "market", "params": ["ts_code", "freq", "start_date", "end_date"], "default_fields": "", "desc": "指数历史分钟行情。", "doc_id": 419, "semantic_tool": None},
    {"api_name": "index_monthly", "name_cn": "指数月线行情", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "指数月线行情。", "doc_id": 172, "semantic_tool": None},
    {"api_name": "index_weight", "name_cn": "指数成分和权重", "category": "指数专题", "scope": "market", "params": ["index_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "指数成分和权重。", "doc_id": 96, "semantic_tool": "get_index_weight"},
    {"api_name": "index_dailybasic", "name_cn": "大盘指数每日指标", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "大盘指数每日指标。", "doc_id": 128, "semantic_tool": None},
    {"api_name": "index_classify", "name_cn": "申万行业分类", "category": "指数专题", "scope": "market", "params": ["level"], "default_fields": "", "desc": "申万行业分类。", "doc_id": 181, "semantic_tool": None},
    {"api_name": "index_member_all", "name_cn": "申万行业成分(分级)", "category": "指数专题", "scope": "stock", "params": ["index_code", "level"], "default_fields": "", "desc": "申万行业成分构成（分级）。", "doc_id": 335, "semantic_tool": "get_index_member"},
    {"api_name": "sw_daily", "name_cn": "申万日线行情", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "申万行业日线行情。", "doc_id": 327, "semantic_tool": "get_sw_daily"},
    {"api_name": "rt_sw_k", "name_cn": "申万实时行情", "category": "指数专题", "scope": "market", "params": ["ts_code"], "default_fields": "", "desc": "申万实时行情。", "doc_id": 417, "semantic_tool": None},
    {"api_name": "sw_mins", "name_cn": "SW历史分钟", "category": "指数专题", "scope": "market", "params": ["ts_code", "freq", "start_date", "end_date"], "default_fields": "", "desc": "申万历史分钟。", "doc_id": 469, "semantic_tool": None},
    {"api_name": "ci_index_member", "name_cn": "中信行业成分", "category": "指数专题", "scope": "stock", "params": ["index_code"], "default_fields": "", "desc": "中信行业成分。", "doc_id": 373, "semantic_tool": None},
    {"api_name": "ci_daily", "name_cn": "中信行业指数日行情", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "中信行业指数行情。", "doc_id": 308, "semantic_tool": None},
    {"api_name": "index_global", "name_cn": "国际主要指数", "category": "指数专题", "scope": "macro", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "国际指数。", "doc_id": 211, "semantic_tool": None},
    {"api_name": "idx_factor_pro", "name_cn": "指数技术面因子(专业版)", "category": "指数专题", "scope": "market", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "指数技术因子（专业版）。", "doc_id": 358, "semantic_tool": None},
    {"api_name": "daily_info", "name_cn": "沪深市场每日交易统计", "category": "指数专题", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "市场交易统计。", "doc_id": 215, "semantic_tool": "get_daily_info"},
    {"api_name": "sz_daily_info", "name_cn": "深圳市场每日交易情况", "category": "指数专题", "scope": "market", "params": ["trade_date", "start_date", "end_date"], "default_fields": "", "desc": "深圳市场每日交易概况。", "doc_id": 268, "semantic_tool": None},

    # ============================================================ 宏观经济
    {"api_name": "cn_schedule", "name_cn": "中国经济数据发布日程", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "中国经济数据发布日程。", "doc_id": 461, "semantic_tool": None},
    {"api_name": "shibor", "name_cn": "Shibor利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "Shibor利率数据。", "doc_id": 149, "semantic_tool": None},
    {"api_name": "shibor_quote", "name_cn": "Shibor报价数据", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "Shibor报价数据。", "doc_id": 150, "semantic_tool": None},
    {"api_name": "shibor_lpr", "name_cn": "LPR贷款基础利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "LPR贷款基础利率。", "doc_id": 151, "semantic_tool": None},
    {"api_name": "libor", "name_cn": "Libor利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "Libor拆借利率。", "doc_id": 152, "semantic_tool": None},
    {"api_name": "hibor", "name_cn": "Hibor利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "Hibor利率。", "doc_id": 153, "semantic_tool": None},
    {"api_name": "wz_index", "name_cn": "温州民间借贷利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "温州民间借贷利率。", "doc_id": 173, "semantic_tool": None},
    {"api_name": "gz_index", "name_cn": "广州民间借贷利率", "category": "国内宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "广州民间借贷利率。", "doc_id": 174, "semantic_tool": None},
    {"api_name": "cn_gdp", "name_cn": "国内生产总值（GDP）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "GDP数据。", "doc_id": 227, "semantic_tool": "get_cn_gdp"},
    {"api_name": "cn_cpi", "name_cn": "居民消费价格指数（CPI）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "居民消费价格指数。", "doc_id": 228, "semantic_tool": "get_cn_cpi"},
    {"api_name": "cn_ppi", "name_cn": "工业生产者出厂价格指数（PPI）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "工业生产者出厂价格指数。", "doc_id": 245, "semantic_tool": "get_cn_ppi"},
    {"api_name": "cn_m", "name_cn": "货币供应量（月）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "货币供应量。", "doc_id": 242, "semantic_tool": None},
    {"api_name": "sf_month", "name_cn": "社融增量（月度）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "社融数据（月度）。", "doc_id": 310, "semantic_tool": None},
    {"api_name": "cn_pmi", "name_cn": "采购经理指数（PMI）", "category": "国内宏观", "scope": "macro", "params": ["start_m", "end_m"], "default_fields": "", "desc": "采购经理人指数。", "doc_id": 325, "semantic_tool": "get_cn_pmi"},
    {"api_name": "us_tycr", "name_cn": "国债收益率曲线利率（美）", "category": "国际宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "美国国债收益率曲线利率（日频）。", "doc_id": 219, "semantic_tool": None},
    {"api_name": "us_trycr", "name_cn": "国债实际收益率曲线利率（美）", "category": "国际宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "美国国债实际收益率曲线利率。", "doc_id": 220, "semantic_tool": None},
    {"api_name": "us_tbr", "name_cn": "短期国债利率（美）", "category": "国际宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "美国短期国债利率。", "doc_id": 221, "semantic_tool": None},
    {"api_name": "us_tltr", "name_cn": "国债长期利率（美）", "category": "国际宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "美国国债长期利率。", "doc_id": 222, "semantic_tool": None},
    {"api_name": "us_trltr", "name_cn": "国债实际长期利率平均值（美）", "category": "国际宏观", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "美国国债实际长期利率平均值。", "doc_id": 223, "semantic_tool": None},

    # ============================================================ 大模型语料
    {"api_name": "npr", "name_cn": "国家政策库", "category": "大模型语料", "scope": "news", "params": ["start_date", "end_date"], "default_fields": "", "desc": "国家政策库。", "doc_id": 406, "semantic_tool": None},
    {"api_name": "research_report", "name_cn": "券商研究报告", "category": "大模型语料", "scope": "news", "params": ["ts_code", "report_date", "start_date", "end_date"], "default_fields": "", "desc": "券商研究报告。", "doc_id": 415, "semantic_tool": None},
    {"api_name": "monetary_policy", "name_cn": "央行货币政策执行报告", "category": "大模型语料", "scope": "news", "params": ["start_date", "end_date"], "default_fields": "", "desc": "央行货币政策执行报告。", "doc_id": 465, "semantic_tool": None},
    {"api_name": "news", "name_cn": "新闻快讯（短讯）", "category": "大模型语料", "scope": "news", "params": ["start_date", "end_date", "src"], "default_fields": "", "desc": "新闻快讯。", "doc_id": 143, "semantic_tool": None},
    {"api_name": "major_news", "name_cn": "新闻通讯（长篇）", "category": "大模型语料", "scope": "news", "params": ["start_date", "end_date"], "default_fields": "", "desc": "新闻通讯。", "doc_id": 195, "semantic_tool": None},
    {"api_name": "cctv_news", "name_cn": "新闻联播文字稿", "category": "大模型语料", "scope": "news", "params": ["start_date", "end_date"], "default_fields": "", "desc": "新闻联播。", "doc_id": 154, "semantic_tool": None},
    {"api_name": "anns_d", "name_cn": "上市公司公告", "category": "大模型语料", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "上市公司全量公告（标题/摘要）。", "doc_id": 176, "semantic_tool": "get_anns"},
    {"api_name": "irm_qa_sh", "name_cn": "上证e互动问答", "category": "大模型语料", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "上证E互动。", "doc_id": 366, "semantic_tool": None},
    {"api_name": "irm_qa_sz", "name_cn": "深证易互动问答", "category": "大模型语料", "scope": "stock", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "深证互动易。", "doc_id": 367, "semantic_tool": None},
    {"api_name": "eco_cal", "name_cn": "财经日历", "category": "大模型语料", "scope": "macro", "params": ["start_date", "end_date"], "default_fields": "", "desc": "全球财经事件日历。", "doc_id": 233, "semantic_tool": None},

    # ============================================================ ETF 专题（选做）
    {"api_name": "etf_basic", "name_cn": "ETF基本信息", "category": "ETF专题", "scope": "etf", "params": ["ts_code", "market"], "default_fields": "", "desc": "ETF基础信息。", "doc_id": 385, "semantic_tool": None},
    {"api_name": "etf_index", "name_cn": "ETF跟踪指数", "category": "ETF专题", "scope": "etf", "params": ["ts_code"], "default_fields": "", "desc": "ETF基准指数列表。", "doc_id": 386, "semantic_tool": None},
    {"api_name": "rt_etf_min", "name_cn": "ETF实时分钟", "category": "ETF专题", "scope": "etf", "params": ["ts_code"], "default_fields": "", "desc": "ETF实时分钟。", "doc_id": 416, "semantic_tool": None},
    {"api_name": "rt_etf_min_daily", "name_cn": "ETF实时分钟-日累计", "category": "ETF专题", "scope": "etf", "params": ["ts_code"], "default_fields": "", "desc": "ETF实时分钟日累计。", "doc_id": 470, "semantic_tool": None},
    {"api_name": "etf_mins", "name_cn": "ETF历史分钟", "category": "ETF专题", "scope": "etf", "params": ["ts_code", "freq", "start_date", "end_date"], "default_fields": "", "desc": "ETF历史分钟行情。", "doc_id": 387, "semantic_tool": None},
    {"api_name": "rt_etf_k", "name_cn": "ETF实时日线", "category": "ETF专题", "scope": "etf", "params": ["ts_code"], "default_fields": "", "desc": "ETF实时日线。", "doc_id": 400, "semantic_tool": None},
    {"api_name": "fund_daily", "name_cn": "ETF日线行情", "category": "ETF专题", "scope": "etf", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "ETF日线行情。", "doc_id": 127, "semantic_tool": None},
    {"api_name": "fund_adj", "name_cn": "ETF复权因子", "category": "ETF专题", "scope": "etf", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "基金复权因子。", "doc_id": 199, "semantic_tool": None},
    {"api_name": "etf_share_size", "name_cn": "ETF份额规模", "category": "ETF专题", "scope": "etf", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "ETF份额规模。", "doc_id": 408, "semantic_tool": None},
    {"api_name": "etf_sh_cons", "name_cn": "每日篮子组合(沪市PCF)", "category": "ETF专题", "scope": "etf", "params": ["trade_date"], "default_fields": "", "desc": "沪市PCF每日篮子组合。", "doc_id": 471, "semantic_tool": None},
    {"api_name": "etf_sz_cons", "name_cn": "每日篮子组合(深市PCF)", "category": "ETF专题", "scope": "etf", "params": ["trade_date"], "default_fields": "", "desc": "深市PCF每日篮子组合。", "doc_id": 472, "semantic_tool": None},
    {"api_name": "rt_etf_sz_iopv", "name_cn": "ETF实时参考", "category": "ETF专题", "scope": "etf", "params": ["ts_code"], "default_fields": "", "desc": "ETF实时参考IOPV。", "doc_id": 454, "semantic_tool": None},
    {"api_name": "idx_anns", "name_cn": "指数公司公告", "category": "ETF专题", "scope": "market", "params": ["start_date", "end_date"], "default_fields": "", "desc": "指数公司公告。", "doc_id": 460, "semantic_tool": None},

    # ============================================================ 可转债（选做）
    {"api_name": "cb_basic", "name_cn": "可转债基础信息", "category": "可转债", "scope": "cb", "params": ["ts_code"], "default_fields": "", "desc": "可转债基本信息。", "doc_id": 185, "semantic_tool": None},
    {"api_name": "cb_issue", "name_cn": "可转债发行", "category": "可转债", "scope": "cb", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "可转债发行。", "doc_id": 186, "semantic_tool": None},
    {"api_name": "cb_call", "name_cn": "可转债赎回信息", "category": "可转债", "scope": "cb", "params": ["ts_code"], "default_fields": "", "desc": "可转债赎回信息。", "doc_id": 269, "semantic_tool": None},
    {"api_name": "cb_rate", "name_cn": "可转债票面利率", "category": "可转债", "scope": "cb", "params": ["ts_code"], "default_fields": "", "desc": "可转债票面利率。", "doc_id": 305, "semantic_tool": None},
    {"api_name": "cb_daily", "name_cn": "可转债行情", "category": "可转债", "scope": "cb", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "可转债行情。", "doc_id": 187, "semantic_tool": None},
    {"api_name": "top10_cb_holders", "name_cn": "可转债十大持有人", "category": "可转债", "scope": "cb", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "可转债十大持有人。", "doc_id": 459, "semantic_tool": None},
    {"api_name": "cb_factor_pro", "name_cn": "可转债技术面因子(专业版)", "category": "可转债", "scope": "cb", "params": ["ts_code", "trade_date", "start_date", "end_date"], "default_fields": "", "desc": "可转债技术因子（专业版）。", "doc_id": 392, "semantic_tool": None},
    {"api_name": "cb_price_chg", "name_cn": "可转债转股价变动", "category": "可转债", "scope": "cb", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "可转债转股价变动。", "doc_id": 246, "semantic_tool": None},
    {"api_name": "cb_share", "name_cn": "可转债转股结果", "category": "可转债", "scope": "cb", "params": ["ts_code", "start_date", "end_date"], "default_fields": "", "desc": "可转债转股结果。", "doc_id": 247, "semantic_tool": None},
    {"api_name": "cb_rating", "name_cn": "可转债债券评级", "category": "可转债", "scope": "cb", "params": ["ts_code"], "default_fields": "", "desc": "可转债债券评级。", "doc_id": 458, "semantic_tool": None},
]


# 同源生成 api_name 白名单：通用透传 tushare_query 仅允许调用 catalog 收录的接口，
# 避免新增 catalog 条目时忘加白名单而静默拒绝。
TUSHARE_API_SET: frozenset[str] = frozenset(entry["api_name"] for entry in TUSHARE_CATALOG)

# api_name -> 条目，快速查单条（通用透传取默认字段/中文名时用）
_BY_API_NAME: dict[str, dict] = {entry["api_name"]: entry for entry in TUSHARE_CATALOG}


def by_api_name(api_name: str) -> dict | None:
    return _BY_API_NAME.get(api_name)


def by_category(category: str) -> list[dict]:
    return [entry for entry in TUSHARE_CATALOG if entry["category"] == category]


def by_category_scope(category: str, scope: str | None) -> list[dict]:
    return [
        entry for entry in TUSHARE_CATALOG
        if entry["category"] == category and (scope is None or entry["scope"] == scope)
    ]


def category_index() -> list[dict]:
    """分类索引：list_tushare_apis 无参调用时返回，避免一次返回全量被 7000 字符截断成碎片。"""
    stats: dict[str, dict] = {}
    for entry in TUSHARE_CATALOG:
        cat = entry["category"]
        bucket = stats.setdefault(cat, {"category": cat, "count": 0, "scopes": set()})
        bucket["count"] += 1
        bucket["scopes"].add(entry["scope"])
    return [
        {"category": b["category"], "count": b["count"], "scopes": sorted(b["scopes"])}
        for b in stats.values()
    ]
