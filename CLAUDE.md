# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SixRabbits Stock (product/display name; the repo dir, Python package, and MySQL database keep the internal `hermes-stock` naming) is a local A-share (Chinese equities) multi-agent research and risk-control workspace: a FastAPI + SQLAlchemy/MySQL backend with a Vue 3 frontend. It is **research/monitoring/review only** — it never connects to a broker, places orders, or gives investment advice. Preserve that safety boundary when adding features (see the SQL sandbox and prompt rules below).

The backend deliberately mirrors the shape of a sibling project `hermes-rabbit`: `settings -> database -> models -> repository -> main`.

## Commands

Backend (run from repo root):

```powershell
python -m pip install -e .                         # install (editable)
python -m backend.app.scripts_init_db              # create tables + seed demo data (DB must exist)
python -m backend.app.scripts_init_db --create-database   # also CREATE DATABASE (needs admin MySQL user)
python -m backend.app.main --host 127.0.0.1 --port 8000   # run API (add --reload for autoreload)
```

Tests (require a reachable MySQL; the suite **skips** itself if MySQL is unreachable):

```powershell
python -m pytest                                   # all tests (testpaths=tests)
python -m pytest tests/test_backend_api.py         # single file
python -m pytest tests/test_backend_api.py::BackendApiTests::test_health_reports_mysql   # single test
```

Tests hit a **real** MySQL and seed into the live `hermes-stock` database. Set `STOCK_SSH_PASSWORD` (+ `STOCK_SSH_USER`) to auto-open a paramiko SSH tunnel to a remote MySQL before the suite runs.

Frontend (from `frontend/`):

```powershell
npm install
npm run dev        # Vite dev server on 127.0.0.1:5173, proxies /api/* -> :8000
npm run build      # vue-tsc --noEmit type-check, then vite build
```

Health check: `http://127.0.0.1:8000/api/health`.

## Configuration & environment

- **MySQL only.** `core/database.py` hard-rejects any URL that is not `mysql+pymysql://` — there is no SQLite path.
- Settings resolution order (`backend/app/core/settings.py`): it loads a **hardcoded** `hermes-rabbit/backend/.env` (`C:\Users\Administrator\Documents\hermes-rabbit\backend\.env`, `RABBIT_DB_*` etc.) first without override, then `backend/.env` (`STOCK_*`) with override. So `STOCK_*` wins, falling back to `RABBIT_*`. The database name is forced to `hermes-stock` by default regardless of the rabbit env.
- `DATABASE_URL` (full SQLAlchemy URL), if set, overrides all `STOCK_DB_*`/`RABBIT_DB_*` pieces.
- `backend/.env` is git-ignored; `backend/.env.example` documents DB keys. LLM/data-source keys: `STOCK_LLM_BASE_URL`, `STOCK_LLM_API_KEY`, `STOCK_LLM_MODEL`, `STOCK_AGENT_MAX_STEPS`, `STOCK_TUSHARE_TOKEN`, `STOCK_TAVILY_API_KEY`. Vector-memory embeddings (`ai/memory.py`, ChromaDB): `STOCK_EMBEDDING_BASE_URL`/`STOCK_EMBEDDING_API_KEY`/`STOCK_EMBEDDING_MODEL` — decoupled from the chat model; if unset it falls back to the active LLM provider's `/embeddings`, and gracefully disables memory if none works. Admin bootstrap: `STOCK_ADMIN_USERNAME`/`STOCK_ADMIN_PASSWORD` are **not** hardcoded (default empty). An initial `super_admin` is seeded **only** when both are set in env **and** the users table has no enabled user; otherwise no default account is created — create users directly in the DB.
- This checkout **is** a git repo (remote `origin` = Gitee `china0310/hermes-stock`, default push branch `master`). `backend/.env` is git-ignored — never commit it.
- **Agent runtime env** (`STOCK_AGENT_*`): `STOCK_AGENT_RUNTIME_MODE` (engine: `legacy`/`agentscope`/`agentscope-stock`, default `agentscope`), `STOCK_AGENT_REDIS_URL` (durable-run broker — backs the queue/leases/pub-sub), `STOCK_AGENT_RUNTIME_WORKER_ENABLED` (run the worker in-process). Concurrency/limits: `STOCK_AGENT_WORKER_CONCURRENCY`, `STOCK_AGENT_TEAM_MAX_MEMBERS`/`_MAX_PARALLEL`, `STOCK_AGENT_USER_ACTIVE_LIMIT`, `STOCK_AGENT_QUEUE_LIMIT`. Loop/retry guards: `STOCK_AGENT_LOOP_MAX_ITERATIONS`, `STOCK_AGENT_RUN_MAX_ATTEMPTS`, `STOCK_AGENT_RUN_TIMEOUT_SECONDS`.

## Backend architecture

Single-responsibility modules grouped into **domain packages** under `backend/app/`. Entry points stay at the top level and must not move: `main.py` (uvicorn serves `backend.app.main:app`) and `scripts_init_db.py` (`python -m backend.app.scripts_init_db`). Internal imports are **absolute** — `from backend.app.<pkg>.<mod> import ...` (not relative dots).

```
backend/app/
  main.py  scripts_init_db.py      # 入口，勿移动（systemd/uvicorn/命令依赖）
  core/       settings  database  applog  models  llm_config
              request_context/request_id_middleware(trace_id)  response(ok/fail信封)
              startup_validator  operation_log  runtime_overrides(+_api)
  auth/       auth  auth_api
  data/       repository(按域 mixin)  deps  sse  positions_api  watchlist_api  dashboard_api
              report_api(报告详情/导出)  logs_api  broker_presets
  market/     stock_tools  digital_human_tools  company_research  position_decision  tushare_api  tushare_catalog
  agents/     stock_agent  position_agent  watchlist_agent  position_import(_v2)  position_intent  tool_approval
  agent_runtime/  service  executor  agentscope_engine  legacy_engine  worker  redis_broker  repository
                  team_service  workflow_service  workflow_templates  swarm_teams  orchestration_tools
                  evidence_service  artifact_service  tool_repository  tool_worker  transaction_state/channels
  ai/         ai_api  llm_client  memory(L1向量)  experience_store(L2经验库)  ledger_store(L3预测台账)
              prompt_recommendations  rag_ingest  retrieval  session_compact  query_rewrite
  quant/      backtest_api/engine/metrics  factor_api/service  screener_api/service(技术选股)
              agent_board_api/service  signals/  factors/(alpha101、gtja191)
  notify/     notification_service  notification_api(站内通知收件箱)
  paper/      paper_service  paper_api(模拟盘：虚拟账户/市价买卖/盈亏)
  feishu/     feishu(推送)  feishu_bot  feishu_bot_api
  scheduler/  rhythm_scheduler  rhythm_api
  seed/       seed  seed_api
```

Note: a `__file__`-relative path in a package module resolves via `parents[N]` counted from its **package depth** — e.g. `core/settings.py` uses `parents[2]` to reach `backend/`, `core/applog.py` uses `parents[3]` for the repo root. Adjust N if you move a file between depths.

- `core/settings.py` — frozen dataclass, builds `database_url`.
- `core/database.py` — engine/session/`Base`. Schema is created with `Base.metadata.create_all`; there is **no Alembic**. Additive migrations are hand-coded in `_ensure_schema_upgrades()` (raw `information_schema` checks + `ALTER TABLE`). Add new column/index migrations there.
- `core/models.py` — all SQLAlchemy models (~35 tables). Groups: instruments/watchlist/positions, market data (`daily_bars`, `documents`), rules/`events`/evidence, agents/reports/risk/reviews, and AI config (`llm_model_platforms`, `llm_model_configs`, `digital_human_profiles`, `ai_chat_sessions`/`messages`). `events` is the central traceability hub. See `docs/database.md`.
- `data/repository.py` — `StockRepository`, the single place for read/write queries and output-dict shaping (dashboard, events, positions, charts, watchlist, reports, risk, logs). It composes per-domain mixins (`position_query_mixin`, `position_write_mixin`, `watchlist_mixin`, `system_mixin`, ...).
- `main.py` — `create_app(init_db=True)` factory; the module-level `app` is what uvicorn serves. **It no longer holds CRUD endpoints** — routes are mounted from per-domain routers (`include_router`), each domain owns its controller/service/DAO. `data/` holds the data-domain routers (`positions_api`/`watchlist_api`/`dashboard_api`/`logs_api`, shared deps in `data/deps.py::get_repo`, SSE formatting in `data/sse.py`); `market/tushare_api.py`, `seed/seed_api.py` hold their own. New endpoints go into the owning domain's router, not `main.py`.
- `auth/auth.py` / `auth/auth_api.py` — custom HMAC-signed JWT-style tokens (not PyJWT), PBKDF2 password hashing. A static `ROLE_PERMISSIONS` map (`super_admin`/`admin`/`researcher`/`viewer`) plus per-user `permission_scope`. **Every** protected endpoint uses `Depends(require_permission("..."))` — follow this pattern for new endpoints rather than checking roles inline.

### Agent runtime & multi-agent orchestration (core feature)

This is a **multi-agent system with a durable execution runtime**, not a single inline agent. Chat entry `ai/ai_api.py::agent_chat` (`POST /api/agent/chat`) creates a durable run via `agent_runtime/service.py::AgentRuntimeService.create_run`, which selects an engine and enqueues the run on a broker (Redis when `STOCK_AGENT_REDIS_URL` is set). A background `AgentRuntimeWorker` (started in the lifespan when `STOCK_AGENT_RUNTIME_WORKER_ENABLED`) executes runs through `agent_runtime/executor.py::AgentRunExecutor`; the client streams events back via `stream_events` (broker pub/sub + a durable event log), so **SSE is decoupled from the worker** and survives restarts/reconnects.

**Two engines** — chosen by `agent_runtime/agentscope_engine.py::select_engine` from `STOCK_AGENT_RUNTIME_MODE` (`legacy` | `agentscope` | `agentscope-stock`, default `agentscope`):
- `legacy` — `run_legacy` wraps the original ReAct generator `agents/stock_agent.py::run_agent_events` (OpenAI-compatible streaming tool loop). Kept as the explicit rollback path.
- `agentscope` — `run_agentscope` builds an AgentScope `Agent` (`ReActConfig` + `Toolkit`) with checkpoint/resume, tool-approval pause, and a `ReasoningLoopGuard` (hard cap `STOCK_AGENT_LOOP_MAX_ITERATIONS`, duplicate-tool limit).

**Durable-run features**: checkpoint/resume, cancel, approval waiting (`waiting_approval`), team/workflow child waiting (`waiting_children`), and retry-with-backoff for transient errors (`STOCK_AGENT_RUN_MAX_ATTEMPTS`). Runs persist to `AgentRun`; leases/locks via Redis (`STOCK_AGENT_LOCK_TTL_SECONDS`). On completion the executor schedules memory digestion + prompt-recommendation refresh (`_schedule_post_run`).

**Multi-agent orchestration** — the lead agent spawns sub-agent runs, all coordinated through the run/broker layer:
- **Teams** — `dispatch_agent_tasks` tool → `team_service.py::AgentTeamService.start_team`: a leader run fans out up to `STOCK_AGENT_TEAM_MAX_MEMBERS` member runs (digital humans assigned a role+task, parallelism `STOCK_AGENT_TEAM_MAX_PARALLEL`) and aggregates their evidence when children terminate.
- **Workflows** — `run_agent_workflow` tool → `workflow_service.py::AgentWorkflowService.start_workflow`: runs a templated DAG of nodes from `workflow_templates.py` (量化策略研究台 / 组合复盘委员会 / **投资决策委员会**（多空并行 + 风控质询 + 终裁）/ 基本面深度研究组 / 风险委员会), unlocking nodes as dependencies finish, with per-node retry.
- **Delegation batches** — `create_agent_task_batch` → `AgentDelegation`: kanban-style tasks handed to other digital humans by `agent_code` (Chinese-name aliases in `AGENT_CODE_ALIASES`).
- **Swarm teams** — `agent_runtime/swarm_teams.py` predefined role combos (`full-research` / `risk-review` / `stock-pick` / `position-review` / `event-response`), served at `/api/agent/swarm-teams`; the frontend triggers them via `//` in the chat box.

**Agent roster**: research-analyst（研究员）、intelligence-officer（情报官）、risk-officer（风控官）、quant-analyst（量化分析师）, plus transaction agents `position-entry-agent` / `watchlist-entry-agent`; `stock-pm`/`ask_stock` are excluded from delegation.

**Chat modes & toolsets** — assembled per mode/digital-human in `agentscope_engine._build_definition`: `research` uses generic DB tools (`run_sql`, `get_schema`) + per-digital-human tools (`market/digital_human_tools.py`); `stock` uses the market toolset in `market/stock_tools.py` (TuShare daily/quotes/indicators, Tavily web search, MA/pattern/chip, backtests) with a built-in backtest router. The active model resolves from `LlmModelConfig` (`is_active`) falling back to `STOCK_LLM_*` env (agentscope resolves via `active_llm_config`).

**`run_sql` is a hard read-only sandbox** (`validate_sql` + `FORBIDDEN_SQL` regex): single statement, `SELECT`-only, no comments, no system schemas, auto-`LIMIT`. Every call is appended to `backend/agent_audit.log`. Keep these guards intact.

**Memory tiers** (`ai/`): L1 vector memory `memory.py` (recall → rerank), L2 cross-user review-experience store `experience_store.py` (sample_size/statistical_strength, market-env tags), L3 append-only hash-chained prediction ledger `ledger_store.py`.

Adjacent flows: `agents/position_agent.py` (natural-language / screenshot position entry), `agents/position_import(_v2).py` (vision parsing into `position_holding_snapshots`), `agents/watchlist_agent.py` (watchlist entry). `ai/prompt_recommendations.py` runs a background scheduler started/stopped in the app lifespan.

### Engineering infrastructure (observability & ops)

- **trace_id 全链路**：`core/request_id_middleware.py`（纯 ASGI，最外层）读 `X-Request-ID`（仅放行字母数字与 `-_`，≤64）或生成 16 位 hex，写入 `core/request_context.py` 的 ContextVar，并回显 `X-Trace-ID` 响应头；`applog` 的格式串含 `%(trace_id)s`，业务日志自动串联。
- **统一响应信封**：`core/response.py::ok()/fail()` —— 本批新增端点（通知/选股/报告/模拟盘/运行时配置）统一返回 `{success,data,message,trace_id}`；既有端点不改（不破坏前端）。
- **操作审计**：`core/operation_log.py` 中间件把 `/api/*` 写操作（POST/PUT/PATCH/DELETE，SSE 除外）落 `operation_logs`（user_id 无外键、解析 bearer 复用 `decode_access_token`、线程池入库、失败只告警）。开关 `STOCK_OPERATION_LOG_ENABLED`。
- **启动校验**：`core/startup_validator.py::validate_startup`（lifespan 第一步，线程池执行）：REQUIRED=DB 可达（fail-fast）；RECOMMENDED=auth_secret/LLM/Tushare/Tavily 占位符检测（只告警——未配 auth_secret 时应用按设计以随机密钥降级运行）。
- **运行时配置覆盖**：`core/runtime_overrides.py` + `runtime_config_overrides` 表，白名单 key/value 免重启覆盖（目前仅 `agent_max_steps`，经 `active_llm_config` 生效，优先于 DB 模型配置与环境变量）。新增可覆盖参数必须先登记进 `SUPPORTED_OVERRIDES`。
- **站内通知**：`notify/`（`inbox_notifications` 表，勿与既有 `notifications` 外部推送流水表混淆）；投递入口 `notification_service.create_notification`。
- **技术选股**：`POST /api/screener/run` —— 候选池=本地已缓存最新价的股票（`stock_latest_prices` JOIN `instruments`），基础条件纯 SQL，技术条件（量比/站上 5/10/20 均线）用窗口查询一次取 K 线在 Python 计算，候选池截断 300。数据按需缓存，选股器不外呼拉全市场。
- **报告导出**：`data/report_api.py` —— `GET /api/reports/{report_id}`（全量结构化）、`/{report_id}/export?format=md|json`、`GET /api/reports/export.csv`（BOM 兼容 Excel，与 backtest trades.csv 同款）。
- **模拟盘**：`paper/` 三表（`paper_accounts`/`paper_positions`/`paper_orders`，与真实持仓域完全隔离、无外键）；市价单即时成交、成交价=`stock_latest_prices` 缓存（无缓存拒绝下单）、一手 100 股、单边万 2.5 手续费、账户行 `with_for_update()` 防双花。**仍是研究演练，无任何真实交易通道。**

## Frontend architecture

Vue 3 + TypeScript + Vite, no router library — `App.vue` switches views via an `activePage` string. Views in `frontend/src/views/` (Workbench, AskStock, PositionChart, Settings, model/data-source config, DigitalHumanCenter, Login). `api.ts` centralizes fetch calls and holds the bearer token in `localStorage` (`hermes_stock_token`); `types.ts` mirrors backend payloads. SSE chat responses are consumed manually from `fetch` streams. Charts use `lightweight-charts`; assistant markdown is rendered via `markdown-it` + `dompurify` (`utils/markdown.ts`).

## Conventions

- Backend user-facing strings (API error `detail`, agent prompts, tool descriptions) are in **Chinese** — match this in new endpoints and tools.
- **新增端点**：用 `core/response.py` 的 `ok()/fail()` 信封 + `JSONResponse`（业务错误 400/404），鉴权走 `require_permission`；新表直接加进 `core/models.py`（`Base.metadata.create_all` 自动建表，无需 ALTER）；日志/审计类新表不加外键（只增表，与用户/标的生命周期解耦）。
- When adding a market/agent tool, register its schema (`STOCK_TOOL_SCHEMAS` or the digital-human schemas) and add an executor branch. Then wire it into the right engine: for `legacy`, add it to the allow-list set in `run_agent_events`; for `agentscope`, register it in `agentscope_engine` (`_build_definition`/`_build_tools`). Write-capable tools must respect the approval/transaction guards (`agents/tool_approval.py`, `transaction_state.py`).
- Docs worth reading before large changes: `docs/database.md` (table groups & design principles) and `docs/ask-stock.md` (the "问股"/ask-stock tool chain and DSA table mapping).
