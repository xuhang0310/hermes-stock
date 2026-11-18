---
name: quant-selection
description: 执行全站统一的 A 股 L1–L7 分层量化选股与单票体检。当用户询问量化选股、今日选什么票、波段/长线/事件选股、大盘仓位上限或某只股票能不能买、挂单价与买多少股时使用。
compatibility: Uses precomputed L1 market regime and L2-L6 daily snapshots with PIT financial and full-market announcement screening.
metadata:
  hermes-display-name: 统一分层量化选股
  hermes-category: quantitative
  hermes-version: "1.0.0"
  hermes-risk-level: low
  license: MIT
allowed-tools: get_market_regime_l1 query_unified_quant_selection inspect_stock_layered_l1_l7
---

# 统一分层量化选股（L1–L7 判断栈）

本技能严格遵循 A 股实证规律与「单层单职责、观点与执行/风控解耦」原则，杜绝左右互搏与拍脑袋追涨。

## 标准决策链

1. **L1 市场环境定仓位上限**：先用 `get_market_regime_l1` 查看全市场均线宽度、成交分位与宽基趋势，明确今日总仓位上限（进攻 90% / 中性 60% / 防守 30%）及风格象限。
2. **L2–L6 六层筛选与一票否决**：用 `query_unified_quant_selection` 按用户持有周期（`swing` 波段 / `long` 长线 / `event` 事件驱动）与账户预算筛选候选池；若用户询问单只标的，用 `inspect_stock_layered_l1_l7` 做完整体检。
3. **执行与 L7 风险股数**：输出量化限价买点（`suggested_buy_price`）、证伪退出参考价（`stop_ref_price`）以及基于账户资金和单笔最大风险比例核定的整手股数。

## 硬性铁律

- L1 大盘温度计只控制总仓位上限，不掺入个股打分。
- 任何触发一票否决（`vetoed=true`：立案调查、退市风险、财务造假、ST、波段破位飞刀）的股票直接判定为「放弃」。
- 数据覆盖率不足或预期差缺乏客观证据支撑的股票最高只能给「观察」，不得列入「动手」。
- 所有输出均为投研与挂单计划草稿，严禁承诺收益，不触碰自动交易。
