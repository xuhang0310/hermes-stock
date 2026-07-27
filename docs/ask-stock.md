# 问股功能设计与数据表映射

本功能参考 `daily_stock_analysis` 的 `/chat` 问股链路，在 SixRabbits Stock 中采用同一类核心模式：

```text
Vue 问股页面 -> POST /api/agent/chat(chat_mode=stock) -> 流式 ReAct Agent
-> LLM Tool Calling -> TuShare/Tavily 工具 -> 工具结果回填模型 -> SSE 返回前端
```

## daily_stock_analysis 表结构分析

`daily_stock_analysis/data/stock_analysis.db` 当前包含 31 张业务表，可以按能力分为这些组：

| 分组 | 表 | 用途 |
| --- | --- | --- |
| 行情分析 | `stock_daily`, `analysis_history`, `fundamental_snapshot` | 日线、分析报告、基本面快照 |
| 问股会话 | `conversation_messages`, `conversation_summaries`, `agent_provider_turns`, `llm_usage` | 多轮消息、上下文压缩、模型调用轨迹、用量统计 |
| 新闻情报 | `news_intel`, `intelligence_items`, `intelligence_sources` | 搜索结果、资讯源、情报条目 |
| 决策信号 | `decision_signals`, `decision_signal_feedback`, `decision_signal_outcomes` | 交易观察信号、反馈和事后评估 |
| 回测 | `backtest_results`, `backtest_summaries` | 分析建议的后验评估 |
| 组合 | `portfolio_accounts`, `portfolio_positions`, `portfolio_trades`, `portfolio_position_lots`, `portfolio_daily_snapshots`, `portfolio_cash_ledger`, `portfolio_fx_rates`, `portfolio_corporate_actions` | 账户、持仓、交易、快照和汇率 |
| 告警 | `alert_rules`, `alert_triggers`, `alert_notifications`, `alert_cooldowns` | 告警规则、触发、通知和冷却 |
| 迁移 | `schema_migrations` | 数据库迁移记录 |

SixRabbits Stock 已有可承接的表：

| DSA 表 | SixRabbits Stock 对应表 | 当前处理 |
| --- | --- | --- |
| `stock_daily` | `daily_bars` | TuShare 日线会写入 `daily_bars` |
| `news_intel` / `intelligence_items` | `documents` | Tavily 搜索结果会尽量写入 `documents` |
| `conversation_messages` | `ai_chat_messages` | 问股会话复用现有 AI 聊天消息表 |
| `agent_provider_turns` | 暂无 | 后续可补模型调用轨迹表 |
| `llm_usage` | 暂无 | 后续可补用量统计表 |
| `analysis_history` | `agent_reports` | 后续可把高价值问股结论沉淀为报告 |

## 本次落地范围

后端新增股票工具：

```text
resolve_stock        股票代码/名称解析
get_tushare_daily    获取 TuShare 日线并缓存 daily_bars
analyze_daily_bars   计算 MA、量比、支撑压力、回撤和风险点
search_stock_news    使用 Tavily Search 搜索消息面
```

前端新增“问股”页面：

```text
frontend/src/views/AskStockView.vue
```

它复用现有 SSE 事件：

```text
model_selected
reasoning_delta
content_delta
content_end
action
observation
error
done
```

## 环境变量

在 `backend/.env` 中配置：

```text
STOCK_TUSHARE_TOKEN=你的 TuShare Token
STOCK_TAVILY_API_KEY=你的 Tavily API Key
```

模型仍复用现有配置：

```text
STOCK_LLM_BASE_URL=
STOCK_LLM_API_KEY=
STOCK_LLM_MODEL=
STOCK_AGENT_MAX_STEPS=6
STOCK_AGENT_LOOP_MAX_ITERATIONS=50
STOCK_AGENT_LOOP_DUPLICATE_TOOL_LIMIT=3
```

`STOCK_AGENT_MAX_STEPS` 保留给旧版运行时；AgentScope 使用独立的
`STOCK_AGENT_LOOP_MAX_ITERATIONS`。50 是复杂任务可用的最大预算，模型输出最终
答案时会提前结束，不会强制执行满 50 轮。相同只读工具和参数重复达到阈值后会自动熔断。
