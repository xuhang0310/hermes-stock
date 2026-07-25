---
name: strategy-backtest
description: 运行可复现的 A 股策略回测并生成结构化指标、交易或调仓明细、归因、图表数据和完整报告。用户提出 MACD 回测、风险平价、等权组合、组合回测、组合归因、收益测试、历史数据测试、策略收益、净值、回撤、Sharpe、胜率或策略验证时使用。
compatibility: Requires Hermes TuShare market data and MySQL backtest result storage.
metadata:
  hermes-display-name: 策略回测
  hermes-category: quant
  hermes-version: "1.0.0"
  hermes-risk-level: medium
allowed-tools: run_macd_backtest run_portfolio_backtest
---

# 策略回测

## 执行规则

1. 明确标的、日期、频率、策略参数、资金、费用、滑点与基准。
2. MACD 单标的策略使用 `run_macd_backtest`；风险平价、等权对比或多标的组合使用 `run_portfolio_backtest`。
3. 内置工具覆盖请求时，禁止模型编写或运行临时 Python 回测脚本，也禁止自行补写交易、调仓和收益数据。
4. MACD 信号在当日收盘确认，下一交易日开盘执行，避免未来函数。
5. 风险平价每个调仓点仅使用该日期以前的数据估计协方差；基准使用相同标的、相同频率和相同成本的等权组合。
6. 最终回答必须保留 `report_url`，并披露置信度和全部重要警告。

## 解释纪律

- 少于 120 个交易日或 5 笔交易属于样本不足。
- 少于 252 个交易日或 10 笔交易属于低置信度。
- 短样本年化收益只能作为描述值，不能用于证明策略稳定。
- 到期平仓产生的盈利要与信号平仓分开解释。
- 组合报告必须区分资产贡献、调仓成本、市场 Beta/Alpha、状态分析和统计检验；条件不足时明确标记 skipped。
- 不得把收益顺序随机打乱后的 Sharpe 当作有效的显著性检验，因为顺序置换不会改变均值和标准差。
- 不得把回测表现表述为未来收益承诺。

## 输出

输出策略和参数、数据区间、核心指标、基准对比、交易或调仓归因、稳健性警告和完整报告链接。
