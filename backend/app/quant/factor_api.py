from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.quant.factor_service import (
    MAX_BENCH_FACTORS,
    MAX_FACTOR_SYMBOLS,
    benchmark_factors,
    factor_health,
    get_factor,
    interpret_benchmark,
    list_factors,
    score_factor,
)

router = APIRouter(prefix="/api/factors", tags=["factors"])


class FactorScoreRequest(BaseModel):
    alpha_id: str = Field(min_length=3, max_length=80)
    symbols: list[str] = Field(min_length=5, max_length=MAX_FACTOR_SYMBOLS)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    refresh: bool = False


class FactorBenchmarkRequest(BaseModel):
    alpha_ids: list[str] = Field(min_length=1, max_length=MAX_BENCH_FACTORS)
    symbols: list[str] = Field(min_length=5, max_length=MAX_FACTOR_SYMBOLS)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    refresh: bool = False
    dedup_threshold: float | None = Field(default=None, gt=0, lt=1, description="可选：按该截面相关阈值对 top 因子贪心去重（如 0.7）")


class FactorBenchmarkInterpretRequest(BaseModel):
    benchmark: dict[str, Any] = Field(description="/api/factors/benchmark 的返回结果（ic_series 可省略）")


@router.get("/health")
def get_factor_health(
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return factor_health()


@router.get("")
def get_factors(
    zoo: str | None = Query(default=None, max_length=32),
    theme: str | None = Query(default=None, max_length=32),
    universe: str | None = Query(default="equity_cn", max_length=32),
    query: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return list_factors(zoo=zoo, theme=theme, universe=universe, query=query, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{alpha_id}")
def get_factor_detail(
    alpha_id: str,
    include_source: bool = False,
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return get_factor(alpha_id, include_source=include_source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/score")
def post_factor_score(
    payload: FactorScoreRequest,
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return score_factor(
            payload.alpha_id,
            payload.symbols,
            start_date=payload.start_date,
            end_date=payload.end_date,
            refresh=payload.refresh,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/benchmark")
def post_factor_benchmark(
    payload: FactorBenchmarkRequest,
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return benchmark_factors(
            payload.alpha_ids,
            payload.symbols,
            start_date=payload.start_date,
            end_date=payload.end_date,
            refresh=payload.refresh,
            dedup_threshold=payload.dedup_threshold,
            include_ic_series=True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/benchmark/interpret")
def post_factor_benchmark_interpret(
    payload: FactorBenchmarkInterpretRequest,
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """把已完成的因子评估结果交给当前活跃 LLM，生成零基础读者可读的大白话解读。"""
    try:
        return interpret_benchmark(payload.benchmark)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

