# SixRabbits Stock MCP 接入

本模块把现有 A 股行情、持仓和定时研究能力开放给其他智能体。采用官方 Python MCP SDK 1.28.1 的 Streamable HTTP，复用现有 API 服务，无需新增端口。协议设计参考 [官方传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 和 [官方 Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.28.1)。

## 地址与认证

部署本版本后使用 `https://shijingxuan.com/api/mcp/`，末尾保留 `/`。本地开发为 `http://127.0.0.1:8000/api/mcp/`。

每次请求带 `Authorization: Bearer <MCP密钥>`。网页账号密码和登录 JWT 用于管理凭据，不交给外部智能体。密钥归属于签发用户，默认只读、90 天有效；可以单独撤销。账号停用、密码重置或“退出所有设备”也会使已有密钥失效。数据库仅存密钥摘要，原文只在创建时返回一次。

这是专用 Bearer 密钥接入，适用于支持自定义 HTTP 请求头的 MCP 客户端；本版没有实现 OAuth 浏览器授权、旧版 HTTP+SSE 或 stdio。只支持 OAuth 的客户端不能直接接入此版本。

## 获取接入密钥

在项目目录运行，交互输入自己的账号密码，不需要服务器或数据库凭据：

```powershell
# 默认只读：行情、持仓，以及账号有管理权限时的任务查询
python scripts/mcp_access.py create --username YOUR_USERNAME --name research-agent

# 允许当前账号已有的持仓录入与任务创建权限
python scripts/mcp_access.py create --username YOUR_USERNAME --name assistant --scopes market:read,portfolio:read,portfolio:write,schedules:read,schedules:write --days 90

# 列出自己签发的凭据（不返回原文）
python scripts/mcp_access.py list --username YOUR_USERNAME

# 撤销某一凭据
python scripts/mcp_access.py revoke --username YOUR_USERNAME --token-id 123

# 用官方 MCP 客户端验证握手与工具发现，交互输入 MCP 密钥
python scripts/mcp_access.py check
```

客户端通用配置示意（具体配置文件结构以该客户端为准）：

```json
{
  "mcpServers": {
    "sixrabbits-stock": {
      "url": "https://shijingxuan.com/api/mcp/",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

不要把真实密钥提交到 Git。可以为不同智能体分别签发密钥，按需只开启一组权限。

管理 API 也支持网页 JWT：`POST /api/mcp-tokens`（`name`、可选 `scopes`、`expires_in_days`），`GET /api/mcp-tokens`，`DELETE /api/mcp-tokens/{id}`。MCP 密钥不能调用这些管理接口。

## 工具与权限

| 工具 | 用途 | 权限 |
|---|---|---|
| `resolve_stock` | 从名称或代码定位股票，多匹配时返回候选 | `market:read` |
| `get_stock_quote` | 最新可用价格、交易日、来源和新鲜度 | `market:read` |
| `analyze_stock` | 个股行情、均线/MACD/RSI、支撑压力和基本面 | `market:read` |
| `analyze_market` | 全 A 收盘截面、领先/落后行业和覆盖率 | `market:read` |
| `list_position_accounts` | 列出本人账户 | `portfolio:read` |
| `get_positions` | 本人持仓数量、成本及盈亏 | `portfolio:read` |
| `analyze_portfolio` | 组合集中度、缺失数据和风险线 | `portfolio:read` |
| `analyze_position` | 持有/减仓决策证据：个人成本、风险计划、行情、财务和资金 | `portfolio:read` |
| `add_position` | 增量录入一笔已发生的买入 | `portfolio:write` |
| `list_research_profiles` | 可用于定时任务的数字人 | `schedules:read` |
| `list_schedules` | 本人任务列表 | `schedules:read` |
| `get_schedule` | 本人指定任务详情 | `schedules:read` |
| `create_schedule` | 创建一次或周期研究任务 | `schedules:write` |

权限与系统账号权限取交集：行情/持仓读取要求 `data:read`，持仓写入要求 `data:write`，任务读写要求 `schedule:manage`。工具不接受 `user_id`，身份只能由密钥确定；管理员的 MCP 工具也只读取其本人持仓和任务。

## 在其他智能体中这样使用

- “分析今天 A 股行情，哪些行业更强？”→ `analyze_market`，需要明确这是收盘截面；盘中数据不可用时不能称为实时分析。
- “看看我的持仓，哪里风险最大？”→ `get_positions` + `analyze_portfolio`，再对重点股票调用 `analyze_position`。
- “浪潮信息该抛还是继续拿？”→ `resolve_stock` 确认证券代码，再 `analyze_position`；同一股票跨多个账户时先选账户。
- “刚以 38 元买了 100 股招商银行，记到主账户。”→ `list_position_accounts` → `add_position`。
- “每个交易日 17:30 帮我复盘持仓。”→ `list_research_profiles` → `create_schedule`，北京时间，`trading_day_only=true`。

分析工具提供真实业务引擎的结构化证据，由调用方智能体组织最终报告，不在 MCP 中额外调用一次大模型。报告应说明持有依据、减仓依据、触发条件及数据缺口；数据过期、行情缺失或账户歧义时返回待核对信息，不编造确定性结论。没有证券交易接口，`add_position` 只维护本系统账本。

## 写入示例与重试

`add_position` 参数：

```json
{
  "account_id": 1,
  "symbol": "600036.SH",
  "quantity": 100,
  "execution_price": 38,
  "idempotency_key": "buy-20261030-unique-id"
}
```

`quantity` 单位为股，价格单位为人民币元。增量增加数量、加权成本，同时更新账本现金和流水，不能用来覆盖现有总持仓。账户必须已存在且属于本人；首次录入前可在网站创建证券账户。

`create_schedule` 参数（`target_agent_code` 应取自可用数字人列表）：

```json
{
  "name": "收盘持仓复盘",
  "instruction": "分析我的持仓和当天行业行情，列出继续持有及减仓的证据、风险线与数据缺口。",
  "target_agent_code": "stock-pm",
  "schedule": {"type": "daily", "run_time": "17:30"},
  "trading_day_only": true,
  "idempotency_key": "schedule-20261030-unique-id"
}
```

支持 `once`（`run_at`）、`daily`（`run_time`）、`weekly`（`run_time` 和 `weekdays`，周一为 1）、`monthly_last`（`run_time`）、`interval`（`interval_minutes`、可选 `window_start/window_end`）。时间统一为 `Asia/Shanghai`，带时区的一次性时间会换算。创建后任务启用，由现有调度器执行，结果在网站任务记录中查看；MCP 创建入口不启用代码执行或群推送。

每个新的写入意图使用新的 `idempotency_key`（1–80 个字母、数字或 `_.:-`）。网络重试必须复用同一个 key 及参数，相同请求返回原回执；同一 key 改参数会拒绝。写入和回执在同一数据库事务提交，跨 API 副本不会重复录入。

## 部署说明

- 本次新增 `mcp_access_tokens`、`mcp_mutation_receipts` 两张表，现有 `initialize_default_database` 会通过 `Base.metadata.create_all` 创建。按当前发布流程先启动 background，再启动 API 副本；不要仅复制 API 文件而跳过建表。
- `/api/mcp/` 走现有 Nginx `/api/` 代理。服务使用无状态 JSON 响应，不需要粘滞会话，两个 API 副本均可处理任意请求。
- 新增显式依赖 `mcp==1.28.1`；保持 `<2`，与现有 AgentScope 依赖一致。
- `STOCK_MCP_ALLOWED_HOSTS` 默认包含正式域名及本地回环；`STOCK_MCP_ALLOWED_ORIGINS` 默认包含正式站点及本地开发地址。换域名时配置精确允许项，浏览器来源不能用 `*` 绕过校验。
- MCP 调用在操作日志中记录工具名、用户、成功/失败和耗时；不记录密钥、调用参数或研究正文。
- 未部署此版本前，正式站点不会自动获得上述入口。本说明中的 URL 是部署后的地址。

相关测试：`tests/test_mcp_gateway.py`、`tests/test_mcp_tokens.py`、`tests/test_mcp_portfolio_tools.py`、`tests/test_mcp_schedule_tools.py`、`tests/test_mcp_market_tools.py`。
