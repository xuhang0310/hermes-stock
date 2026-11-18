"""PIT（Point-in-Time）财务指标同步与读取模块（L3 基本面质量 & L4 业绩加速度底座）。

核心防线（杜绝未来函数）：
- 入库时必须保留 TuShare 真实披露日 `ann_date`，绝不允许用报告期末 `end_date` 冒充披露日；
  例如 2025 年报（end_date=20251231）在 2026-04-20 披露，则 2026-03-01 的截面绝对看不到这条记录。
- 计算营收/净利增速二阶导（加速度 = 本期同比 - 上期同比）时，仅与披露日 <= 当前记录披露日的上一季度比较。
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import StockFinancialPit, TushareFactorValue
from backend.app.market.stock_tools import _tushare_request

logger = logging.getLogger(__name__)

# 付费 TuShare PIT 截面因子库字段（单次按 trade_date 拉全市场 5500+ 只，严格 PIT 口径）
LAYERED_PIT_FACTOR_NAMES: tuple[str, ...] = (
    "roe_ttm",
    "gpm_q",
    "debt_asset_ratio",
    "yoy_ocf",
    "yoy_net_profit",
    "yoy_revenue",
    "sa",
    "delta_roe",
)

_FINA_FIELDS = (
    "ts_code,ann_date,end_date,roe,grossprofit_margin,netprofit_margin,"
    "debt_to_assets,tr_yoy,netprofit_yoy,q_sales_yoy,q_profit_yoy,ocf_yoy"
)
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_PAGE_LIMIT = 4000
_MAX_PAGES = 4
_API_SLEEP_SECONDS = 0.25


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def recent_report_periods(as_of: date | None = None, count: int = 6) -> list[str]:
    """返回最近 count 个标准财报季末日期（YYYY0331 / YYYY0630 / YYYY0930 / YYYY1231），由旧到新。"""
    ref = as_of or date.today()
    quarters = ("0331", "0630", "0930", "1231")
    candidates: list[str] = []
    for year in range(ref.year - 2, ref.year + 1):
        for q in quarters:
            period = f"{year}{q}"
            if period <= ref.strftime("%Y%m%d"):
                candidates.append(period)
    return candidates[-max(2, count) :]


def _compute_pit_accelerations(records: list[dict[str, Any]]) -> None:
    """对同一标的按 (end_date, ann_date) 排序，严格在已披露集合内计算单季/累计增速环比加速度。"""
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_symbol[rec["symbol"]].append(rec)

    for sym_records in by_symbol.values():
        sym_records.sort(key=lambda item: (item["end_date"], item["ann_date"]))
        for idx, current in enumerate(sym_records):
            # 找严格早于当前 end_date 且 ann_date <= 当前 ann_date 的最近一期财报
            prev = None
            for back in range(idx - 1, -1, -1):
                cand = sym_records[back]
                if cand["end_date"] < current["end_date"] and cand["ann_date"] <= current["ann_date"]:
                    prev = cand
                    break
            if prev is None:
                continue
            cur_sales = current.get("q_sales_yoy") if current.get("q_sales_yoy") is not None else current.get("tr_yoy")
            prev_sales = prev.get("q_sales_yoy") if prev.get("q_sales_yoy") is not None else prev.get("tr_yoy")
            if cur_sales is not None and prev_sales is not None:
                current["tr_yoy_accel"] = round(cur_sales - prev_sales, 4)

            cur_profit = (
                current.get("q_profit_yoy") if current.get("q_profit_yoy") is not None else current.get("netprofit_yoy")
            )
            prev_profit = (
                prev.get("q_profit_yoy") if prev.get("q_profit_yoy") is not None else prev.get("netprofit_yoy")
            )
            if cur_profit is not None and prev_profit is not None:
                current["profit_yoy_accel"] = round(cur_profit - prev_profit, 4)


def upsert_financial_pit_rows(
    db: Session,
    rows: Sequence[dict[str, Any]],
    *,
    source: str = "tushare.fina_indicator",
) -> int:
    """幂等写入 PIT 财务指标，丢弃缺少真实披露日 ann_date 的记录。"""
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").strip().upper()
        end_date = _compact_date(row.get("end_date"))
        ann_date = _compact_date(row.get("ann_date"))
        # 铁律：缺少真实披露日或披露日早于报告期末（脏数据）的一律拒收
        if not _SYMBOL_RE.fullmatch(symbol) or not end_date or not ann_date or ann_date < end_date:
            continue
        deduped[(symbol, end_date, ann_date)] = {
            "symbol": symbol,
            "end_date": end_date,
            "ann_date": ann_date,
            "roe": _finite_float(row.get("roe")),
            "grossprofit_margin": _finite_float(row.get("grossprofit_margin")),
            "netprofit_margin": _finite_float(row.get("netprofit_margin")),
            "debt_to_assets": _finite_float(row.get("debt_to_assets")),
            "tr_yoy": _finite_float(row.get("tr_yoy")),
            "netprofit_yoy": _finite_float(row.get("netprofit_yoy")),
            "q_sales_yoy": _finite_float(row.get("q_sales_yoy")),
            "q_profit_yoy": _finite_float(row.get("q_profit_yoy")),
            "tr_yoy_accel": _finite_float(row.get("tr_yoy_accel")),
            "profit_yoy_accel": _finite_float(row.get("profit_yoy_accel")),
            "ocf_yoy": _finite_float(row.get("ocf_yoy")),
            "source": source,
        }
    if not deduped:
        return 0

    symbols = sorted({sym for sym, _, _ in deduped})
    existing_rows: list[StockFinancialPit] = []
    for start in range(0, len(symbols), 500):
        batch_syms = symbols[start : start + 500]
        existing_rows.extend(
            db.scalars(select(StockFinancialPit).where(StockFinancialPit.symbol.in_(batch_syms))).all()
        )
    existing_map = {(r.symbol, r.end_date, r.ann_date): r for r in existing_rows}

    # 合并库中已有历史季度以准确算出最新季度加速度
    combined: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, r in existing_map.items():
        combined[key] = {
            "symbol": r.symbol,
            "end_date": r.end_date,
            "ann_date": r.ann_date,
            "tr_yoy": r.tr_yoy,
            "netprofit_yoy": r.netprofit_yoy,
            "q_sales_yoy": r.q_sales_yoy,
            "q_profit_yoy": r.q_profit_yoy,
            "tr_yoy_accel": r.tr_yoy_accel,
            "profit_yoy_accel": r.profit_yoy_accel,
        }
    for key, payload in deduped.items():
        combined[key] = payload

    _compute_pit_accelerations(list(combined.values()))

    new_rows: list[dict[str, Any]] = []
    upserted = 0
    for key, payload in deduped.items():
        current = existing_map.get(key)
        if current is None:
            new_rows.append(payload)
        else:
            for field in (
                "roe",
                "grossprofit_margin",
                "netprofit_margin",
                "debt_to_assets",
                "tr_yoy",
                "netprofit_yoy",
                "q_sales_yoy",
                "q_profit_yoy",
                "tr_yoy_accel",
                "profit_yoy_accel",
                "ocf_yoy",
                "source",
            ):
                setattr(current, field, payload[field])
        upserted += 1
    for start in range(0, len(new_rows), 1000):
        db.execute(insert(StockFinancialPit), new_rows[start : start + 1000])
    return upserted


def sync_financial_pit(*, periods: Sequence[str] | None = None) -> dict[str, Any]:
    """按报告期批量拉取全市场财务指标（优先 fina_indicator_vip），严格按 ann_date 入库。"""
    target_periods = list(periods) if periods else recent_report_periods(count=6)
    upserted_total = 0
    per_period: dict[str, int] = {}
    errors: list[str] = []

    for period in target_periods:
        period_rows: list[dict[str, Any]] = []
        api_name = "fina_indicator_vip"
        for page in range(_MAX_PAGES):
            res = _tushare_request(
                api_name,
                {"period": period, "limit": _PAGE_LIMIT, "offset": page * _PAGE_LIMIT},
                _FINA_FIELDS,
            )
            if "error" in res and page == 0 and api_name == "fina_indicator_vip":
                # 兼容积分不足回退普通 fina_indicator
                api_name = "fina_indicator"
                res = _tushare_request(
                    api_name,
                    {"period": period, "limit": _PAGE_LIMIT, "offset": 0},
                    _FINA_FIELDS,
                )
            if "error" in res:
                errors.append(f"{api_name}({period}): {str(res['error'])[:180]}")
                break
            batch = res.get("rows") or []
            period_rows.extend(batch)
            if len(batch) < _PAGE_LIMIT:
                break
            time.sleep(_API_SLEEP_SECONDS)

        if period_rows:
            with SessionLocal() as db:
                written = upsert_financial_pit_rows(db, period_rows, source=f"tushare.{api_name}")
                db.commit()
            upserted_total += written
            per_period[period] = written
        else:
            per_period[period] = 0
        time.sleep(_API_SLEEP_SECONDS)

    return {
        "periods": target_periods,
        "upserted": upserted_total,
        "per_period": per_period,
        "errors": errors,
        "ready": not errors,
    }


def sync_financial_pit_for_date(trade_date: str) -> dict[str, Any]:
    """优先使用已开通的 TuShare PIT 因子库截面接口同步 8 个核心财务质量与增长加速度因子（全市场仅 8 次调用）。"""
    compact = _compact_date(trade_date)
    if not compact:
        return {"ready": False, "errors": [f"无效日期: {trade_date}"]}
    from backend.app.quant import tushare_factor_service

    res = tushare_factor_service.sync_factor_values(
        list(LAYERED_PIT_FACTOR_NAMES),
        start_date=compact,
        end_date=compact,
    )
    return {
        "trade_date": compact,
        "stored_rows": res.get("stored_rows", 0),
        "errors": res.get("errors", []),
        "ready": not res.get("errors"),
    }


def load_pit_financials(
    db: Session,
    *,
    as_of_date: str,
    symbols: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """严格 PIT 查询：融合 StockFinancialPit（按 ann_date<=as_of_date）与 TushareFactorValue（按 trade_date<=as_of_date）。"""
    compact = _compact_date(as_of_date)
    if not compact:
        return {}
    as_of_dt = datetime.strptime(compact, "%Y%m%d").date()
    min_end_date = f"{as_of_dt.year - 2}0101"

    stmt = select(StockFinancialPit).where(
        StockFinancialPit.ann_date <= compact,
        StockFinancialPit.end_date >= min_end_date,
    )
    allowed_set: set[str] | None = None
    if symbols is not None:
        sym_list = sorted({str(s).strip().upper() for s in symbols if s})
        if not sym_list:
            return {}
        allowed_set = set(sym_list)
        if len(sym_list) <= 500:
            stmt = stmt.where(StockFinancialPit.symbol.in_(sym_list))

    rows = db.scalars(
        stmt.order_by(
            StockFinancialPit.end_date.desc(),
            StockFinancialPit.ann_date.desc(),
            StockFinancialPit.id.desc(),
        )
    ).all()
    latest_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        if allowed_set is not None and row.symbol not in allowed_set:
            continue
        if row.symbol in latest_by_symbol:
            continue
        latest_by_symbol[row.symbol] = {
            "symbol": row.symbol,
            "end_date": row.end_date,
            "ann_date": row.ann_date,
            "roe": row.roe,
            "grossprofit_margin": row.grossprofit_margin,
            "netprofit_margin": row.netprofit_margin,
            "debt_to_assets": row.debt_to_assets,
            "tr_yoy": row.tr_yoy,
            "netprofit_yoy": row.netprofit_yoy,
            "q_sales_yoy": row.q_sales_yoy,
            "q_profit_yoy": row.q_profit_yoy,
            "tr_yoy_accel": row.tr_yoy_accel,
            "profit_yoy_accel": row.profit_yoy_accel,
            "ocf_yoy": row.ocf_yoy,
        }

    # 融合 tushare_factor_values 中 <= compact 的最近一日 PIT 财务与增速加速度因子
    from sqlalchemy import func as sa_func

    factor_day = db.scalar(
        select(sa_func.max(TushareFactorValue.trade_date)).where(
            TushareFactorValue.factor_name.in_(list(LAYERED_PIT_FACTOR_NAMES)),
            TushareFactorValue.trade_date <= compact,
        )
    )
    if factor_day:
        f_stmt = select(
            TushareFactorValue.symbol,
            TushareFactorValue.factor_name,
            TushareFactorValue.value,
        ).where(
            TushareFactorValue.trade_date == str(factor_day),
            TushareFactorValue.factor_name.in_(list(LAYERED_PIT_FACTOR_NAMES)),
            TushareFactorValue.value.is_not(None),
        )
        if allowed_set is not None and len(allowed_set) <= 500:
            f_stmt = f_stmt.where(TushareFactorValue.symbol.in_(sorted(allowed_set)))
        for sym_raw, fname, fval in db.execute(f_stmt).all():
            sym = str(sym_raw).strip().upper()
            if allowed_set is not None and sym not in allowed_set:
                continue
            val = _finite_float(fval)
            if val is None:
                continue
            entry = latest_by_symbol.setdefault(
                sym,
                {
                    "symbol": sym,
                    "end_date": None,
                    "ann_date": str(factor_day),
                    "roe": None,
                    "grossprofit_margin": None,
                    "netprofit_margin": None,
                    "debt_to_assets": None,
                    "tr_yoy": None,
                    "netprofit_yoy": None,
                    "q_sales_yoy": None,
                    "q_profit_yoy": None,
                    "tr_yoy_accel": None,
                    "profit_yoy_accel": None,
                    "ocf_yoy": None,
                },
            )
            if fname == "roe_ttm" and entry["roe"] is None:
                # TuShare roe_ttm 若为小数（如 0.12）转百分比以统一展示口径
                entry["roe"] = val * 100.0 if abs(val) <= 2.0 else val
            elif fname == "gpm_q" and entry["grossprofit_margin"] is None:
                entry["grossprofit_margin"] = val
            elif fname == "debt_asset_ratio" and entry["debt_to_assets"] is None:
                entry["debt_to_assets"] = val * 100.0 if 0 < val <= 1.5 else val
            elif fname == "yoy_ocf" and entry["ocf_yoy"] is None:
                entry["ocf_yoy"] = val
            elif fname == "yoy_net_profit" and entry["netprofit_yoy"] is None:
                entry["netprofit_yoy"] = val
            elif fname == "yoy_revenue" and entry["tr_yoy"] is None:
                entry["tr_yoy"] = val
            elif fname == "sa" and entry["tr_yoy_accel"] is None:
                entry["tr_yoy_accel"] = val
            elif fname == "delta_roe" and entry["profit_yoy_accel"] is None:
                entry["profit_yoy_accel"] = val

    return latest_by_symbol
