"""全市场公告事件排雷同步（L6 事件与窗口底座）。

解决旧架构盲区：原先 market_events 只保留持仓与自选股公告，非自选的 5000+ 只股票即使发了
立案调查、退市风险警示、财务造假非标意见也不会落库。
本模块复用 events.rules 的成熟标题分类器，把全市场重大/重要公告落入公共表 stock_event_signals，
供 L6 分层选股按真实披露日（ann_date <= trade_date）做 PIT 一票否决与利好/利空打分。
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import StockEventSignal
from backend.app.events import rules as event_rules
from backend.app.market.stock_tools import _tushare_request

logger = logging.getLogger(__name__)

VETO_POLARITY = "veto"
NEGATIVE_POLARITY = "negative"
POSITIVE_POLARITY = "positive"
NEUTRAL_POLARITY = "neutral"

VETO_EVENT_TYPES = frozenset({"立案调查", "退市风险", "财务造假"})

_POSITIVE_EARNINGS_RE = re.compile(r"预增|扭亏|大增|翻倍|增长\s*[3-9]\d|增长\s*\d{3}")
_NEGATIVE_EARNINGS_RE = re.compile(r"预亏|首亏|续亏|预减|腰斩|下滑|下降|亏损")
_REDUCE_RE = re.compile(r"减持")
_INCREASE_RE = re.compile(r"增持")
_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")

_ANNOUNCEMENT_FIELDS = "ts_code,ann_date,name,title,url"
_ANNOUNCEMENT_PAGE = 2000
_ANNOUNCEMENT_MAX_PAGES = 5
_API_SLEEP_SECONDS = 0.2


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def infer_event_polarity(classification: event_rules.Classification, title: str) -> str:
    """根据事件类型与标题关键词做确定性极性判定（不调 LLM，保证 5000 只股票毫秒级可复现）。"""
    if classification.event_type in VETO_EVENT_TYPES:
        return VETO_POLARITY
    if classification.event_type in {"监管", "诉讼", "质押冻结"}:
        return NEGATIVE_POLARITY
    if classification.event_type in {"回购", "重大合同", "分红送转"}:
        return POSITIVE_POLARITY
    if classification.event_type == "增减持":
        if _REDUCE_RE.search(title) and not _INCREASE_RE.search(title):
            return NEGATIVE_POLARITY
        if _INCREASE_RE.search(title) and not _REDUCE_RE.search(title):
            return POSITIVE_POLARITY
        return NEUTRAL_POLARITY
    if classification.event_type == "业绩":
        if _NEGATIVE_EARNINGS_RE.search(title):
            return NEGATIVE_POLARITY
        if _POSITIVE_EARNINGS_RE.search(title):
            return POSITIVE_POLARITY
        return NEUTRAL_POLARITY
    return NEUTRAL_POLARITY


def record_public_event_signals(
    db: Session,
    rows: Sequence[dict[str, Any]],
    *,
    source: str = "tushare:anns_d",
) -> int:
    """将全市场公告列表中的 CRITICAL / MAJOR 事件幂等写入 stock_event_signals。"""
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").strip().upper()
        title = str(row.get("title") or "").strip()
        ann_date = _compact_date(row.get("ann_date"))
        if not _SYMBOL_RE.fullmatch(symbol) or not title or not ann_date:
            continue
        cls = event_rules.classify(title)
        if cls.severity not in (event_rules.CRITICAL, event_rules.MAJOR):
            continue
        digest = event_rules.event_hash(symbol, title)
        candidates[(symbol, digest)] = {
            "symbol": symbol,
            "ann_date": ann_date,
            "event_type": cls.event_type,
            "severity": cls.severity,
            "polarity": infer_event_polarity(cls, title),
            "title": title[:500],
            "url": (str(row.get("url") or "").strip()[:500] or None),
            "content_hash": digest,
            "source": source,
        }
    if not candidates:
        return 0

    symbols = sorted({sym for sym, _ in candidates})
    hashes = sorted({h for _, h in candidates})
    existing_keys: set[tuple[str, str]] = set()
    # 分批查询防 IN 列表过大
    for start in range(0, len(hashes), 500):
        batch_hashes = hashes[start : start + 500]
        found = db.execute(
            select(StockEventSignal.symbol, StockEventSignal.content_hash).where(
                StockEventSignal.symbol.in_(symbols),
                StockEventSignal.content_hash.in_(batch_hashes),
            )
        ).all()
        existing_keys.update((str(sym), str(h)) for sym, h in found)

    new_rows = [payload for key, payload in candidates.items() if key not in existing_keys]
    for start in range(0, len(new_rows), 1000):
        db.execute(insert(StockEventSignal), new_rows[start : start + 1000])
    return len(new_rows)


def sync_market_event_signals(
    *,
    end_date: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """按日期拉取最近 days 个自然日的全市场公告并提取重大/重要信号入库。"""
    target_end = _compact_date(end_date) or date.today().strftime("%Y%m%d")
    end_dt = datetime.strptime(target_end, "%Y%m%d").date()
    span = max(1, min(int(days), 30))

    fetched_rows = 0
    inserted_total = 0
    errors: list[str] = []

    for offset in range(span - 1, -1, -1):
        day_str = (end_dt - timedelta(days=offset)).strftime("%Y%m%d")
        day_rows: list[dict[str, Any]] = []
        for page in range(_ANNOUNCEMENT_MAX_PAGES):
            res = _tushare_request(
                "anns_d",
                {"ann_date": day_str, "limit": _ANNOUNCEMENT_PAGE, "offset": page * _ANNOUNCEMENT_PAGE},
                _ANNOUNCEMENT_FIELDS,
            )
            if "error" in res:
                errors.append(f"anns_d({day_str}): {str(res['error'])[:160]}")
                break
            batch = res.get("rows") or []
            day_rows.extend(batch)
            if len(batch) < _ANNOUNCEMENT_PAGE:
                break
            time.sleep(_API_SLEEP_SECONDS)
        fetched_rows += len(day_rows)
        if day_rows:
            with SessionLocal() as db:
                inserted_total += record_public_event_signals(db, day_rows)
                db.commit()
        time.sleep(_API_SLEEP_SECONDS)

    return {
        "end_date": target_end,
        "days": span,
        "fetched_rows": fetched_rows,
        "inserted_signals": inserted_total,
        "errors": errors,
        "ready": not errors,
    }


def load_pit_event_signals(
    db: Session,
    *,
    as_of_date: str,
    lookback_days: int = 45,
    veto_lookback_days: int = 90,
    symbols: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """严格 PIT 读取不晚于 as_of_date 的事件信号。

    立案调查/退市风险/财务造假的一票否决窗口默认回看 90 个自然日；一般增减持/业绩事件回看 45 个自然日。
    绝不读取 ann_date > as_of_date 的未来公告。
    """
    compact = _compact_date(as_of_date)
    if not compact:
        return {}
    as_of_dt = datetime.strptime(compact, "%Y%m%d").date()
    earliest_veto = (as_of_dt - timedelta(days=max(lookback_days, veto_lookback_days))).strftime("%Y%m%d")
    earliest_regular = (as_of_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")

    stmt = select(StockEventSignal).where(
        StockEventSignal.ann_date >= earliest_veto,
        StockEventSignal.ann_date <= compact,
    )
    if symbols is not None:
        sym_list = sorted({str(s).strip().upper() for s in symbols if s})
        if not sym_list:
            return {}
        if len(sym_list) <= 500:
            stmt = stmt.where(StockEventSignal.symbol.in_(sym_list))

    rows = db.scalars(stmt.order_by(StockEventSignal.ann_date.desc(), StockEventSignal.id.desc())).all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    allowed_set = {str(s).strip().upper() for s in symbols} if symbols is not None else None

    for row in rows:
        if allowed_set is not None and row.symbol not in allowed_set:
            continue
        if row.polarity != VETO_POLARITY and row.ann_date < earliest_regular:
            continue
        grouped[row.symbol].append(
            {
                "ann_date": row.ann_date,
                "event_type": row.event_type,
                "severity": row.severity,
                "polarity": row.polarity,
                "title": row.title,
                "url": row.url,
            }
        )
    return dict(grouped)
