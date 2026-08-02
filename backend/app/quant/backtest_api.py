from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
import logging

from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.app.ai.llm_client import chat_completion
from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config
from backend.app.core.models import BacktestRun
from backend.app.quant.backtest_engine import create_backtest, get_backtest, list_backtests
from backend.app.quant.backtest_runner import SIGNAL_ENGINE_REGISTRY, run_signal_backtest
from backend.app.market.stock_tools import _stock_basic_item, resolve_stock
from backend.app.quant.strategy_descriptors import descriptor_public, get_descriptor


router = APIRouter(prefix="/api/backtests", tags=["backtests"])
stocks_router = APIRouter(prefix="/api/stocks", tags=["stocks"])

logger = logging.getLogger(__name__)


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
    """策略库卡片数据源：派生自策略登记（strategy_descriptors.py）。

    每项附 ``symbol_labels``（默认标的 code→名称），供卡片回显——研究员看
    名称而非代码。名称取自 TuShare 主数据，取不到时该 code 缺省、前端回退显示代码。
    """
    items = descriptor_public()
    for item in items:
        labels: dict[str, str] = {}
        for code in item.get("default_symbols", []):
            name = (_stock_basic_item(code) or {}).get("name")
            if name:
                labels[code] = str(name)
        item["symbol_labels"] = labels
    return {"items": items}


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
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"回测执行失败: {exc}") from exc

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


@stocks_router.get("/search")
def search_stocks(
    q: str = Query(default="", max_length=32),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """按代码或名称模糊检索 A 股标的（确定性主数据，不依赖 LLM）。

    供策略库卡片输入框联想：研究员输名称或代码片段，返回候选
    ``[{ts_code, symbol, name}]``。空查询返回空列表，绝不回吐全市场。
    """
    query = q.strip()
    if not query:
        return {"items": []}
    candidates = resolve_stock(query).get("candidates") or []
    return {
        "items": [
            {"ts_code": c.get("ts_code"), "symbol": c.get("symbol"), "name": c.get("name")}
            for c in candidates
        ]
    }


def _pct(value: object) -> str:
    """百分比格式化，None/非数 → N/A。"""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def _num(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def _build_takeaway_messages(result: dict) -> list[dict[str, str]]:
    """把回测结果压成一段事实，交给 LLM 做人话点评。"""
    metrics = result.get("metrics") or {}
    strategy = result.get("strategy")
    strategy_name = strategy.get("name") if isinstance(strategy, dict) else (strategy or "未命名策略")
    symbols = result.get("symbols") or []
    period = result.get("period") or {}
    trades = result.get("trades") or []

    trade_lines = []
    for trade in trades[:6]:
        trade_lines.append(
            f"  - {trade.get('symbol')} 盈亏{_num(trade.get('pnl'))}元 "
            f"({_pct(trade.get('pnl_pct'))}) 持{trade.get('holding_days') or trade.get('holding_bars') or '?'}天"
        )

    facts = (
        f"策略：{strategy_name}\n"
        f"标的：{', '.join(symbols) if symbols else '组合'}\n"
        f"回测区间：{period.get('start', '?')} ~ {period.get('end', '?')}\n"
        f"总收益：{_pct(metrics.get('total_return'))}　最大回撤：{_pct(metrics.get('max_drawdown'))}　"
        f"夏普：{_num(metrics.get('sharpe'))}　胜率：{_pct(metrics.get('win_rate'))}　"
        f"交易笔数：{metrics.get('trade_count') or metrics.get('n_trades') or len(trades)}　"
        f"盈/亏笔：{metrics.get('winning_trades', '?')}/{metrics.get('losing_trades', '?')}　"
        f"平均持仓：{metrics.get('avg_holding_days', '?')}天\n"
        f"逐笔概览：\n{chr(10).join(trade_lines) if trade_lines else '  （无成交）'}"
    )

    system = (
        "你是 A 股量化回测的解读顾问，服务对象是股票研究员。给你一次策略回测的绩效与逐笔统计，"
        "请用 2 到 4 句通俗中文点评「这次回测说明了什么」。\n"
        "要求：\n"
        "1. 结合策略特性给判断，而非复述数字。例如趋势/海龟类策略在震荡市小亏、频繁止损，往往是纪律性风控的正常表现，"
        "不等于策略失效；动量/横截面策略要留意持仓是否集中在同一板块或风格。\n"
        "2. 从收益、回撤、夏普、胜率、交易笔数的组合里读出信号（如交易太少则结论不可靠、胜率低但靠少数大赚支撑等）。\n"
        "3. 点明局限：样本区间长短、是否覆盖牛熊、交易次数是否足够支撑统计结论。\n"
        "4. 这是研究复盘，绝不给出买入/卖出建议，也不预测未来涨跌。\n"
        "只输出点评正文，不要标题、不要列表、不要 JSON、不要复述原始数字清单。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": facts},
    ]


def _generate_takeaway(result: dict) -> str | None:
    """调活跃模型生成人话点评；任何失败返回 None（best-effort，不影响回测）。"""
    try:
        llm_config = active_llm_config()
        if not llm_config.get("api_key") or not llm_config.get("base_url"):
            return None
        data = chat_completion(
            _build_takeaway_messages(result),
            llm_config,
            temperature=0.3,
            timeout=45.0,
            log_module="backtest_takeaway",
        )
        choices = data.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else ""
        text = (content or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("回测 AI 解读生成失败: %s", str(exc)[:200])
        return None


def _save_takeaway(run_id: str, text: str) -> None:
    """把解读写回 result_json 作缓存；失败静默（下次重算即可）。"""
    try:
        with SessionLocal() as db:
            row = db.scalar(select(BacktestRun).where(BacktestRun.run_id == run_id).limit(1))
            if row is None:
                return
            payload = json.loads(row.result_json) if row.result_json else {}
            payload["ai_takeaway"] = text
            row.result_json = json.dumps(payload, ensure_ascii=False)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回测 AI 解读缓存写入失败: %s", str(exc)[:200])


@router.get("/{run_id}/takeaway")
def get_backtest_takeaway(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """回测的 AI 人话点评：首次懒生成并缓存，之后直接读缓存。

    失败时返回 ``status=unavailable`` 与空文本，前端据此降级，绝不影响回测本身。
    """
    try:
        result = get_backtest(run_id, owner_user_id=context.user.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    cached = (result.get("ai_takeaway") or "").strip()
    if cached:
        return {"text": cached, "status": "ready"}

    text = _generate_takeaway(result)
    if not text:
        return {"text": "", "status": "unavailable"}
    _save_takeaway(run_id, text)
    return {"text": text, "status": "ready"}
