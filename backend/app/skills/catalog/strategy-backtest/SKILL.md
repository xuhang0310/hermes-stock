---
name: strategy-backtest
description: 运行可复现的 A 股策略回测并生成结构化指标、逐笔交易、归因、图表数据和完整报告。用户提出 MACD 回测、回归测试、收益测试、历史数据测试、策略收益、净值、回撤、Sharpe、胜率、交易明细或策略验证时使用。
compatibility: Requires Hermes TuShare market data and MySQL backtest result storage.
metadata:
  hermes-display-name: 策略回测
  hermes-category: quant
  hermes-version: "1.0.0"
  hermes-risk-level: medium
allowed-tools: run_macd_backtest
---

# 策略回测

## 执行规则

1. 明确标的、日期、频率、策略参数、资金、费用、滑点与基准。
2. 使用 `run_macd_backtest` 生成确定性的结构化结果，禁止模型自行计算或补写交易数据。
3. 信号在当日收盘确认，下一交易日开盘执行，避免未来函数。
4. A 股现货默认只做多，并模拟 T+1、100 股整数手、佣金最低收费、印花税和滑点。
5. 最终回答必须保留 `report_url`，并披露置信度和全部重要警告。

## 解释纪律

- 少于 120 个交易日或 5 笔交易属于样本不足。
- 少于 252 个交易日或 10 笔交易属于低置信度。
- 短样本年化收益只能作为描述值，不能用于证明策略稳定。
- 到期平仓产生的盈利要与信号平仓分开解释。
- 不得把回测表现表述为未来收益承诺。

## 输出

输出策略和参数、数据区间、核心指标、交易归因、稳健性警告和完整报告链接。
