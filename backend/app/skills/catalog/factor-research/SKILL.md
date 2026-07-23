---
name: factor-research
description: 检索并评估 Alpha101、国泰君安191、Qlib158、学术异象和PIT基本面因子。用户要求量化选股、因子研究、IC/IR检验、多因子比较或横截面评分时使用。
compatibility: Requires at least five A-share symbols and sufficient daily bars in Hermes Stock; TuShare can refresh missing history.
metadata:
  hermes-display-name: 量化因子研究
  hermes-category: quantitative
  hermes-version: "1.0.0"
  hermes-risk-level: medium
  license: MIT
allowed-tools: list_factor_alphas get_factor_alpha score_factor_universe benchmark_factor_alphas
---

# 量化因子研究

因子是某个交易日对一组股票计算的横截面分数。它是待验证的研究假设，不是天然有效的买卖信号。

## 标准流程

1. 明确股票池、研究区间、调仓频率和预测周期。
2. 使用 `list_factor_alphas` 按市场、主题或因子库筛选候选公式。
3. 使用 `get_factor_alpha` 检查公式、输入字段、预热窗口和实现说明。
4. 使用 `benchmark_factor_alphas` 检查下一交易日横截面 IC、IR、正 IC 比例和样本数量。
5. 只有通过样本外检验的因子，才使用 `score_factor_universe` 生成最新横截面排名。
6. 报告数据覆盖、幸存者偏差、交易成本和中性化缺口。

## 纪律

- 至少需要 5 只股票，专业研究优先使用 20 只以上。
- 高分不自动表示看多；负 IC 稳定的因子可能需要反向使用。
- 不允许使用未来数据，不把全样本筛选结果冒充样本外表现。
- 多因子组合前应检查相关性，避免重复堆叠同一类量价信号。
- 快速 IC 评估没有包含交易成本、停牌、涨跌停、行业和市值中性化。

## 输出结构

```markdown
## 研究目标与股票池
## 候选因子与公式
## 数据覆盖与预热
## IC/IR及稳定性
## 因子方向与失效条件
## 样本外验证建议
## 当前限制
```

