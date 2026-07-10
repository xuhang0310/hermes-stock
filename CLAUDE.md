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

- **MySQL only.** `database.py` hard-rejects any URL that is not `mysql+pymysql://` — there is no SQLite path.
- Settings resolution order (`backend/app/settings.py`): it loads a **hardcoded** `hermes-rabbit/backend/.env` (`C:\Users\Administrator\Documents\hermes-rabbit\backend\.env`, `RABBIT_DB_*` etc.) first without override, then `backend/.env` (`STOCK_*`) with override. So `STOCK_*` wins, falling back to `RABBIT_*`. The database name is forced to `hermes-stock` by default regardless of the rabbit env.
- `DATABASE_URL` (full SQLAlchemy URL), if set, overrides all `STOCK_DB_*`/`RABBIT_DB_*` pieces.
- `backend/.env` is git-ignored; `backend/.env.example` documents DB keys. LLM/data-source keys: `STOCK_LLM_BASE_URL`, `STOCK_LLM_API_KEY`, `STOCK_LLM_MODEL`, `STOCK_AGENT_MAX_STEPS`, `STOCK_TUSHARE_TOKEN`, `STOCK_TAVILY_API_KEY`. Admin bootstrap: `STOCK_ADMIN_USERNAME`/`STOCK_ADMIN_PASSWORD` are **not** hardcoded (default empty). An initial `super_admin` is seeded **only** when both are set in env **and** the users table has no enabled user; otherwise no default account is created — create users directly in the DB.
- This checkout is **not a git repository** — `git` commands will fail. Don't assume version-control state.

## Backend architecture

Layered, single-responsibility modules under `backend/app/`:

- `settings.py` — frozen dataclass, builds `database_url`.
- `database.py` — engine/session/`Base`. Schema is created with `Base.metadata.create_all`; there is **no Alembic**. Additive migrations are hand-coded in `_ensure_schema_upgrades()` (raw `information_schema` checks + `ALTER TABLE`). Add new column/index migrations there.
- `models.py` — all SQLAlchemy models (~35 tables). Groups: instruments/watchlist/positions, market data (`daily_bars`, `documents`), rules/`events`/evidence, agents/reports/risk/reviews, and AI config (`llm_model_platforms`, `llm_model_configs`, `digital_human_profiles`, `ai_chat_sessions`/`messages`). `events` is the central traceability hub. See `docs/database.md`.
- `repository.py` — `StockRepository`, the single place for read queries and output-dict shaping (dashboard, events, positions, charts, watchlist, reports, risk, logs).
- `main.py` — `create_app(init_db=True)` factory; the module-level `app` is what uvicorn serves. CRUD/read endpoints live here; AI and auth endpoints are mounted from routers.
- `auth.py` / `auth_api.py` — custom HMAC-signed JWT-style tokens (not PyJWT), PBKDF2 password hashing. A static `ROLE_PERMISSIONS` map (`super_admin`/`admin`/`researcher`/`viewer`) plus per-user `permission_scope`. **Every** protected endpoint uses `Depends(require_permission("..."))` — follow this pattern for new endpoints rather than checking roles inline.

### The ReAct agent (core feature)

`stock_agent.py::run_agent_events(...)` is the heart of the chat system — a synchronous generator implementing a ReAct tool-calling loop against an **OpenAI-compatible** `/chat/completions` endpoint (streamed with `httpx`, parsed by `_parse_sse_events`). It yields typed events (`model_selected`, `reasoning_delta`, `content_delta`, `content_end`, `action`, `observation`, `tool_start/tool_done`, `error`, `done`).

- `ai_api.py::agent_chat` (`POST /api/agent/chat`) wraps that generator: it runs the blocking producer in a thread, bridges events into an asyncio queue, streams them to the client as SSE, and persists the final assistant message.
- **Two chat modes** (`chat_mode`): `research` uses generic DB tools (`run_sql`, `get_schema`) plus per-digital-human tools (`digital_human_tools.py`); `stock` uses the market toolset in `stock_tools.py` (TuShare daily/quotes/indicators, Tavily news search, MA/pattern/chip analysis).
- The active model is resolved from the DB (`LlmModelConfig` where `is_active`), falling back to `STOCK_LLM_*` env. Model platforms/configs are user-managed via the model-config endpoints.
- **`run_sql` is a hard read-only sandbox** (`validate_sql` + `FORBIDDEN_SQL` regex): single statement, `SELECT`-only, no comments, no system schemas, auto-`LIMIT`. Every call is appended to `backend/agent_audit.log`. Keep these guards intact.

Adjacent agent flows: `position_agent.py` (natural-language / screenshot position entry, streamed) and `position_import.py` (vision-model screenshot parsing into `position_holding_snapshots`). `prompt_recommendations.py` runs a background scheduler started/stopped in the app lifespan.

## Frontend architecture

Vue 3 + TypeScript + Vite, no router library — `App.vue` switches views via an `activePage` string. Views in `frontend/src/views/` (Workbench, AskStock, PositionChart, Settings, model/data-source config, DigitalHumanCenter, Login). `api.ts` centralizes fetch calls and holds the bearer token in `localStorage` (`hermes_stock_token`); `types.ts` mirrors backend payloads. SSE chat responses are consumed manually from `fetch` streams. Charts use `lightweight-charts`; assistant markdown is rendered via `markdown-it` + `dompurify` (`utils/markdown.ts`).

## Conventions

- Backend user-facing strings (API error `detail`, agent prompts, tool descriptions) are in **Chinese** — match this in new endpoints and tools.
- When adding a market/agent tool, register its schema (`STOCK_TOOL_SCHEMAS` or the digital-human schemas), add an executor branch, and add it to the allow-list set in `run_agent_events` so its events stream correctly.
- Docs worth reading before large changes: `docs/database.md` (table groups & design principles) and `docs/ask-stock.md` (the "问股"/ask-stock tool chain and DSA table mapping).
