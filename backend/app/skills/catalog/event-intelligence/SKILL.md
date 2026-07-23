---
name: event-intelligence
description: 检索和研判 A 股公告、新闻、监管政策、产业事件、龙虎榜线索和市场舆情，构建事件时间线并区分事实、传闻和解释。用户询问“为什么涨跌”、近期催化、公告影响、风险事件或新闻核验时使用。
compatibility: Requires configured Tavily, Brave, market-news, announcement, or equivalent search providers.
metadata:
  hermes-display-name: 事件与情报核验
  hermes-category: intelligence
  hermes-version: "3.0.0"
  hermes-risk-level: low
allowed-tools: search_stock_news search_market_intel search_comprehensive_intel get_anns
---

# 事件与情报核验

搜索结果是线索，不是结论。先建立发生了什么，再讨论市场为什么这样反应。

## 执行流程

1. 明确标的、事件窗口和用户要判断的问题。
2. 优先检索交易所公告、公司披露和监管文件。
3. 再检索权威媒体、行业来源和市场报道。
4. 为每条材料记录标题、来源、发布日期、URL 和事件日期。
5. 合并转载和重复报道，识别它们是否来自同一个原始来源。
6. 建立时间线：预期形成、事实披露、价格反应、后续澄清。
7. 分离已证实事实、合理推断、市场传闻和未知项。
8. 同时寻找可能推翻主流解释的证据。

## 来源等级

| 等级 | 来源 | 用法 |
|---|---|---|
| A | 交易所、监管、公司法定披露 | 可直接支撑事实 |
| B | 官方部门、权威统计和一手采访 | 可支撑背景与政策 |
| C | 主流财经媒体、专业数据库 | 需要核对原始材料 |
| D | 聚合网站、自媒体和匿名消息 | 只能提供线索 |

详细判定见 [references/source-hierarchy.md](references/source-hierarchy.md)。

## 因果纪律

- 时间接近不等于因果关系。
- “市场人士认为”不是可验证证据。
- 事件影响需要说明传导链：事件 → 收入/成本/风险偏好 → 估值或现金流。
- 股价已经提前反应时，利好披露后也可能下跌。
- 没有找到新闻不能写成“没有风险事件”。

## 输出结构

```markdown
## 已确认事实
## 事件时间线
## 影响传导与市场预期
## 反方解释
## 来源清单
## 未核验信息与后续观察
```
