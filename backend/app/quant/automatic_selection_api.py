from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, has_permission, require_permission
from backend.app.core.database import get_db
from backend.app.core.response import fail, ok
from backend.app.quant import automatic_selection_presentation as presentation
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant.automatic_selection_scoring import RESEARCH_LIMITATIONS
from backend.app.scheduler.rhythm_api import _owned_job
from backend.app.scheduler.rhythm_scheduler import (
    AUTOMATIC_SELECTION_DATA_JOB_KEY,
    AUTOMATIC_SELECTION_JOB_KEY,
    dispatch_automatic_selection_pipeline_result,
)

router = APIRouter(prefix="/api/automatic-selection", tags=["automatic-selection"])


class StrategyCreateIn(BaseModel):
    strategy_key: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    config: dict[str, Any]
    lifecycle_state: Literal["challenger", "shadow"] = "challenger"
    parent_strategy_id: int | None = Field(default=None, ge=1)


class StrategyPromoteIn(BaseModel):
    confirmation_note: str = Field(max_length=1000)


class StrategyRetireIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@router.get("/overview")
def automatic_selection_overview(
    _context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    strategies = repository.list_strategies(include_retired=True)
    runs = repository.list_runs(limit=10)
    latest_run = runs[0] if runs else None
    latest_detail = (
        repository.run_detail(latest_run["run_id"], candidate_scope="top")
        if latest_run is not None
        else None
    )
    state_counts = {state: 0 for state in ("official", "challenger", "shadow", "retired")}
    for strategy in strategies:
        state = str(strategy.get("lifecycle_state") or "")
        if state in state_counts:
            state_counts[state] += 1
    return ok(
        {
            "latest_run": latest_run,
            "top_candidates": (latest_detail or {}).get("candidates", []),
            "evaluations": (latest_detail or {}).get("evaluations", []),
            "recent_runs": runs,
            "strategy_counts": state_counts,
            "official_strategy": next(
                (strategy for strategy in strategies if strategy.get("is_official")),
                None,
            ),
            "limitations": RESEARCH_LIMITATIONS,
        }
    )


@router.get("/daily")
def automatic_selection_daily(
    trade_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
    strategy_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    """面向普通用户的某日选股结果：入选理由、风险、选出后表现与白话状态。"""
    try:
        view = presentation.daily_view(trade_date=trade_date, strategy_id=strategy_id)
    except LookupError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    can_dispatch = has_permission(context, "schedule:manage")
    if can_dispatch:
        try:
            _require_pipeline_owner(db, context.user.id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            can_dispatch = False
    return ok({**view, "can_dispatch": can_dispatch})


@router.get("/track-record")
def automatic_selection_track_record(
    strategy_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=60, ge=1, le=250),
    _context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        return ok(presentation.track_record(strategy_id=strategy_id, limit=limit))
    except LookupError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))


@router.get("/strategies")
def automatic_selection_strategies(
    include_retired: bool = Query(default=True),
    _context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok(repository.list_strategies(include_retired=include_retired))


@router.post("/strategies")
def automatic_selection_create_strategy(
    payload: StrategyCreateIn,
    context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    try:
        strategy = repository.create_strategy(
            strategy_key=payload.strategy_key,
            name=payload.name,
            config=payload.config,
            lifecycle_state=payload.lifecycle_state,
            actor_user_id=context.user.id,
            parent_strategy_id=payload.parent_strategy_id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(strategy, "已创建不可变策略版本")


@router.post("/strategies/{strategy_id}/promote")
def automatic_selection_promote_strategy(
    strategy_id: int,
    payload: StrategyPromoteIn,
    context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    try:
        strategy = repository.promote_strategy(
            strategy_id,
            actor_user_id=context.user.id,
            confirmation_note=payload.confirmation_note,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(strategy, "策略已人工晋升为正式策略")


@router.post("/strategies/{strategy_id}/retire")
def automatic_selection_retire_strategy(
    strategy_id: int,
    payload: StrategyRetireIn,
    context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    try:
        strategy = repository.retire_strategy(
            strategy_id,
            actor_user_id=context.user.id,
            note=payload.note,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(strategy, "策略已退役")


def _require_pipeline_owner(db: Session, user_id: int) -> None:
    for job_key in (
        AUTOMATIC_SELECTION_DATA_JOB_KEY,
        AUTOMATIC_SELECTION_JOB_KEY,
    ):
        _owned_job(
            db,
            user_id=user_id,
            job_key=job_key,
        )


@router.post("/runs/dispatch")
async def automatic_selection_dispatch(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    _require_pipeline_owner(db, context.user.id)
    claim = await dispatch_automatic_selection_pipeline_result(
        actor_user_id=context.user.id,
    )
    status = str(claim.get("status") or "")
    if status == "missing":
        return JSONResponse(
            status_code=404,
            content=fail(
                "自动选股内置任务不存在",
                code="not_found",
            ),
        )
    if status == "invalid_kind":
        return JSONResponse(
            status_code=400,
            content=fail(
                "自动选股内置任务类型异常，请联系管理员",
                code="job_invalid",
            ),
        )
    if status == "already_running":
        return JSONResponse(
            status_code=400,
            content=fail(
                "自动选股任务正在运行，请稍后重试",
                code="run_conflict",
            ),
        )
    job_run_id = str(claim.get("run_id") or "")
    if not job_run_id:
        return JSONResponse(
            status_code=500,
            content=fail(
                "自动选股任务投递失败",
                code="dispatch_failed",
            ),
        )
    return ok(
        {
            "job_run_id": job_run_id,
            "job_key": str(claim.get("job_key") or AUTOMATIC_SELECTION_DATA_JOB_KEY),
            "pipeline_stage": str(claim.get("pipeline_stage") or "data_ingestion"),
        },
        "自动选股数据入库与排名流水线已投递",
    )


@router.get("/runs")
def automatic_selection_runs(
    strategy_id: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    _context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok(repository.list_runs(strategy_id=strategy_id, status=status, limit=limit))


@router.get("/runs/{run_id}")
def automatic_selection_run_detail(
    run_id: str,
    candidate_scope: Literal[
        "top",
        "eligible",
        "excluded",
        "all",
    ] = Query(default="top"),
    candidate_limit: int = Query(default=50, ge=1, le=200),
    candidate_offset: int = Query(default=0, ge=0),
    _context: AuthContext = Depends(
        require_permission("data:read")
    ),
) -> dict:
    detail = repository.run_detail(
        run_id,
        candidate_scope=candidate_scope,
        candidate_limit=candidate_limit,
        candidate_offset=candidate_offset,
    )
    if detail is None:
        return JSONResponse(status_code=404, content=fail("自动选股运行不存在", code="not_found"))
    return ok(detail)


@router.get("/runs/{run_id}/evaluations")
def automatic_selection_run_evaluations(
    run_id: str,
    _context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    evaluations = repository.run_evaluations(run_id)
    if evaluations is None:
        return JSONResponse(
            status_code=404,
            content=fail(
                "自动选股运行不存在",
                code="not_found",
            ),
        )
    return ok(evaluations)
