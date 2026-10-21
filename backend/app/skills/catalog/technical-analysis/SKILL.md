---
name: technical-analysis
description: 对 A 股价格序列进行趋势、均线、MACD、RSI、布林带、成交量、支撑阻力和形态分析。用户询问技术面、买卖点、趋势是否反转、超买超卖、量价配合或短线交易结构时使用。
compatibility: Requires sufficient daily or intraday price history from Hermes Stock market tools.
metadata:
  hermes-display-name: 技术与量价分析
  hermes-category: quantitative
  hermes-version: "3.0.0"
  hermes-risk-level: medium
allowed-tools: get_tushare_daily get_analysis_context analyze_daily_bars calculate_ma get_volume_analysis analyze_pattern
---

# 技术与量价分析

技术指标是价格和成交量的变换，不是独立事实来源。使用多个指标时，应寻找相互确认或冲突，而不是堆叠术语。

## 数据准备

1. 确认证券代码、交易日、复权口径和观察周期。
2. 日线分析通常需要至少 60 个有效交易日；长期趋势需要更长窗口。
3. 检查停牌、除权、异常成交量和缺失数据。
4. 指标参数偏离常用设置时说明原因。

## 分析顺序

1. **位置**：价格处于观察窗口的高位、中位还是低位。
2. **趋势**：高低点结构与 MA5/10/20/60 排列。
3. **动量**：MACD、RSI 等是否确认趋势，是否出现背离。
4. **波动**：布林带宽度、振幅和波动扩张/收缩。
5. **量价**：突破是否放量，回撤是否缩量，异常量能是否可持续。
6. **关键位**：支撑、阻力、缺口、前高前低和失效条件。
7. **情景**：延续、震荡和反转三种路径，而不是单点预测。

详细指标纪律见 [references/indicator-discipline.md](references/indicator-discipline.md)。

## 结论边界

- 金叉、死叉和超买超卖不是自动交易信号。
- 背离需要价格结构确认。
- 单根 K 线形态在低流动性或消息冲击下可靠性更低。
- 技术面不能替代公告、财务和流动性风险检查。
- 涉及行动建议时给出触发条件、失效条件和风险边界。

## 回答要讲到

用白话结论做小标题（如“下跌的力度在减弱，但还没确认止跌”），不照抄下面的条目：

- 现在处在什么位置、趋势往哪走，用均线和前高前低的具体价位说明；
- 指标说明了什么（第一次出现的指标用半句话解释，如“RSI 只有 22，说明短期跌得太急”），指标互相矛盾时直说；
- 量价配不配合：放量还是缩量，说明什么；
- 上方压力和下方支撑分别在哪个价位，为什么；
- 操作参考写成“跌破多少说明什么、站上多少说明什么”；
- 数据窗口或复权对结论有影响时，一句话交代。
