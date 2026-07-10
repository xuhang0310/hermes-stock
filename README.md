# SixRabbits Stock

A-share multi-agent research and risk-control workspace.

> Product/display name is **SixRabbits Stock**. Internal identifiers (repo dir, Python package, MySQL database `hermes-stock`, `STOCK_*`/`RABBIT_*` env keys) keep the `hermes` naming for continuity — do not rename those.

The project now follows the same backend shape as `hermes-rabbit`:

```text
backend/app/settings.py    environment and MySQL settings
backend/app/database.py    SQLAlchemy engine/session/base
backend/app/models.py      database models
backend/app/repository.py  query/write repository
backend/app/seed.py        demo bootstrap data
backend/app/main.py        FastAPI API
frontend/                 Vue 3 workspace
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
