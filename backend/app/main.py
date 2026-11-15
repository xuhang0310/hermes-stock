from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import time

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.app.ai.ai_api import resume_background_tasks, resume_pending_uploads, router as ai_router
from backend.app.ai.prompt_recommendations import ensure_ask_stock_profile, start_prompt_recommendation_scheduler, stop_prompt_recommendation_scheduler
from backend.app.agent_runtime.redis_broker import redis_broker
from backend.app.agent_runtime.worker import agent_runtime_worker
from backend.app.auth.auth_api import router as auth_router
from backend.app.auth.user_api import router as user_router
from backend.app.core.applog import configure_logging, get_logger, log_event, prune_system_logs
from backend.app.core.database import initialize_default_database, session_scope
from backend.app.core.operation_log import OperationLogMiddleware
from backend.app.core.request_id_middleware import RequestIDMiddleware
from backend.app.core.runtime_overrides_api import router as runtime_overrides_router
from backend.app.core.settings import settings
from backend.app.core.startup_validator import validate_startup
from backend.app.data.dashboard_api import router as dashboard_router
from backend.app.data.deps import get_repo
from backend.app.data.logs_api import router as logs_router
from backend.app.data.ops_console_api import router as ops_console_router
from backend.app.data.positions_api import router as positions_router
from backend.app.data.report_api import router as report_router
from backend.app.data.share_api import router as share_router
from backend.app.data.repository import StockRepository
from backend.app.data.watchlist_api import router as watchlist_router
from backend.app.feishu.feishu_bot import start_feishu_bots, stop_feishu_bots
from backend.app.feishu.feishu_bot_api import router as feishu_bot_router
from backend.app.market.tushare_api import router as tushare_router
from backend.app.market.industry_api import router as industry_router
from backend.app.mcp_gateway.server import MCPAuthMiddleware, create_mcp_server
from backend.app.mcp_gateway.token_api import router as mcp_token_router
from backend.app.notify.notification_api import router as notification_router
from backend.app.paper.paper_api import router as paper_router
from backend.app.scorecard.api import router as scorecard_router
from backend.app.quant.agent_board_api import router as agent_board_router
from backend.app.quant.automatic_selection_api import router as automatic_selection_router
from backend.app.quant.automatic_selection_repository import ensure_default_strategy
from backend.app.quant.backtest_api import router as backtest_router, stocks_router
from backend.app.quant.factor_api import router as factor_router
from backend.app.quant.factor_composite_api import router as factor_composite_router
from backend.app.quant.tushare_factor_api import router as tushare_factor_router
from backend.app.quant.screener_api import router as screener_router
from backend.app.scheduler.rhythm_api import router as rhythm_router
from backend.app.scheduler.rhythm_scheduler import ensure_default_jobs, start_rhythm_scheduler, stop_rhythm_scheduler
from backend.app.seed.seed_api import router as seed_router
from backend.app.seed.seed import (
    seed_notify_channels,
    seed_position_accounts,
    seed_position_agent_profile,
    seed_watchlist_agent_profile,
    upgrade_role_prompts,
)
from backend.app.skills.registry import skill_registry


def create_app(init_db: bool = True) -> FastAPI:
    configure_logging()
    mcp_server = create_mcp_server()
    mcp_app = MCPAuthMiddleware(mcp_server.streamable_http_app())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 启动校验：必需依赖（DB）失败直接 fail-fast，避免带病运行
        await asyncio.to_thread(validate_startup)
        # 技能系统：启动时自动发现并注册所有技能（Phase 1：仅注册，不接管分发链）
        skill_registry.discover()
        if init_db and settings.background_services_enabled:
            # 建表前不入库（system_logs 尚不存在），只落进程日志
            log_event("system", "INFO", "应用启动：初始化数据库与后台组件", to_db=False)
            initialize_default_database()
            ensure_default_strategy()
            # 保证内置飞书通道与节律任务存在（正常启动不跑 seed_demo，这里补齐基础数据）
            with session_scope() as db:
                seed_notify_channels(db)
                seed_position_accounts(db)
                seed_position_agent_profile(db)
                seed_watchlist_agent_profile(db)
                upgrade_role_prompts(db)
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
        elif init_db and not settings.agent_runtime_worker_enabled:
            log_event("system", "INFO", "API replica started; background services run separately", to_db=False)
        if init_db and settings.agent_runtime_worker_enabled:
            agent_runtime_worker.start()
            log_event("system", "INFO", "Agent Runtime worker started", to_db=False)
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            if init_db and settings.background_services_enabled:
                log_event("system", "INFO", "应用关闭：停止后台组件", to_db=False)
                await stop_rhythm_scheduler()
                await stop_prompt_recommendation_scheduler()
                stop_feishu_bots()
            if init_db and settings.agent_runtime_worker_enabled:
                await agent_runtime_worker.stop()
            await redis_broker.close()

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
    # add_middleware 后加的在外层：RequestID 最外（先置 trace 上下文），
    # 操作日志次之（写库时可读到 trace_id），再到请求日志与 CORS。
    app.add_middleware(OperationLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
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
    app.include_router(user_router)
    app.include_router(ai_router)
    app.include_router(tushare_factor_router)
    app.include_router(factor_router)
    app.include_router(factor_composite_router)
    app.include_router(backtest_router)
    app.include_router(stocks_router)
    app.include_router(agent_board_router)
    app.include_router(screener_router)
    app.include_router(industry_router)
    app.include_router(automatic_selection_router)
    app.include_router(rhythm_router)
    app.include_router(feishu_bot_router)
    app.include_router(positions_router)
    app.include_router(watchlist_router)
    app.include_router(dashboard_router)
    app.include_router(report_router)
    app.include_router(share_router)
    app.include_router(logs_router)
    app.include_router(ops_console_router)
    app.include_router(tushare_router)
    app.include_router(seed_router)
    app.include_router(notification_router)
    app.include_router(paper_router)
    app.include_router(scorecard_router)
    app.include_router(runtime_overrides_router)
    app.include_router(mcp_token_router)
    app.mount("/api/mcp", mcp_app)

    @app.exception_handler(IntegrityError)
    async def db_integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        error_id = uuid.uuid4().hex[:12]
        get_logger("db").error(
            "数据库约束冲突 [%s] %s %s",
            error_id,
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=409,
            content={"detail": "数据冲突或关联记录不存在，请刷新后重试", "error_id": error_id},
        )

    @app.exception_handler(SQLAlchemyError)
    async def db_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        error_id = uuid.uuid4().hex[:12]
        get_logger("db").error(
            "数据库操作失败 [%s] %s %s",
            error_id,
            request.method,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "数据库操作失败，请稍后重试", "error_id": error_id},
        )

    @app.get("/api/health")
    def api_health(repository: StockRepository = Depends(get_repo)) -> dict:
        return repository.health()

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
