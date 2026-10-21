---
name: market-snapshot
description: 获取并解释 A 股标的的最新可用价格、实时或准实时行情、日线序列、涨跌幅和量价状态。用户询问股价、K 线、涨跌、近期走势、开收盘表现或要求复盘某个交易日时使用。
compatibility: Requires at least one configured A-share market data source in Hermes Stock.
metadata:
  hermes-display-name: 行情快照
  hermes-category: market
  hermes-version: "3.0.0"
  hermes-risk-level: low
allowed-tools: resolve_stock get_latest_price get_realtime_quote get_tushare_daily get_limit_list
---

# 行情快照

行情分析的第一步是确定“价格属于哪一天、哪种口径”，而不是立即解释涨跌原因。

## 执行流程

1. 用 `resolve_stock` 消除代码、名称和市场歧义。
2. 用户问“现在、当前、盘中”时尝试 `get_realtime_quote`。
3. 无可靠实时源或市场已收盘时，使用 `get_latest_price` 并明确称为最近可用价格。
4. 涉及趋势、波动或复盘时，用 `get_tushare_daily` 获取足够长度的日线。
5. 检查交易日、OHLC、成交量、数据来源、缓存和陈旧标记。
6. 将事实性行情与原因推断分开；价格本身不能证明事件因果。
7. 开盘时若 `trade_date` 早于当前交易日，或返回 `stale` / `intraday_unavailable`，不得用上一交易日收盘数据否定用户描述的今日盘中事件。

## 最低分析维度

- 标准证券代码和名称；
- 数据截止交易日与获取时间；
- 最新价或最近收盘价；
- 当日涨跌幅、振幅和量能（返回数据支持时）；
- 观察窗口内的高低点与位置；
- 数据源和实时性限制。

## 边界

- 停牌、节假日或盘前阶段没有新成交，不把最后收盘价写成实时成交。
- 用户询问今日涨跌停、炸板或连板时，必须用带 `symbol` 和目标交易日的涨跌停数据交叉核验；盘中空结果不等于“没有发生”。
- 复权口径不明确时，不直接比较跨除权日价格。
- 单日涨跌不能自动推导基本面变化。
- 日线缺口、零成交量或异常价格先标记数据问题。
- 用户只问一个明确数字时保持简洁，不强行扩写成完整研报。

## 回答要讲到

用白话直接回答，不照抄下面的条目：

- 是哪只股票（名称和代码）；
- 价格是哪天、什么时候的（盘中实时还是最近收盘）；
- 现在多少钱、当天涨跌多少、成交量比平时大还是小；
- 最近一段时间处在高位、中间还是低位；
- 数据有什么限制（如还没覆盖今天盘中），一句话说明。

更严格的时效判断按 `data-routing` 技能的来源策略执行。
