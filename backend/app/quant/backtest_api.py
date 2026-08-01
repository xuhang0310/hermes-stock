from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.quant.backtest_engine import create_backtest, get_backtest, list_backtests
from backend.app.quant.backtest_runner import SIGNAL_ENGINE_REGISTRY, run_signal_backtest
from backend.app.quant.strategy_descriptors import descriptor_public, get_descriptor


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


class StrategyRunRequest(BaseModel):
    strategy_code: str = Field(min_length=1, max_length=32)
    symbols: list[str] = Field(default_factory=list)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    strategy_params: dict | None = None
    initial_cash: float = Field(default=1_000_000, ge=10_000, le=1_000_000_000)
    commission_rate: float = Field(default=0.00025, ge=0, le=0.01)
    slippage: float = Field(default=0.001, ge=0, le=0.01)


def _default_period() -> tuple[str, str]:
    """卡片未指定区间时默认回看最近两年（YYYYMMDD）。"""
    end = date.today()
    start = end - timedelta(days=730)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


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


@router.get("/strategies")
def list_strategy_catalog(
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """策略库卡片数据源：派生自策略登记（strategy_descriptors.py）。"""
    return {"items": descriptor_public()}


@router.post("/run")
def run_strategy_from_catalog(
    payload: StrategyRunRequest,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """策略库卡片「用默认回测」的确定性入口：复用 run_signal_backtest。"""
    descriptor = get_descriptor(payload.strategy_code)
    if descriptor is None or payload.strategy_code not in SIGNAL_ENGINE_REGISTRY:
        raise HTTPException(status_code=400, detail=f"未知或暂不支持的策略: {payload.strategy_code}")

    symbols = [symbol.strip() for symbol in (payload.symbols or descriptor["default_symbols"]) if symbol.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="股票池不能为空")
    if descriptor["kind"] == "cross_sectional" and len(symbols) < 3:
        raise HTTPException(status_code=400, detail="横截面策略至少需要 3 只股票进行横向比较")

    start_date = payload.start_date
    end_date = payload.end_date
    if not start_date or not end_date:
        start_date, end_date = _default_period()

    try:
        result = run_signal_backtest(
            strategy=payload.strategy_code,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            strategy_params=payload.strategy_params or descriptor["default_params"],
            initial_cash=payload.initial_cash,
            commission_rate=payload.commission_rate,
            slippage=payload.slippage,
            owner_user_id=context.user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "run_id": result["run_id"],
        "report_url": result["report_url"],
        "summary_markdown": result.get("summary_markdown"),
        "metrics": result.get("metrics"),
    }


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
