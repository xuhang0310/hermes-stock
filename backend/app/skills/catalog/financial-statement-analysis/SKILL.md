---
name: financial-statement-analysis
description: 对 A 股公司进行基本面和财报三表研究，包括主营业务、收入利润、资产负债、现金流、盈利质量、估值和财务红旗。用户询问基本面、财务质量、业绩、估值、现金流、ROE、PE/PB 或公司是否值得长期研究时使用。
compatibility: Requires configured company profile and financial statement data tools.
metadata:
  hermes-display-name: 财报与基本面分析
  hermes-category: fundamental
  hermes-version: "3.0.0"
  hermes-risk-level: low
allowed-tools: get_stock_info get_analysis_context get_income get_balancesheet get_cashflow
---

# 财报与基本面分析

从业务事实和三张报表的勾稽关系出发，判断利润是否真实、增长是否可持续、估值使用的口径是否合理。

## 执行顺序

1. 用 `get_stock_info` 确认公司、行业、市值和基础估值。
2. 获取最近完整年度和最新报告期的利润表、资产负债表、现金流量表。
3. 统一币种、单位、单季/累计、合并/母公司和实际/预测口径。
4. 分析收入增长、毛利、费用、净利润和扣非利润。
5. 检查应收、存货、商誉、有息负债、合同负债和受限资金。
6. 用经营现金流验证利润质量，并检查三表勾稽。
7. 估值必须说明使用 TTM、静态、动态或预测口径。
8. 给出多头证据、反方证据、数据缺口和下一报告期观察点。

## 三表核心问题

### 利润表

- 增长来自销量、价格、并表还是一次性收益？
- 毛利率变化是产品结构、成本还是竞争导致？
- 扣非利润和归母利润是否长期偏离？

### 资产负债表

- 应收和存货增速是否长期高于收入？
- 是否存在存贷双高、商誉减值或短债压力？
- 在建工程和资本化支出是否与产能兑现相匹配？

### 现金流量表

- 经营现金流能否覆盖净利润和资本开支？
- 自由现金流改善来自经营，还是延迟付款或削减投资？
- 融资现金流是否暴露再融资依赖？

详细红旗和计算口径见 [references/red-flags.md](references/red-flags.md)。

## 输出结构

```markdown
## 业务与行业位置
## 财务表现与报告期
## 盈利质量和三表勾稽
## 资产负债与现金安全
## 估值口径
## 多头证据 / 反方证据
## 风险、缺口与下一验证点
```

不要把行业经验值当成适用于所有行业的硬阈值。金融、周期、地产和轻资产公司的报表结构不同。
