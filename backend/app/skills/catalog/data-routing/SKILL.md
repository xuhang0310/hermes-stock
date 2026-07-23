---
name: data-routing
description: 为 A 股行情、财务、公告新闻、资金流、持仓、自选池和知识库研究选择正确的数据工具、来源与回退路径。任何需要取数、联网检索、交叉核验或发现数据源异常的研究任务都应先使用本技能。
compatibility: Hermes Stock data tools; external providers require their configured API credentials and network access.
metadata:
  hermes-display-name: 数据路由与来源治理
  hermes-category: data
  hermes-version: "3.0.0"
  hermes-risk-level: low
allowed-tools: resolve_stock get_latest_price get_realtime_quote get_tushare_daily get_stock_info search_stock_news search_market_intel search_comprehensive_intel search_knowledge
---

# 数据路由与来源治理

先明确需要什么数据，再选工具。不要因为某个工具顺手就让它承担不适合的任务。

## 路由流程

1. 确认标的、市场、时间范围、频率和统计口径。
2. 区分实时行情、历史序列、财务报表、事件资料、用户私有数据和内部知识。
3. 选择最接近原始来源的工具。
4. 检查返回中的 `source`、`trade_date`、`report_date`、`fetched_at` 和错误字段。
5. 来源失败时按同市场、同口径回退；不能跨口径静默替换。
6. 会影响核心结论的数据至少进行一次独立交叉核验。

## 能力路由

| 数据需要 | 首选工具 | 关键检查 |
|---|---|---|
| 标的代码标准化 | `resolve_stock` | 市场后缀、名称歧义 |
| 最新可用价格 | `get_latest_price` | 交易日、是否缓存、是否陈旧 |
| 盘中报价 | `get_realtime_quote` | 是否真正实时、市场是否开盘 |
| 日线序列 | `get_tushare_daily` | 起止日期、复权和缺口 |
| 公司与估值概览 | `get_stock_info` | 报告期、PE/PB 口径 |
| 个股公告和新闻 | `search_stock_news` | 标题、来源、发布日期、URL |
| 市场和政策情报 | `search_market_intel` | 权威来源和时效 |
| 综合交叉检索 | `search_comprehensive_intel` | 不同来源是否独立 |
| 内部资料 | `search_knowledge` | 文档、切块和可信等级 |

## 来源优先级

一般按以下顺序选择：

1. 交易所公告、公司法定披露、监管机构和官方统计；
2. 已配置的结构化行情或财务数据源；
3. 权威媒体和专业数据库；
4. 聚合搜索结果；
5. 无法追溯来源的二手转述。

低等级来源可以提供线索，但不能单独支撑高置信结论。

## 回退纪律

- Token 缺失、超时或限流时明确记录失败原因。
- 实时源失败后使用最近收盘价，必须改称“最近可用收盘价”。
- 不同来源数值偏差显著时同时保留，并解释可能的复权、币种、报告期或合并口径差异。
- 不把空数组解释为“没有事件”；它也可能意味着检索失败。

详细的来源和时效判定见 [references/source-policy.md](references/source-policy.md)。
