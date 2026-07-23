---
name: company-deep-research
description: 对单一 A 股公司进行证据驱动的完整研究，综合业务、行业、财务、估值、行情、事件、资金、竞争优势和反方论证。用户要求深度研究、完整复盘、投资逻辑、公司研究备忘录或多维度判断时使用。
compatibility: Uses Hermes Stock market, financial, news, knowledge, and evidence-ledger capabilities.
metadata:
  hermes-display-name: 公司深度研究
  hermes-category: research
  hermes-version: "3.0.0"
  hermes-risk-level: medium
allowed-tools: get_analysis_context get_stock_info search_knowledge search_stock_news
---

# 公司深度研究

公司研究的目标不是把所有指标写一遍，而是回答：这家公司如何创造现金、市场在定价什么、哪些证据会改变判断。

## 研究问题

开始前明确：

- 研究对象和证券代码；
- 用户关心的期限；
- 是首次覆盖、事件复盘、持仓决策支持还是观点更新；
- 当前已知事实和需要验证的假设。

## 工作流

1. 用 `research-discipline` 检查偏差。
2. 用 `data-routing` 规划来源和回退路径。
3. 建立行情和事件时间线。
4. 分析业务结构、行业位置、客户与竞争。
5. 用财报和现金流验证商业叙事。
6. 检查估值口径和市场隐含预期。
7. 补充资金、技术和内部知识证据，但不让它们替代基本面。
8. 主动构建最强反方论证。
9. 列出催化、风险、监测指标和观点失效条件。
10. 用完成审计检查必选证据。

## 观点结构

每个关键观点采用：

```text
Claim：可被证伪的一句话判断
Evidence：直接支撑材料，含来源和日期
Reasoning：证据如何支持判断
Counter-evidence：最强反方材料
Confidence：高 / 中 / 低
Invalidation：什么新事实会推翻判断
```

完整研究模板见 [references/research-template.md](references/research-template.md)。

## 专业边界

- 不因用户持仓而调整事实判断。
- 不把远期行业空间直接等同于公司收入。
- 不用单一估值倍数替代盈利质量分析。
- 不把管理层目标写成已实现结果。
- 证据不足时输出研究缺口，不拼凑确定结论。
