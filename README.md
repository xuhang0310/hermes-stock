# SixRabbits Stock

A-share multi-agent research and risk-control workspace.

> Product/display name is **SixRabbits Stock**. Internal identifiers (repo dir, Python package, MySQL database `hermes-stock`, `STOCK_*`/`RABBIT_*` env keys) keep the `hermes` naming for continuity — do not rename those.

The backend is organized into domain packages under `backend/app/` (entry points `main.py` / `scripts_init_db.py` stay at the top level):

```text
backend/app/main.py            FastAPI factory + CRUD/read endpoints (uvicorn entry)
backend/app/scripts_init_db.py create-tables + seed CLI
backend/app/core/              settings · database · applog · models
backend/app/auth/              auth service + auth API
backend/app/data/              repository · broker presets
backend/app/market/            stock_tools · digital_human_tools (行情/问股)
backend/app/agents/            stock_agent (ReAct) · position_agent · position_import
backend/app/ai/                ai_api · prompt_recommendations
backend/app/feishu/            feishu push · feishu_bot · feishu_bot_api
backend/app/scheduler/         rhythm_scheduler · rhythm_api (定时任务)
backend/app/seed/              demo bootstrap data
frontend/                      Vue 3 workspace
```

## Database

The backend uses MySQL only.

By default it reuses `hermes-rabbit/backend/.env` database connection values:

```text
RABBIT_DB_HOST
RABBIT_DB_PORT
RABBIT_DB_USER
RABBIT_DB_PASSWORD
```

The database name is overridden to:

```text
hermes-stock
```

You can also create `backend/.env` locally with `STOCK_DB_*` values. This file is ignored by git.

Ask-stock data sources:

```text
STOCK_TUSHARE_TOKEN=your_tushare_token
STOCK_TAVILY_API_KEY=your_tavily_api_key
```

See `docs/ask-stock.md` for the daily_stock_analysis table mapping and implementation notes.

## Backend

Install dependencies:

```powershell
python -m pip install -e .
```

Initialize MySQL tables and seed demo A-share data. The MySQL database should already exist:

```powershell
python -m backend.app.scripts_init_db
```

If you are using an admin database account and need to create the database too:

```powershell
python -m backend.app.scripts_init_db --create-database
```

Run API:

```powershell
python -m backend.app.main --host 127.0.0.1 --port 8000
```

Health check:

```text
http://127.0.0.1:8000/api/health
```

## Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies `/api/*` to FastAPI on port `8000`.

## Current Scope

- Python FastAPI backend
- SQLAlchemy + MySQL database
- Vue 3 frontend
- Rabbit logo reused from `hermes-rabbit`
- ROG-inspired black and gold UI theme
- A-share instruments, watchlist, positions, events, reports, risk checks, and logs
- No broker connection and no automatic order placement

## Safety Boundary

This system is only for research, monitoring, alerting, and review. It does not place orders and does not provide investment advice.
