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
              report_api(报告详情/导出)  share_api/share_service/share_render(手机版分享页)  logs_api  broker_presets
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
  plans/      rules  service  watcher  page(结构化挂单计划：校验/生效/到价提醒/成交对账/计划页)
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

**Durable-run features**: checkpoint/resume, cancel, approval waiting (`waiting_approval`), team/workflow child waiting (`waiting_children`), and retry-with-backoff for transient errors (`STOCK_AGENT_RUN_MAX_ATTEMPTS`). **Model fallback** (`executor._try_model_fallback`): on a model-provider error (relay 400 "生成失败", 5xx, stream interruption, access/key errors) a side-effect-free run is re-queued on the next enabled model in `resolve_chat_model_config_ids` order (other platforms first), at most 2 switches, tracked in the request's `failed_model_config_ids`; transient errors first retry the same model once, non-retryable ones switch immediately. Exceptions with an empty message are recorded by type name (`exception_text`) so they can still be classified. Runs persist to `AgentRun`; leases/locks via Redis (`STOCK_AGENT_LOCK_TTL_SECONDS`). On completion the executor schedules memory digestion + prompt-recommendation refresh (`_schedule_post_run`).

**Multi-agent orchestration** — the lead agent spawns sub-agent runs, all coordinated through the run/broker layer:
- **Teams** — `dispatch_agent_tasks` tool → `team_service.py::AgentTeamService.start_team`: a leader run fans out up to `STOCK_AGENT_TEAM_MAX_MEMBERS` member runs (digital humans assigned a role+task, parallelism `STOCK_AGENT_TEAM_MAX_PARALLEL`) and aggregates their evidence when children terminate.
- **Workflows** — `run_agent_workflow` tool → `workflow_service.py::AgentWorkflowService.start_workflow`: runs a templated DAG of nodes from `workflow_templates.py` (量化策略研究台 / 组合复盘委员会 / **投资决策委员会**（多空并行 + 风控质询 + 终裁）/ 基本面深度研究组 / 风险委员会), unlocking nodes as dependencies finish, with per-node retry.
- **Delegation batches** — `create_agent_task_batch` → `AgentDelegation`: kanban-style tasks handed to other digital humans by `agent_code` (Chinese-name aliases in `AGENT_CODE_ALIASES`).
- **Swarm teams** — `agent_runtime/swarm_teams.py` predefined role combos (`full-research` / `risk-review` / `stock-pick` / `position-review` / `event-response`), served at `/api/agent/swarm-teams`; the frontend triggers them via `//` in the chat box.

**Agent roster**: research-analyst（研究员）、intelligence-officer（情报官）、risk-officer（风控官）、quant-analyst（量化分析师）, plus transaction agents `position-entry-agent` / `watchlist-entry-agent`; `stock-pm`/`ask_stock` are excluded from delegation.

**Chat modes & toolsets** — assembled per mode/digital-human in `agentscope_engine._build_definition`: `research` uses the documented shared-schema helper (`get_schema`) plus per-digital-human structured tools (`market/digital_human_tools.py`); `stock` uses the market toolset in `market/stock_tools.py` (TuShare daily/quotes/indicators, Tavily web search, MA/pattern/chip, backtests) with a built-in backtest router. The active model resolves from `LlmModelConfig` (`is_active`) falling back to `STOCK_LLM_*` env (agentscope resolves via `active_llm_config`).

**Role prompts have one source**: in `research` mode the role section comes from `digital_human_profiles.system_prompt` (editable on 数字人管理, takes effect next run) via `digital_human_skill_prompt`; `ROLE_SKILLS` is only the default (`default_role_prompt`, exposed as `default_system_prompt` for “恢复默认”), and `seed.upgrade_role_prompts` replaces untouched legacy seed one-liners at startup. Tool-bound hard rules (e.g. the stock-pm position-write flow) live in `ROLE_FIXED_RULES`, not in the editable text. Context builders (web chat, scheduler, Feishu, team, workflow, board) omit the prompt line for role profiles (`is_role_profile`) so it is not injected twice; 问股助手/录入 Agent keep theirs in the context.

**Agent SQL is user-scoped, read-only, and AST-checked** (`agents/sql_sandbox.py`, used by `stock_agent.run_sql`). sqlglot parses the query (MySQL dialect); only one SELECT/UNION is accepted. Every table reference — including inside subqueries, CTEs, UNIONs and LATERAL — is classified against an explicit allowlist: shared research tables pass through; personal tables (`PERSONAL_TABLES`, e.g. positions/watchlist/events/reports) are replaced with `(SELECT * FROM t WHERE owner = <run user>) AS <alias>` (`events`/`backtest_runs` also include NULL-owner public rows); everything else (users, model/data-source keys, notify channels, logs, runtime tables, schema-qualified refs) is rejected. The SQL that runs is regenerated from the tree without comments or hints, so `/*! ... */` payloads never reach MySQL. Execution uses a separate pool whose sessions are `READ ONLY` with `max_execution_time`. Both engines pass the run's `user_id` explicitly (`run_sql(sql, owner_user_id=...)`); admins see only their own rows. When adding a table, classify it in `sql_sandbox.py` — unlisted tables are not queryable. `get_schema` lists exactly the queryable tables and marks personal ones. Calls are audited to `backend/agent_audit.log`.

**Answer style (说人话)** — `agents/answer_style.py::ANSWER_STYLE_RULES` is injected into all three base prompts (`stock_agent._system_prompt`, `_knowledge_system_prompt`, `stock_tools.stock_system_prompt*`), and the agentscope loop appends `ANSWER_STYLE_REMINDER` after skill context. Final answers lead with a plain conclusion, back it with dated numbers, phrase actions as "如果……就……", and never print evidence grades (A级/C级), confidence labels, tool/field names or internal data-correction chatter. Skill `SKILL.md` output sections and role `回答要讲到` lists describe **content to cover, not headings** — keep them that way when editing skills (`tests/test_answer_style.py` guards it). The chat renderer (`frontend/src/utils/markdown.ts`) relaxes `*` emphasis flanking next to CJK characters so `**结论：**后文` renders as bold.

**Scheduled-report plain rewrite (去 AI 味)** — client-written task prompts are full of checking rules, and agents tend to recite them ("不跨账户挪用""不构成授权""不冒充…"). Two layers handle it: `_scheduled_execution_constraints` tells every scheduled run that the system pushes Feishu itself and that task rules are to be followed, not recited (gaps go once at the end); and `_run_agent_report` passes the answer through `ai/plain_rewrite.py::rewrite_plain` before storing/pushing — one extra LLM call that changes wording only. `check_facts_kept` rejects the edit (falls back to the original) if it introduces any key number not in the original (prices/shares/amounts/percentages, so no rounding or unit changes), drops a 6-digit code, drops >2% of key numbers, shrinks below 35%, or was truncated. Reports under 600 or over 16000 chars are pushed as-is. The original stays in the scheduler chat session. Switch: `STOCK_PLAIN_REWRITE_ENABLED` (default true). Upstream, the `instruction` field of `create_agent_schedule`/`update_agent_schedule` (`orchestration_tools._SCHEDULE_INSTRUCTION_GUIDE`) tells the stock-pm how to write task prompts (stay close to the user's own words; don't turn one-off agreements or self-invented limits into standing rules; no "推送飞书"; no checklists/disclaimers); see `docs/scheduled-task-prompts.md` for why.

**AgentScope loop guards** (`agent_runtime/agentscope_engine.py`, all from production incidents): (1) if a reply ends empty or as a short progress note ("接下来我去查……"), `reply_events()` re-prompts the same agent up to `_UNFINISHED_ANSWER_CONTINUATIONS` times (`continuation_started` event; the chat views move the note into steps), and a still-empty answer fails visibly instead of storing a blank reply; (2) memory compression uses a one-field Chinese summary schema and `_resilient_agent_class` falls back to `_trim_context_without_model` when structured summarisation fails (relies on AgentScope private methods — re-check on upgrade); (3) per-tool-result cap `_TOOL_RESULT_TOKEN_LIMIT`. Executor retries also cover stream interruptions/overload/deadlocks, with 20s/60s backoff for non-interactive sources. For `web`/`feishu` sources the stock-pm gets `INTERACTIVE_ORCHESTRATION_PROMPT` (answer directly; workflows only when explicitly asked). Skill `roles` only scope research-mode digital humans — stock mode gets every tool declared for `stock`.

**Memory tiers** (`ai/`): L1 vector memory `memory.py` (recall → rerank; never stores holdings/trades/prices/cash — `is_volatile_memory` filters them at digestion and recall — injects each memory with its date, and `archive_stale_memories` archives snapshots, gaps older than 60 days and idle old facts after each digestion), L2 cross-user review-experience store `experience_store.py` (sample_size/statistical_strength, market-env tags), L3 append-only hash-chained prediction ledger `ledger_store.py`.

**Position sync wizard (持仓同步)** — the positions page's main entry (`components/PositionSyncWizard.vue`, replaced the single-image OCR modal): `POST /api/positions/sync/recognize` reads up to 6 screenshots in parallel (`data/position_sync.py`, own vision tool `submit_position_screenshot` that also extracts funds and account no./broker), merges/dedupes rows, detects the account (account-number tail, then broker/App name aliases; only unique matches), and returns a change list from `PositionSyncMixin.position_sync_preview` (new / changed / missing / unchanged / invalid; missing rows default to “close” unless the screenshot market value doesn't match the account's, i.e. likely incomplete). `/sync/preview` recomputes for another account without calling the model; `/sync/apply` writes in one transaction as a **correction** (`source_type=screenshot_sync` snapshots, stop/target/thesis carried forward, snapshot date = today so it is never ignored as stale) and records trades only for rows the user ticked “这是一笔买入/卖出”; `/sync/{batch_id}/undo` deletes the batch's snapshots/trades and restores the pre-sync cash, refusing if the account changed afterwards. Screenshot imports are never trades: agent OCR extraction forces `action=replace`, and `position_trades` rows with `source_type=screenshot_import` (July pseudo-trades) are hidden from the trade list unless `include_imports=true`.

Adjacent flows: `agents/position_agent.py` (natural-language / screenshot position entry), `agents/position_import(_v2).py` (vision parsing into `position_holding_snapshots`), `agents/watchlist_agent.py` (watchlist entry). `ai/prompt_recommendations.py` runs a background scheduler started/stopped in the app lifespan.

### Engineering infrastructure (observability & ops)

- **trace_id 全链路**：`core/request_id_middleware.py`（纯 ASGI，最外层）读 `X-Request-ID`（仅放行字母数字与 `-_`，≤64）或生成 16 位 hex，写入 `core/request_context.py` 的 ContextVar，并回显 `X-Trace-ID` 响应头；`applog` 的格式串含 `%(trace_id)s`，业务日志自动串联。
- **统一响应信封**：`core/response.py::ok()/fail()` —— 本批新增端点（通知/选股/报告/模拟盘/运行时配置）统一返回 `{success,data,message,trace_id}`；既有端点不改（不破坏前端）。
- **操作审计**：`core/operation_log.py` 中间件把 `/api/*` 写操作（POST/PUT/PATCH/DELETE，SSE 除外）落 `operation_logs`（user_id 无外键、解析 bearer 复用 `decode_access_token`、线程池入库、失败只告警）。开关 `STOCK_OPERATION_LOG_ENABLED`。
- **启动校验**：`core/startup_validator.py::validate_startup`（lifespan 第一步，线程池执行）：REQUIRED=DB 可达（fail-fast）；RECOMMENDED=auth_secret/LLM/Tushare/Tavily 占位符检测（只告警——未配 auth_secret 时应用按设计以随机密钥降级运行）。
- **运行时配置覆盖**：`core/runtime_overrides.py` + `runtime_config_overrides` 表，白名单 key/value 免重启覆盖（目前仅 `agent_max_steps`，经 `active_llm_config` 生效，优先于 DB 模型配置与环境变量）。新增可覆盖参数必须先登记进 `SUPPORTED_OVERRIDES`。
- **站内通知**：`notify/`（`inbox_notifications` 表，勿与既有 `notifications` 外部推送流水表混淆）；投递入口 `notification_service.create_notification`。前端入口是顶栏铃铛 `components/NotificationBell.vue`（60 秒轮询未读数，`utils/notifications.ts` 把 link 映射到页面）。时间按 UTC 存、带时区输出（生产库 `NOW()` 是 UTC），前端用 `utils/datetime.ts::beijingFromDb` 显示。
- **技术选股**：`POST /api/screener/run` —— 候选池=最近一次全市场截面（`stock_latest_prices` 中 trade_date 的众数）上仍在交易的非 ST 股票（JOIN `instruments`）；基础条件纯 SQL，技术条件（站上 5/10/20 均线）对**全部**命中股票分批取 K 线在 Python 计算（按 `stock_adj_factors` 前复权，缺因子退回不复权并标 `ma_adjusted=false`）。综合分（估值规模/动量/流动性分位分 × 权重）与排序在服务端对全部命中计算，`universe`=候选池、`total`=真实命中数、`items`=排序后前 limit 只。选股器不外呼拉全市场。
- **复权**：`daily_bars` 是不复权价。凡跨交易日比较价格（自动选股动量/波动、前瞻评估收益、选股器均线）都必须乘同日 `stock_adj_factors.adj_factor`；缺因子按缺失处理，不得退回原始价冒充。
- **自动选股页面**：`/automatic-selection` 面向普通用户（今日选股 / 历史战绩 / 选股方法；有 `model:manage` 或 `schedule:manage` 才见“高级”审计台 `components/AutomaticSelectionAudit.vue`）。白话内容全部由 `quant/automatic_selection_presentation.py` 从落库证据派生（维度 0–100 分、理由、规则风险、选出后表现、历史战绩），接口 `GET /api/automatic-selection/daily`、`/track-record`。呈现层只读，不得回写排名或改动 `DEFAULT_STRATEGY_CONFIG`/评分实现（会改变 config_hash/指纹）。详见 `docs/automatic-selection.md`。
- **自动选股时间口径**：目标交易日 = 不晚于 `market_sync.selection_target_ceiling()` 的最近开市日（北京时间 17:00 前用前一日，TuShare 当天数据此前未发齐）。内置入库 17:30（失败每 30 分钟重试至 22:00）、排名 17:50；任务键名 `automatic-selection-data-1545`/`automatic-selection-1610` 是历史 ID，与实际时间无关。
- **报告导出**：`data/report_api.py` —— `GET /api/reports/{report_id}`（全量结构化）、`/{report_id}/export?format=md|json`、`GET /api/reports/export.csv`（BOM 兼容 Excel，与 backtest trades.csv 同款）。
- **手机版分享页**：`send_feishu(share=ShareSpec(...))` 在配置了 `STOCK_PUBLIC_BASE_URL` 时存一份正文快照到 `report_shares`（和卡片同一份，白话改写之后的版本），卡片上加“手机查看完整版”按钮（第一张，消息多于一张时最后一张也有）；没配时推送和原来完全一样。公开页 `GET /api/share/{id}`（`data/share_api.py`）不需要登录，链接即凭证（128 位随机，默认 `STOCK_SHARE_TTL_DAYS`=30 天，可 `POST /api/shares/{id}/revoke` 作废），表不登记进 `sql_sandbox`，数字人查不到。`data/share_render.py` 服务端出自包含 HTML：markdown-it 关原始 HTML + nh3 白名单，无脚本、无外部资源，严格 CSP；中文旁 `**` 的放宽规则与前端 `utils/markdown.ts` 一致（子类覆盖 `scanDelims`，依赖 markdown-it-py 内部结构，升级时看 `tests/test_share_pages.py`）。计划表（按表头识别）和长文字表窄屏变卡片，其他表横向滑动、首列固定。分享失败或飞书拒收带按钮的卡片都不影响送达（后者去掉按钮重发并告警）。新增要带手机版的推送传 `share` 即可。报告类推送（定时数字人报告、早盘舆情简报、每日选股）再传 `card_brief`：有链接时卡片只有标题、一句核心的话、按钮和页脚，长报告也只发一张，详细内容都在手机版里（传字符串就用它；传 True 由 `share_render.lead_brief` 取正文开头那段的第一行，不成句就接下一行，超过 80 字按句子截）；没有链接、取句出错或飞书拒收时照旧发全文，内容不会丢。触价提醒本身就是要马上执行的内容，不传 `card_brief`。开关 `STOCK_FEISHU_BRIEF_CARD`（默认开）。执行约束和白话改写都要求第一行单独写这一句，取句规则只是兜底，要改那一句的质量先改约束。设置页“测试”按钮会带一个 1 天有效的虚构示例页，用来验收卡片和排版。页面是研报版式（只有暗色）：颜色 token 取值抄自 `styles.css`，因为页面不能依赖前端打包，站内改 token 时要同步 `share_render._PAGE_CSS`（`tests/test_share_pages.py` 会对比）；抬头和放大的数字用内嵌的 Newsreader 子集（`share_assets.py`），这是 `docs/design-system.md` 写明的例外。定时任务的执行约束（`_scheduled_execution_constraints`）要求一行一只的内容都写成 Markdown 表格、第一列带代码，白话改写也不许拆表，这是版式稳定的前提；模板对旧写法做了兜底，新格式优先改约束而不是再加解析。计划表和看盘表按表头拆成账本行，`★名称（代码）｜现价｜涨跌幅` 开头或全用竖线分隔的股票列表也一样：名称行放代码、账户、现价和涨跌幅，金色大字只给计划价（价格列或理由里写明的），现价永远是小字；拆不干净时整句留在下面，不丢字。结论只放大第一句，行情时间和免责这类短说明变小字。顶部笔数统计和计划入口来自 `meta.plan_stats`/`plan_url`，盘中提醒这类不出计划的报告带今天已生效的计划（`current_plan_meta`），没有就不显示。
- **挂单计划与到价提醒**（`plans/`，规则见 `docs/digital-human-upgrade-plan.md` 第 2 节）：`rules.plan_source_for_job` 认出出计划的定时任务（内置按 job_key，用户自建按名称，如“挂单清单”“挂单调整”），`_run_agent_report` 给它加 `save_trade_plan` 工具和一句提示；其他任务流程不变。工具当场校验（名单、账户、交易单位、可卖数量、涨跌停、资金来源），存成 draft；报告定稿后 `_finish_trade_plan` 和最终正文核对，对不上让数字人补存一次（`:plan-repair` 运行，最多等 240 秒，答复不用），仍对不上的条目 `text_matched=false` 不盯价并在报告末尾注明；然后 `activate_for_report` 生效并替代旧计划。**先存后生效**是为了保证客户先看到报告才收到提醒，任务失败的计划永不提醒。交易时段内手动跑出明天才生效的计划，不替代今天的计划（`session_live`）。盯价挂在每分钟价格同步后面（`_check_trade_plans` → `watcher.check_plan_triggers`），不调模型；每位用户只盯已生效里最新的一份；基准是第一次盯价时的当日最高/最低价，避免午盘计划被上午的高点误触发；条目先落状态再推送，失败 30 分钟内每分钟重试；盯价失败不影响价格同步。条目状态和成交结果**只由** `reconcile_owner` 从 `position_trades` 推出，可反复算，删错单会自动回退；止损和挂单卖是同一批股票的两个出口，止损不计入卖出合计和资金来源，持仓卖光后不再提醒另一个。晚间日报的“计划和实际对照”由 `reconciliation_text` 注入。计划页是 `source_type=trade_plan` 的分享页（`LIVE_SHARE_SOURCES`），每次打开实时算。两张表登记为 `sql_sandbox` 个人表。legacy 引擎没有这个工具，报告末尾会注明没有到价提醒。
- **模拟盘**：`paper/` 三表（`paper_accounts`/`paper_positions`/`paper_orders`，与真实持仓域完全隔离、无外键）；市价单即时成交、成交价=`stock_latest_prices` 缓存（无缓存拒绝下单）、一手 100 股、单边万 2.5 手续费、账户行 `with_for_update()` 防双花。**仍是研究演练，无任何真实交易通道。**

## Frontend architecture

Vue 3 + TypeScript + Vite, no router library — `App.vue` switches views via an `activePage` string. Views in `frontend/src/views/` (Workbench, AskStock, PositionChart, Settings, model/data-source config, DigitalHumanCenter, Login). `api.ts` centralizes fetch calls and holds the bearer token in `localStorage` (`hermes_stock_token`); `types.ts` mirrors backend payloads. SSE chat responses are consumed manually from `fetch` streams. Charts use `lightweight-charts`; assistant markdown is rendered via `markdown-it` + `dompurify` (`utils/markdown.ts`).

## Conventions

- Backend user-facing strings (API error `detail`, agent prompts, tool descriptions) are in **Chinese** — match this in new endpoints and tools.
- **新增端点**：用 `core/response.py` 的 `ok()/fail()` 信封 + `JSONResponse`（业务错误 400/404），鉴权走 `require_permission`；新表直接加进 `core/models.py`（`Base.metadata.create_all` 自动建表，无需 ALTER）；日志/审计类新表不加外键（只增表，与用户/标的生命周期解耦）。
- When adding a market/agent tool, register its schema (`STOCK_TOOL_SCHEMAS` or the digital-human schemas) and add an executor branch. Then wire it into the right engine: for `legacy`, add it to the allow-list set in `run_agent_events`; for `agentscope`, register it in `agentscope_engine` (`_build_definition`/`_build_tools`). Write-capable tools must respect the approval/transaction guards (`agents/tool_approval.py`, `transaction_state.py`).
- Docs worth reading before large changes: `docs/database.md` (table groups & design principles) and `docs/ask-stock.md` (the "问股"/ask-stock tool chain and DSA table mapping).
