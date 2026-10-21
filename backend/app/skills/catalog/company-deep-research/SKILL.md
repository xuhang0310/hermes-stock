---
name: company-deep-research
description: 对单一 A 股公司进行数据驱动的完整研究，综合业务、行业、财务、估值、行情、事件、资金、竞争优势和反方论证。用户要求深度研究、完整复盘、投资逻辑、公司研究备忘录或多维度判断时使用。
compatibility: Uses Hermes Stock market, financial, news, and knowledge capabilities.
metadata:
  hermes-display-name: 公司深度研究
  hermes-category: research
  hermes-version: "3.0.0"
  hermes-risk-level: medium
allowed-tools: get_company_research_bundle get_latest_price get_daily_history get_limit_list get_stock_info get_income get_balancesheet get_cashflow get_anns search_stock_news get_capital_flow get_margin_detail get_chip_distribution search_knowledge
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
3. 首先调用 `get_company_research_bundle`，一次取得标准七维底稿和数据可用性。
4. 根据用户问题和底稿中的数据可用性使用专用工具补查，不重复查询已经取得的维度。
5. 建立行情和事件时间线，必须检查涨停、跌停、炸板和大幅波动记录。
6. 分析业务结构、行业位置、客户与竞争。
7. 用财报和现金流验证商业叙事。
8. 检查估值口径和市场隐含预期。
9. 补充资金、技术和内部知识证据，但不让它们替代基本面。
10. 主动构建最强反方论证。
11. 列出催化、风险、监测指标和观点失效条件。
12. 按回答结构一次性用白话写出完整结论；数据源暂不可用时在末尾用一句话说明。

## 观点自检

写每个关键观点前先自己过一遍（这是思考清单，不要把这些条目或标签写进回答）：

- 这个判断能不能被事实推翻；
- 直接支撑它的材料是什么，哪天、从哪来；
- 证据怎么推出这个判断；
- 最强的反面材料是什么；
- 有多大把握；
- 出现什么新事实就说明判断错了。

写给用户时把这些融进白话里，例如：“市盈率 11 倍，比同行便宜（10 月 20 日数据）；但便宜可能有原因——煤价要是比电价涨得快，利润会被吃掉，10 月 29 日三季报出来就能看清。”

回答结构见 [references/research-template.md](references/research-template.md)。

## 专业边界

- 不因用户持仓而调整事实判断。
- 不把远期行业空间直接等同于公司收入。
- 不用单一估值倍数替代盈利质量分析。
- 不把管理层目标写成已实现结果。
- 数据不足时降低结论置信度，并明确说明数据局限，不拼凑确定结论。
