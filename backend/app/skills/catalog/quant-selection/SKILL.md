---
name: quant-selection
description: 查量化选股（L1–L7）的大盘环境、分层得分、排雷结果和单票体检。当用户询问量化选股页面的结论、大盘环境、某只股票有没有踩雷时使用。除模型排名外的分层打分没有经过回测，只作参考。
compatibility: Uses precomputed L1 market regime and L2-L6 daily snapshots with PIT financial and full-market announcement screening.
metadata:
  hermes-display-name: 量化选股（L1–L7）
  hermes-category: quantitative
  hermes-version: "1.0.0"
  hermes-risk-level: low
  license: MIT
allowed-tools: get_market_regime_l1 query_unified_quant_selection inspect_stock_layered_l1_l7
---

# 量化选股（L1–L7）

这个技能只查信息，不定计划。

## 哪些验证过，哪些没有

- 验证过：模型排名（7 项量价特征，2019 年以后的样本外回测通过）；买入模型的挂买价；止损不能比 3 倍 ATR 更近。
- 没有验证：L2–L6 的分层得分和权重、“高分/中等/低分或排除”、波段的破位和阴跌否决、大盘总仓位上限（90%/60%/30%）。
- 波段总分已经作为挑战者“分层打分前 30”接进自动选股，和模型排名前 30 用同一套规则记成绩；问哪个更好，看自动选股的历史战绩。
- 排雷（立案调查、退市风险、财务造假、ST）是常识性的风险排除，可以直接引用。

## 怎么用

1. 用户问大盘环境，用 `get_market_regime_l1`，说清进攻/中性/防守，并说明仓位上限还没有经过回测。
2. 用户问某只股票，用 `inspect_stock_layered_l1_l7`。命中排雷就直接说不建议买；分层得分和结论只作参考，要说明没有经过回测。
3. 用户要量化选股页面的名单，用 `query_unified_quant_selection`，同样说明哪些没有经过回测。

## 和操作计划的关系

- 操作计划里的买价、止损和股数，以计划提示里系统算好的为准。
- 这里的挂买价和止损下限用的是同一套代码。买入模型说不挂的股票，这里也不给买价。
- 这里的股数对新开仓只是参考：验证过的股数规则只管已有持仓。已有持仓加减多少，看操作计划。
- 系统不接券商、不下单。
