"""TuShare 官方因子库（付费接口）接入：目录同步 + 因子值按需摄入 + 本地面板读取。

职责边界：
- 只负责 TuShare 因子数据的进/出/查，不做 IC 等评估计算（那是 ``factor_service``）。
- ``factor_list`` 同步 212 个因子元信息到 ``tushare_factor_catalog``；
- ``factor_value`` 按 (因子, 交易日) 截面摄入到 ``tushare_factor_values``，幂等覆盖；
- 评估管线（``factor_service.benchmark_factors``）通过 ``load_factor_panel`` 读缓存面板，
  因子 id 形如 ``tushare:roe_ttm``。

摄入策略（按需缓存，不拉全量历史）：
- 截面模式（``factor_value(factor_name, trade_date)``）一天一次请求返回全市场，
  比逐股票序列模式省请求；本地按股票池过滤行——TuShare 值本身是全市场口径
  （部分为截面排名），过滤不改变保留值的含义。
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import delete, func, insert, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import TushareFactorCatalog, TushareFactorValue
from backend.app.market.stock_tools import _tushare_request, normalize_a_share_symbol

TUSHARE_FACTOR_ZOO = "tushare"
TUSHARE_FACTOR_PREFIX = "tushare:"
MAX_SYNC_FACTORS = 10
MAX_SYNC_CALLS = 30  # 单次请求的 factor_value 调用上限（30 次≈48s，远低于网关 /api 的 300s read timeout）；长区间由调用方带 offset 分片续传
MAX_SYNC_SYMBOLS = 50
API_PAUSE_SECONDS = 0.15
MAX_REPORTED_ERRORS = 20
_DATE_RE = re.compile(r"^\d{8}$")


def tushare_factor_id(factor_name: str) -> str:
    return f"{TUSHARE_FACTOR_PREFIX}{factor_name}"


def parse_tushare_factor_id(alpha_id: str) -> str | None:
    """``tushare:roe_ttm`` → ``roe_ttm``；非 tushare 因子返回 None。"""
    value = str(alpha_id or "").strip()
    if not value.startswith(TUSHARE_FACTOR_PREFIX):
        return None
    return value[len(TUSHARE_FACTOR_PREFIX):]


def _compact_date(value: str | None, *, fallback: str | None = None) -> str | None:
    raw = str(value or fallback or "").strip().replace("-", "")
    if not raw:
        return None
    if not _DATE_RE.fullmatch(raw):
        raise ValueError(f"日期格式不正确：{value!r}，应为 YYYYMMDD 或 YYYY-MM-DD")
    return raw


def _call_or_raise(api_name: str, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
    response = _tushare_request(api_name, params, fields)
    if response.get("error"):
        raise RuntimeError(f"TuShare {api_name} 调用失败：{response['error']}")
    return response.get("rows") or []


# ------------------------------------------------------------------ 目录


def sync_catalog() -> dict[str, Any]:
    """从 TuShare ``factor_list`` 全量同步因子目录（一次返回全部，无需分页）。"""
    rows = _call_or_raise("factor_list", {}, "")
    if not rows:
        raise RuntimeError("TuShare factor_list 返回为空，请确认账号已开通因子库权限")
    by_type: dict[str, int] = {}
    with SessionLocal() as db:
        for row in rows:
            name = str(row.get("factor_name") or "").strip()
            if not name:
                continue
            factor_type = str(row.get("factor_type") or "").strip()
            by_type[factor_type] = by_type.get(factor_type, 0) + 1
            existing = db.get(TushareFactorCatalog, name)
            if existing is None:
                db.add(TushareFactorCatalog(
                    factor_name=name,
                    asset_type=str(row.get("asset_type") or "STK").strip() or "STK",
                    factor_type=factor_type,
                    factor_desc=str(row.get("factor_desc") or "").strip() or None,
                ))
            else:
                existing.asset_type = str(row.get("asset_type") or existing.asset_type or "STK").strip()
                existing.factor_type = factor_type
                existing.factor_desc = str(row.get("factor_desc") or "").strip() or None
        db.commit()
    return {"total": sum(by_type.values()), "by_type": dict(sorted(by_type.items()))}


def ensure_catalog() -> None:
    """目录为空时自动同步一次（首次使用免手动点同步）。"""
    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(TushareFactorCatalog)) or 0
    if not total:
        sync_catalog()


def list_catalog(
    *,
    factor_type: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """目录 + 本地缓存覆盖情况（供管理面板与因子选择器展示）。"""
    with SessionLocal() as db:
        metas = db.scalars(
            select(TushareFactorCatalog).order_by(TushareFactorCatalog.factor_type, TushareFactorCatalog.factor_name)
        ).all()
        coverage_rows = db.execute(
            select(
                TushareFactorValue.factor_name,
                func.count(TushareFactorValue.id),
                func.min(TushareFactorValue.trade_date),
                func.max(TushareFactorValue.trade_date),
            ).group_by(TushareFactorValue.factor_name)
        ).all()
    coverage = {row[0]: {"rows": int(row[1]), "min_date": row[2], "max_date": row[3]} for row in coverage_rows}

    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    for meta in metas:
        if factor_type and meta.factor_type != factor_type:
            continue
        if needle:
            haystack = f"{meta.factor_name} {meta.factor_type} {meta.factor_desc or ''}".lower()
            if needle not in haystack:
                continue
        cached = coverage.get(meta.factor_name) or {}
        items.append({
            "id": tushare_factor_id(meta.factor_name),
            "zoo": TUSHARE_FACTOR_ZOO,
            "factor_name": meta.factor_name,
            "factor_type": meta.factor_type,
            "nickname": _nickname(meta.factor_type, meta.factor_desc),
            "factor_desc": meta.factor_desc or "",
            "cached_rows": cached.get("rows", 0),
            "cached_min_date": cached.get("min_date"),
            "cached_max_date": cached.get("max_date"),
        })
    return items


def _nickname(factor_type: str, factor_desc: str | None) -> str:
    desc = str(factor_desc or "").strip()
    first_line = desc.split("\n")[0] if desc else ""
    return f"{factor_type} · {first_line[:60]}" if first_line else factor_type


def catalog_factor_types() -> list[str]:
    with SessionLocal() as db:
        rows = db.execute(
            select(TushareFactorCatalog.factor_type, func.count(TushareFactorCatalog.factor_name))
            .group_by(TushareFactorCatalog.factor_type)
            .order_by(TushareFactorCatalog.factor_type)
        ).all()
    return [{"factor_type": row[0], "count": int(row[1])} for row in rows]


def known_factor_names() -> set[str]:
    with SessionLocal() as db:
        rows = db.scalars(select(TushareFactorCatalog.factor_name)).all()
    return set(rows)


# ------------------------------------------------------------------ 因子值摄入


def _trading_days(start: str, end: str) -> list[str]:
    rows = _call_or_raise(
        "trade_cal",
        {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": 1},
        "cal_date",
    )
    days = sorted(str(row.get("cal_date") or "").strip() for row in rows if row.get("cal_date"))
    if not days:
        raise ValueError("指定区间内没有交易日（请检查起止日期）")
    return days


def sync_factor_values(
    factor_names: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: Iterable[str] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """按截面模式摄入因子值：每 (因子, 交易日) 一次全市场请求，按需过滤股票池。

    幂等：同一 (因子, 日期) 重复同步时，有新数据则覆盖旧行。

    任务网格 = 因子 × 交易日（行优先）。单次请求最多执行 ``MAX_SYNC_CALLS`` 格，
    超出部分由调用方携带 ``offset`` 续传（前端自动循环分片），避免网关 60s 超时。
    """
    names = [str(name or "").strip() for name in dict.fromkeys(factor_names) if str(name or "").strip()]
    if not names:
        raise ValueError("至少选择一个因子")
    if len(names) > MAX_SYNC_FACTORS:
        raise ValueError(f"单次最多同步 {MAX_SYNC_FACTORS} 个因子")

    ensure_catalog()
    known = known_factor_names()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"TuShare 因子库中不存在这些因子：{', '.join(unknown[:5])}")

    pool: list[str] | None = None
    if symbols is not None:
        pool = []
        for value in symbols:
            symbol = normalize_a_share_symbol(str(value or "").strip())
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise ValueError(f"无法识别 A 股代码：{value}")
            if symbol not in pool:
                pool.append(symbol)
        if not pool:
            raise ValueError("股票池为空")
        if len(pool) > MAX_SYNC_SYMBOLS:
            raise ValueError(f"单次同步最多过滤 {MAX_SYNC_SYMBOLS} 只股票")

    end = _compact_date(end_date, fallback=date.today().strftime("%Y%m%d"))
    start = _compact_date(start_date, fallback=(date.today() - timedelta(days=90)).strftime("%Y%m%d"))
    if start is None or end is None or start > end:
        raise ValueError("开始日期不能晚于结束日期")

    trade_days = _trading_days(start, end)
    planned = len(names) * len(trade_days)
    offset = max(0, int(offset or 0))
    if offset > planned:
        offset = planned

    stored_total = 0
    per_factor: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    completed_cells = 0
    failed_cells = 0
    halted = False
    for name, day in [(names[i // len(trade_days)], trade_days[i % len(trade_days)])
                      for i in range(offset, min(offset + MAX_SYNC_CALLS, planned))]:
        stats = per_factor.setdefault(name, {"factor_name": name, "stored_rows": 0, "days": 0})
        response = _tushare_request("factor_value", {"factor_name": name, "trade_date": day}, "")
        if response.get("error"):
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.append({"factor_name": name, "trade_date": day, "error": str(response["error"])[:200]})
            failed_cells += 1
            time.sleep(API_PAUSE_SECONDS)
            completed_cells += 1
            continue
        rows = response.get("rows") or []
        if pool is not None:
            pool_set = set(pool)
            rows = [row for row in rows if row.get("ts_code") in pool_set]
        payload = [
            {
                "factor_name": name,
                "symbol": str(row.get("ts_code") or "").strip(),
                "trade_date": str(row.get("trade_date") or day),
                "factor_value": row.get("factor_value"),
            }
            for row in rows
            if row.get("ts_code")
        ]
        if payload:
            with SessionLocal() as db:
                db.execute(
                    delete(TushareFactorValue).where(
                        TushareFactorValue.factor_name == name,
                        TushareFactorValue.trade_date == day,
                        *([TushareFactorValue.symbol.in_(pool)] if pool is not None else []),
                    )
                )
                db.execute(insert(TushareFactorValue), payload)
                db.commit()
            stats["stored_rows"] += len(payload)
            stored_total += len(payload)
        stats["days"] += 1
        completed_cells += 1
        time.sleep(API_PAUSE_SECONDS)

    offset_next = offset + completed_cells
    has_more = offset_next < planned
    if has_more and completed_cells and failed_cells == completed_cells:
        # 本轮全部失败（如上游故障）：停止续传，避免前端空转
        halted = True
        has_more = False

    return {
        "window": {"start_date": start, "end_date": end},
        "trading_days": len(trade_days),
        "stored_rows": stored_total,
        "symbols_filtered": len(pool) if pool is not None else None,
        "factors": list(per_factor.values()),
        "errors": errors,
        "planned": planned,
        "offset": offset,
        "offset_next": offset_next,
        "has_more": has_more,
        "halted": halted,
        "note": (
            "因子值已缓存到本地，重复同步同一区间会覆盖旧值；"
            "长区间自动按每 30 次请求分片续传，缓存累积。"
        ),
    }


# ------------------------------------------------------------------ 读取


def load_factor_panel(
    factor_name: str,
    symbols: Iterable[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame | None:
    """读取缓存因子值并整形成 (日期 × 股票) 面板；无缓存返回 None。

    ``symbols`` 为 None 时读取该因子全部缓存行（全市场截面摄入模式）。
    """
    wanted = [str(symbol) for symbol in symbols] if symbols is not None else []
    with SessionLocal() as db:
        statement = select(TushareFactorValue).where(TushareFactorValue.factor_name == factor_name)
        if wanted:
            statement = statement.where(TushareFactorValue.symbol.in_(wanted))
        if start_date:
            statement = statement.where(TushareFactorValue.trade_date >= _compact_date(start_date))
        if end_date:
            statement = statement.where(TushareFactorValue.trade_date <= _compact_date(end_date))
        rows = db.scalars(statement).all()
    if not rows:
        return None
    frame = pd.DataFrame.from_records(
        [
            {
                "trade_date": pd.Timestamp(str(row.trade_date)),
                "symbol": row.symbol,
                "factor_value": float(row.factor_value) if row.factor_value is not None else None,
            }
            for row in rows
        ]
    )
    panel = frame.pivot(index="trade_date", columns="symbol", values="factor_value").sort_index()
    if wanted:
        panel = panel.reindex(columns=wanted)
    return panel.astype(float)


def query_values(
    factor_name: str,
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    name = str(factor_name or "").strip()
    if not name:
        raise ValueError("factor_name 不能为空")
    with SessionLocal() as db:
        statement = (
            select(TushareFactorValue)
            .where(TushareFactorValue.factor_name == name)
            .order_by(TushareFactorValue.trade_date.desc(), TushareFactorValue.symbol)
        )
        if symbol:
            statement = statement.where(TushareFactorValue.symbol == normalize_a_share_symbol(symbol))
        if start_date:
            statement = statement.where(TushareFactorValue.trade_date >= _compact_date(start_date))
        if end_date:
            statement = statement.where(TushareFactorValue.trade_date <= _compact_date(end_date))
        rows = db.scalars(statement.limit(max(1, min(int(limit), 2000)))).all()
    return [
        {
            "symbol": row.symbol,
            "trade_date": row.trade_date,
            "factor_value": row.factor_value,
        }
        for row in rows
    ]


# ------------------------------------------------------------------ Agent 工具数据源

MAX_TOOL_SYMBOLS = 20
MAX_TOOL_ROWS = 500
MAX_TOOL_CATALOG_ROWS = 50


def get_tushare_factors(
    factor_name: str | None = None,
    *,
    symbols: Iterable[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Agent 工具 ``get_tushare_factors`` 的数据源，只读本地缓存，不外呼。

    - ``factor_name`` 缺省：返回目录（前 50 个，含缓存覆盖），供数字人了解有哪些因子；
    - 指定 ``factor_name``：返回该因子已缓存的因子值（建议给定 ``symbols``；
      缺省按最近缓存行截断返回）。
    """
    if not str(factor_name or "").strip():
        items = list_catalog()
        rows = [
            {
                "factor_name": item["factor_name"],
                "factor_type": item["factor_type"],
                "nickname": item["nickname"],
                "cached_rows": item["cached_rows"],
                "cached_max_date": item["cached_max_date"],
            }
            for item in items[:MAX_TOOL_CATALOG_ROWS]
        ]
        return {
            "source": "tushare.factor_catalog",
            "count": len(rows),
            "rows": rows,
            "truncated": len(items) > len(rows),
        }

    name = str(factor_name).strip()
    if name not in known_factor_names():
        raise ValueError(f"TuShare 因子库中不存在因子：{name}")
    wanted = list(dict.fromkeys(normalize_a_share_symbol(str(symbol)) for symbol in (symbols or [])))
    if len(wanted) > MAX_TOOL_SYMBOLS:
        raise ValueError(f"股票数量不能超过 {MAX_TOOL_SYMBOLS} 只")

    with SessionLocal() as db:
        statement = (
            select(TushareFactorValue)
            .where(TushareFactorValue.factor_name == name)
            .order_by(TushareFactorValue.trade_date.desc(), TushareFactorValue.symbol)
        )
        if wanted:
            statement = statement.where(TushareFactorValue.symbol.in_(wanted))
        if start_date:
            statement = statement.where(TushareFactorValue.trade_date >= _compact_date(start_date))
        if end_date:
            statement = statement.where(TushareFactorValue.trade_date <= _compact_date(end_date))
        cap = max(1, min(int(limit or 100), MAX_TOOL_ROWS))
        rows = db.scalars(statement.limit(cap)).all()
    return {
        "source": "tushare.factor_value",
        "factor_name": name,
        "count": len(rows),
        "rows": [
            {
                "symbol": row.symbol,
                "trade_date": row.trade_date,
                "factor_value": row.factor_value,
            }
            for row in rows
        ],
        "truncated": len(rows) >= cap,
        "note": "因子值来自本地缓存（以最近同步窗口为限）；横截面排序打分请用 score_factor_universe。",
    }
