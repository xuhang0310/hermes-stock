---
name: portfolio-risk-analysis
description: 基于系统真实持仓分析组合集中度、行业暴露、相关性、流动性、回撤、止损聚集和情景风险。用户询问整个账户是否安全、组合风险、资产配置、集中度或多个持仓如何共同影响风险时使用。
compatibility: Requires authenticated portfolio snapshots and sufficient price history.
metadata:
  hermes-display-name: 组合风险分析
  hermes-category: risk
  hermes-version: "3.0.0"
  hermes-risk-level: high
allowed-tools: get_portfolio_snapshot get_risk_context get_analysis_context
---

# 组合风险分析

组合风险不是单票风险简单相加。相关持仓在压力情景下可能同时失效，因此先读取真实组合，再讨论分散化。

## 执行流程

1. 获取账户、现金和完整持仓集合。
2. 计算或描述单票集中度、前五持仓占比和行业暴露。
3. 检查相同风险因子：政策、周期、利率、汇率、商品价格和风格。
4. 用价格历史评估相关性时注明窗口和数据缺口。
5. 检查流动性、停牌、涨跌停和大额减持等退出风险。
6. 汇总止损距离和风险事件，识别风险是否在同一时间聚集。
7. 设计至少一个市场压力情景和一个标的特定情景。

常用指标和误区见 [references/portfolio-metrics.md](references/portfolio-metrics.md)。

## 回答要讲到

用白话结论做小标题（如“看着买了 8 只，其实都押在电力上”），不照抄下面的条目：

- 组合现在什么样：总市值、现金、几只票、前几大占多少；
- 钱是不是集中在同一个行业或同一类风险上；
- 哪些持仓会一起涨跌、一起出问题；
- 想卖的时候卖不卖得掉；
- 大盘大跌或某只票出事时组合会怎样；
- 最该先处理哪几件事。

数据不足时直接说哪些还没拿到，结论相应收窄。不能用“持仓数量很多”直接判断已经分散。
