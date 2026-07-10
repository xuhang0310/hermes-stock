# 数据库设计

当前后端只使用 MySQL，结构与 `hermes-rabbit` 保持一致：

```text
settings -> database -> models -> repository -> main
```

## 连接约定

默认读取 `hermes-rabbit/backend/.env` 的数据库地址、端口、账号和密码：

```text
RABBIT_DB_HOST
RABBIT_DB_PORT
RABBIT_DB_USER
RABBIT_DB_PASSWORD
```

本项目数据库名固定为：

```text
hermes-stock
```

如果需要覆盖配置，可以在 `backend/.env` 中写：

```text
STOCK_DB_HOST=127.0.0.1
STOCK_DB_PORT=3306
STOCK_DB_USER=root
STOCK_DB_PASSWORD=
STOCK_DB_NAME=hermes-stock
```

## 表分组

### 标的与范围

- `instruments`: 股票、指数、ETF、行业指数统一标的表
- `watchlist_groups`: 自选池分组
- `watchlist_items`: 自选池成员
- `positions`: 用户手动维护的持仓台账
- `trade_reports`: 用户交易报备

### 数据与情报

- `market_snapshots`: 分钟/实时快照
- `daily_bars`: 日线行情
- `documents`: 新闻、公告、研报、政策、互动问答
- `document_symbols`: 文档和标的关联

### 规则与事件

- `rules`: 规则定义
- `events`: 规则命中事件，系统核心中枢
- `event_evidence`: 事件证据，保证每个告警可追溯

### Agent

- `agent_profiles`: Agent 角色、权限和模型配置
- `agent_tasks`: Agent 待办和执行记录
- `agent_reports`: Agent 输出报告

### 风控、通知、复盘

- `risk_checks`: 风控检查
- `reviews`: 复盘记录
- `notifications`: 飞书、PM、本地通知发送记录
- `api_audit_logs`: 外部接口调用审计
- `system_logs`: 系统运行日志
- `heartbeats`: 后台组件心跳

## 设计原则

- 单一职责：配置、连接、模型、查询、API 分离。
- DRY：列表查询和输出转换集中在 `repository.py`。
- 奥卡姆剃刀：当前不保留 SQLite 与 MySQL 两套后端。
- 事件中心：`events` 是报告、风控、通知和复盘的追溯源头。
- 安全边界：系统只做研究、提醒和复盘，不做自动交易。
