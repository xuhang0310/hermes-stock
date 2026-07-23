---
name: watchlist-research
description: 审查和维护系统自选池，检查标的入池理由、研究覆盖、近期催化、风险、去留条件和后续任务。用户要求增加或删除自选、扫描自选池、挑选候选股、检查入池逻辑或上传自选截图时使用。
compatibility: Requires authenticated access to Hermes Stock watchlist records; mutations follow approval policy.
metadata:
  hermes-display-name: 自选池研究与维护
  hermes-category: portfolio
  hermes-version: "3.0.0"
  hermes-risk-level: medium
allowed-tools: get_watchlist_snapshot get_symbol_context search_stock_news
---

# 自选池研究与维护

自选池不是代码列表，而是一组待验证研究假设。每个标的应有入池理由、观察变量和移出条件。

## 上下文连续性

1. 优先读取当前自选池和当前对话已确认标的。
2. 图片识别后保留识别结果和用户修正，后续轮次不要重新猜测。
3. 同一标的已有研究时进行增量更新，不重复生成没有变化的背景。
4. 写操作交给自选录入 Agent，并按审批策略执行。

## 单标的审查

- 当前是否已入池；
- 入池日期和原始理由；
- 最新行情与近期事件；
- 已有研究覆盖和证据缺口；
- 催化、风险和关键观察指标；
- 保留、补充研究或移出的条件。

## 全池审查

- 无入池理由的标的；
- 长期无研究更新的标的；
- 重复暴露或高度相关标的；
- 风险事件后未复核的标的；
- 已满足移出条件但仍保留的标的；
- 候选标的优先级和下一研究任务。

## 输出

以表格展示标的、状态、理由、催化、风险、覆盖度和下一步。删除自选前说明原因并要求必要审批，不因一次短期波动自动移出。
