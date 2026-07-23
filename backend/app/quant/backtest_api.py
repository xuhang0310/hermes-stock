from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.quant.backtest_engine import create_backtest, get_backtest, list_backtests


router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class MacdBacktestRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=24)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    fast_period: int = Field(default=12, ge=2, le=200)
    slow_period: int = Field(default=26, ge=3, le=200)
    signal_period: int = Field(default=9, ge=2, le=100)
    initial_cash: float = Field(default=1_000_000, ge=10_000, le=1_000_000_000)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    stamp_duty_rate: float = Field(default=0.0005, ge=0, le=0.01)
    transfer_fee_rate: float = Field(default=0.00001, ge=0, le=0.01)
    slippage_rate: float = Field(default=0.0005, ge=0, le=0.05)
    lot_size: int = Field(default=100, ge=100, le=10000)
    benchmark: str = Field(default="000300.SH", max_length=24)


@router.get("")
def get_backtest_runs(
    limit: int = Query(default=30, ge=1, le=100),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return {"items": list_backtests(context.user.id, limit=limit)}


@router.post("")
def post_backtest(
    payload: MacdBacktestRequest,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return create_backtest(payload.model_dump(exclude_none=True), owner_user_id=context.user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{run_id}")
def get_backtest_detail(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        return get_backtest(run_id, owner_user_id=context.user.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{run_id}/trades.csv")
def download_backtest_trades(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> Response:
    try:
        result = get_backtest(run_id, owner_user_id=context.user.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    columns = [
        "symbol",
        "direction",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "quantity",
        "holding_days",
        "gross_pnl",
        "fees",
        "pnl",
        "return_pct",
        "exit_reason",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result.get("trades") or [])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-trades.csv"'},
    )


@router.get("/{run_id}/metrics.json")
def download_backtest_metrics(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> Response:
    try:
        result = get_backtest(run_id, owner_user_id=context.user.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=json.dumps(result.get("metrics") or {}, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-metrics.json"'},
    )
