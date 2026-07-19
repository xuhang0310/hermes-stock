from __future__ import annotations

import argparse
import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import time

from backend.app.ai.ai_api import resume_background_tasks, resume_pending_uploads, router as ai_router
from backend.app.core.applog import configure_logging, get_logger, log_event, prune_system_logs
from backend.app.auth.auth import AuthContext, require_permission
from backend.app.auth.auth_api import router as auth_router
from backend.app.core.database import SessionLocal, get_db, initialize_default_database, session_scope
from backend.app.agents.position_agent import run_position_agent, run_position_agent_events
from backend.app.agents.position_import import parse_position_screenshot
from backend.app.ai.prompt_recommendations import ensure_ask_stock_profile, start_prompt_recommendation_scheduler, stop_prompt_recommendation_scheduler
from backend.app.feishu.feishu_bot import start_feishu_bots, stop_feishu_bots
from backend.app.feishu.feishu_bot_api import router as feishu_bot_router
from backend.app.data.repository import StockRepository
from backend.app.scheduler.rhythm_api import router as rhythm_router
from backend.app.scheduler.rhythm_scheduler import ensure_default_jobs, start_rhythm_scheduler, stop_rhythm_scheduler
from backend.app.skills.registry import skill_registry
from backend.app.seed.seed import seed_demo, seed_notify_channels, seed_position_accounts
from backend.app.core.settings import settings


class WatchlistCreate(BaseModel):
    symbol: str = Field(min_length=2, max_length=16)
    group_name: str = "default"
    reason: str | None = None


class PositionAccountIn(BaseModel):
    broker_code: str = Field(default="manual", max_length=40)
    broker_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str = Field(default="cash", max_length=40)
    currency: str = Field(default="CNY", max_length=12)
    is_default: bool = False
    remark: str | None = None


class PositionAccountPatchIn(BaseModel):
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str | None = Field(default=None, max_length=40)
    status: str | None = Field(default=None, max_length=24)
    remark: str | None = None
    sort_order: int | None = None
    is_default: bool | None = None


class AccountFundsIn(BaseModel):
    # 用户维护"可用现金"（券商 App 现金数），系统算 total_asset = 可用现金 + 持仓市值
    available_cash: float | None = Field(default=None, ge=0)


class PositionHoldingIn(BaseModel):
    symbol: str = Field(min_length=2, max_length=24)
    name: str | None = Field(default=None, max_length=120)
    account_id: int | None = None
    broker_code: str | None = Field(default=None, max_length=40)
    broker_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str | None = Field(default="cash", max_length=40)
    as_of_date: str | None = Field(default=None, max_length=16)
    position_quantity: float = Field(default=0, ge=0)
    available_quantity: float | None = Field(default=None, ge=0)
    frozen_quantity: float | None = Field(default=None, ge=0)
    today_buy_quantity: float | None = Field(default=None, ge=0)
    today_sell_quantity: float | None = Field(default=None, ge=0)
    cost_price: float | None = Field(default=None, ge=0)
    execution_price: float | None = Field(default=None, ge=0)
    cost_amount: float | None = Field(default=None, ge=0)
    latest_price: float | None = Field(default=None, ge=0)
    market_value: float | None = Field(default=None, ge=0)
    floating_pnl: float | None = None
    floating_pnl_pct: float | None = None
    day_pnl: float | None = None
    position_pct: float | None = Field(default=None, ge=0)
    stop_price: float | None = Field(default=None, ge=0)
    take_profit_price: float | None = Field(default=None, ge=0)
    thesis: str | None = None
    risk_note: str | None = None
    status: str | None = Field(default=None, max_length=24)
    action: str | None = Field(default=None, max_length=24, description="交易动作: buy/sell/add/reduce/close/replace/update_field")


class PositionAiImportIn(BaseModel):
    image_base64: str = Field(min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    model_config_id: int | None = None
    account_id: int | None = None


class PositionAgentAddIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    image_base64: str | None = Field(default=None, min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    account_id: int | None = None
    model_config_id: int | None = None


def _sse_event(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def create_app(init_db: bool = True) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 技能系统：启动时自动发现并注册所有技能（Phase 1：仅注册，不接管分发链）
        skill_registry.discover()
        if init_db:
            # 建表前不入库（system_logs 尚不存在），只落进程日志
            log_event("system", "INFO", "应用启动：初始化数据库与后台组件", to_db=False)
            initialize_default_database()
            # 保证内置飞书通道与节律任务存在（正常启动不跑 seed_demo，这里补齐基础数据）
            with session_scope() as db:
                seed_notify_channels(db)
                seed_position_accounts(db)
                ensure_default_jobs(db)
                ensure_ask_stock_profile(db)
            await resume_pending_uploads()
            await resume_background_tasks()
            start_prompt_recommendation_scheduler()
            start_rhythm_scheduler()
            start_feishu_bots()
            log_event("system", "INFO", "后台组件已启动：调度器 + 飞书机器人 + 推荐刷新")
            # 启动时清理一次过期日志（每日健康检查还会再清理），控制表体积
            await asyncio.to_thread(prune_system_logs)
        try:
            yield
        finally:
            if init_db:
                log_event("system", "INFO", "应用关闭：停止后台组件", to_db=False)
                stop_feishu_bots()
                await stop_rhythm_scheduler()
                await stop_prompt_recommendation_scheduler()

    app = FastAPI(
        title="SixRabbits Stock API",
        description="A-share research and risk-control API.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _req_logger = get_logger("api")

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            elapsed = int((time.monotonic() - started) * 1000)
            # 入库是同步阻塞操作，放到线程池，避免卡住事件循环（异常常伴随 DB 变慢）
            await asyncio.to_thread(
                log_event, "api", "ERROR", "请求未处理异常",
                method=request.method, path=request.url.path, ms=elapsed,
            )
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        line = f"{request.method} {request.url.path} -> {response.status_code} ({elapsed}ms)"
        if response.status_code >= 500:
            # 5xx 入库便于前端排查；4xx/2xx 只落进程日志文件，避免撑爆数据库。
            # 入库同步阻塞，放线程池执行，防止 5xx 高峰期堵塞事件循环。
            await asyncio.to_thread(
                log_event, "api", "ERROR", line,
                method=request.method, path=request.url.path,
                status=response.status_code, ms=elapsed,
            )
        elif response.status_code >= 400:
            _req_logger.warning(line)
        else:
            _req_logger.info(line)
        return response

    app.include_router(auth_router)
    app.include_router(ai_router)
    app.include_router(rhythm_router)
    app.include_router(feishu_bot_router)

    def repo(db: Session = Depends(get_db)) -> StockRepository:
        return StockRepository(db)

    @app.exception_handler(SQLAlchemyError)
    async def db_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        origin = getattr(exc, "orig", None)
        return JSONResponse(
            status_code=500,
            content={"detail": f"database error: {str(origin or exc)[:300]}"},
        )

    @app.get("/api/health")
    def api_health(repository: StockRepository = Depends(repo)) -> dict:
        return repository.health()

    @app.get("/api/tushare/catalog")
    def tushare_catalog_list(
        category: str | None = Query(default=None),
        scope: str | None = Query(default=None),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> dict:
        """TuShare 接口元数据目录（聚焦 A 股核心约 150 接口）。供前端管理页与 LLM 目录查询共用。"""
        from backend.app.market.tushare_catalog import (
            TUSHARE_CATALOG,
            by_category_scope as catalog_by_category_scope,
            category_index as catalog_category_index,
        )
        if not category:
            return {
                "categories": catalog_category_index(),
                "total": len(TUSHARE_CATALOG),
            }
        items = catalog_by_category_scope(category, scope)
        return {"category": category, "scope": scope, "count": len(items), "items": items}

    @app.post("/api/dev/seed-demo")
    def api_seed_demo(
        db: Session = Depends(get_db),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        seed_demo(db)
        return {"ok": True, "message": "demo data inserted"}

    @app.get("/api/dashboard")
    def dashboard(
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> dict:
        return repository.dashboard()

    @app.get("/api/events")
    def events(
        repository: StockRepository = Depends(repo),
        limit: int = Query(default=50, ge=1, le=200),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.events(limit)

    @app.get("/api/events/{event_id}")
    def event_detail(
        event_id: str,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> dict:
        event = repository.event_detail(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        return event

    @app.get("/api/positions")
    def positions(
        account_id: int | None = Query(default=None),
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.positions(account_id)

    @app.get("/api/positions/brokers")
    def position_brokers(
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        from backend.app.data.broker_presets import BROKER_PRESETS

        return BROKER_PRESETS

    @app.get("/api/positions/accounts")
    def position_accounts(
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.list_position_accounts()

    @app.post("/api/positions/accounts")
    def create_position_account(
        payload: PositionAccountIn,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        return repository.create_position_account(payload.model_dump())

    @app.patch("/api/positions/accounts/{account_id}")
    def update_position_account(
        account_id: int,
        payload: PositionAccountPatchIn,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            return repository.update_position_account(account_id, payload.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/positions/accounts/{account_id}/funds")
    def update_account_funds(
        account_id: int,
        payload: AccountFundsIn,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            return repository.update_account_funds(account_id, payload.available_cash)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/positions/summary")
    def positions_summary(
        account_id: int | None = Query(default=None),
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> dict:
        return repository.positions_summary(account_id)

    @app.get("/api/positions/trades")
    def position_trades(
        symbol: str | None = Query(default=None),
        account_id: int | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        """买卖交易流水查询，按时间倒序。"""
        return repository.position_trades(symbol=symbol, account_id=account_id, limit=limit)

    @app.delete("/api/positions/trades/{trade_id}")
    def delete_position_trade(
        trade_id: int,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        """删单条交易流水（不回滚持仓，仅删流水行）。用于删错单。"""
        result = repository.delete_position_trade(trade_id)
        if not result.get("deleted"):
            raise HTTPException(status_code=404, detail="交易流水不存在")
        return result

    @app.get("/api/chart/positions/{symbol}")
    def position_chart(
        symbol: str,
        limit: int = Query(default=240, ge=30, le=1000),
        refresh: bool = Query(default=False),
        resolution: str = Query(default="1D", max_length=8),
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> dict:
        try:
            return repository.position_chart(symbol, limit=limit, refresh=refresh, resolution=resolution)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/positions")
    def add_position(
        payload: PositionHoldingIn,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            data = payload.model_dump(exclude_none=True)
            action = data.pop("action", None)
            return repository.merge_position_holding(data, action=action, source_type="manual", commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/positions/{symbol}")
    def delete_position(
        symbol: str,
        account_id: int | None = Query(default=None),
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        """物理删除一条持仓记录及其历史快照（不可逆）。用于录错重来的场景。"""
        try:
            result = repository.delete_position(symbol, account_id=account_id)
            if "error" in result:
                raise HTTPException(status_code=404, detail=result["error"])
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/positions/ai-import")
    def import_positions_from_image(
        payload: PositionAiImportIn,
        repository: StockRepository = Depends(repo),
        context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            parsed = parse_position_screenshot(
                image_base64=payload.image_base64,
                mime_type=payload.mime_type,
                model_config_id=payload.model_config_id,
            )
            batch = repository.create_position_import_batch(
                source_name=parsed["model"].get("display_name") or parsed["model"].get("model_name") or "vision_model",
                image_mime_type=payload.mime_type,
                image_sha256=parsed.get("image_sha256"),
                raw_text=parsed.get("raw_text"),
                parsed_json=json.dumps(parsed.get("parsed"), ensure_ascii=False),
                created_by=context.user.username,
            )
            account_ctx = dict(parsed.get("account") or {})
            if payload.account_id:
                account_ctx["account_id"] = payload.account_id
            saved_rows = repository.import_position_rows(parsed.get("rows") or [], batch, account_ctx)
            repository.finish_position_import_batch(batch, saved_rows)
            return {
                "batch": {
                    "id": batch.id,
                    "status": batch.status,
                    "image_sha256": batch.image_sha256,
                    "created_at": batch.created_at.isoformat() if batch.created_at else None,
                },
                "model": parsed.get("model"),
                "rows": saved_rows,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/positions/agent-add")
    def agent_add_positions(
        payload: PositionAgentAddIn,
        repository: StockRepository = Depends(repo),
        context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            result = run_position_agent(
                repository=repository,
                instruction=payload.instruction,
                image_base64=payload.image_base64,
                mime_type=payload.mime_type,
                model_config_id=payload.model_config_id,
                created_by=context.user.username,
                account_id=payload.account_id,
            )
            return {
                "batch": None,
                "model": result.get("model"),
                "intent": result.get("intent"),
                "should_save": result.get("should_save"),
                "reply": result.get("reply"),
                "rows": result.get("rows") or [],
                "steps": result.get("steps") or [],
                "summary": result.get("summary") or result.get("reply"),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/positions/agent-add/stream")
    async def stream_agent_add_positions(
        payload: PositionAgentAddIn,
        request: Request,
        context: AuthContext = Depends(require_permission("data:write")),
    ) -> StreamingResponse:
        async def event_stream() -> AsyncIterator[str]:
            yield ": connected\n\n"
            queue: asyncio.Queue[dict | object] = asyncio.Queue()
            loop = asyncio.get_running_loop()
            _DONE = object()
            cancel_event = threading.Event()

            def produce_events() -> None:
                try:
                    # The streaming worker owns its Session for its full lifetime.
                    with SessionLocal() as worker_db:
                        worker_repo = StockRepository(worker_db)
                        for event in run_position_agent_events(
                            repository=worker_repo,
                            instruction=payload.instruction,
                            image_base64=payload.image_base64,
                            mime_type=payload.mime_type,
                            model_config_id=payload.model_config_id,
                            created_by=context.user.username,
                            account_id=payload.account_id,
                            cancel_event=cancel_event,
                        ):
                            if cancel_event.is_set():
                                break
                            asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
                except Exception as exc:  # noqa: BLE001
                    asyncio.run_coroutine_threadsafe(
                        queue.put({"type": "error", "message": str(exc)}), loop
                    ).result()
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()

            producer = asyncio.create_task(asyncio.to_thread(produce_events))
            disconnected = False
            while True:
                if await request.is_disconnected():
                    disconnected = True
                    cancel_event.set()
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=8)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is _DONE:
                    break
                yield _sse_event(event)
                await asyncio.sleep(0)
            if not disconnected:
                await producer

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/watchlist")
    def watchlist(
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.watchlist()

    @app.post("/api/watchlist")
    def add_watchlist_item(
        payload: WatchlistCreate,
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("data:write")),
    ) -> dict:
        try:
            return repository.add_watchlist_item(payload.symbol, payload.group_name, payload.reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/agents")
    def agents(
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("agent:use")),
    ) -> list[dict]:
        return repository.agents()

    @app.get("/api/reports")
    def reports(
        repository: StockRepository = Depends(repo),
        limit: int = Query(default=50, ge=1, le=200),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.reports(limit)

    @app.get("/api/risk-checks")
    def risk_checks(
        repository: StockRepository = Depends(repo),
        limit: int = Query(default=50, ge=1, le=200),
        _context: AuthContext = Depends(require_permission("dashboard:view")),
    ) -> list[dict]:
        return repository.risk_checks(limit)

    @app.get("/api/logs")
    def logs(
        repository: StockRepository = Depends(repo),
        limit: int = Query(default=100, ge=1, le=500),
        module: str | None = Query(default=None),
        level: str | None = Query(default=None),
        _context: AuthContext = Depends(require_permission("system:view")),
    ) -> list[dict]:
        return repository.system_logs(limit, module=module, level=level)

    @app.get("/api/logs/modules")
    def log_modules(
        repository: StockRepository = Depends(repo),
        _context: AuthContext = Depends(require_permission("system:view")),
    ) -> list[str]:
        return repository.system_log_modules()

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-stock-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("backend.app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
