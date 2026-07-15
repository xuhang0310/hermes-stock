from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, desc, func, select, update
from sqlalchemy.orm import Session

from backend.app.data.broker_presets import broker_code_for_name, broker_name_for
from backend.app.core.models import (
    AgentProfile,
    AgentReport,
    AgentTask,
    DailyBar,
    Event,
    EventEvidence,
    Instrument,
    Position,
    PositionAccount,
    PositionHoldingSnapshot,
    PositionImportBatch,
    RiskCheck,
    SystemLog,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.market.stock_tools import get_tushare_daily, get_tushare_minutes


def _account_eq(account_id: int | None):
    return Position.account_id == account_id if account_id is not None else Position.account_id.is_(None)


CHINA_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_RESOLUTIONS = {"1": "1min", "5": "5min", "15": "15min", "30": "30min", "60": "60min"}


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def dt(value: Any) -> str | None:
    return value.isoformat() if value else None


def normalize_a_share_symbol(symbol: str) -> str:
    value = (symbol or "").strip().upper()
    if not value:
        raise ValueError("请输入标的代码")
    value = value.replace("_", ".")
    prefixed = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", value)
    if prefixed:
        return f"{prefixed.group(2)}.{prefixed.group(1)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value
    pure = re.fullmatch(r"\d{6}", value)
    if pure:
        if value.startswith(("6", "9")):
            return f"{value}.SH"
        if value.startswith(("4", "8")):
            return f"{value}.BJ"
        return f"{value}.SZ"
    return value


def exchange_from_symbol(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "SSE"
    if symbol.endswith(".SZ"):
        return "SZSE"
    if symbol.endswith(".BJ"):
        return "BSE"
    return "A-share"


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text_value = str(value).strip()
    if text_value in {"-", "--", "—", "无", "null", "None"}:
        return None
    multiplier = 1.0
    text_value = text_value.replace(",", "").replace("，", "").replace(" ", "")
    if text_value.endswith("万"):
        multiplier = 10000.0
        text_value = text_value[:-1]
    elif text_value.endswith("亿"):
        multiplier = 100000000.0
        text_value = text_value[:-1]
    text_value = text_value.replace("%", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text_value)
    return float(match.group(0)) * multiplier if match else None


def system_log_out(row: SystemLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "level": row.level,
        "module": row.module,
        "message": row.message,
        "context": parse_json(row.context_json),
        "created_at": dt(row.created_at),
    }


class StockRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "database": self.db.bind.url.render_as_string(hide_password=True) if self.db.bind else "",
            "user_version": 1,
            "journal_mode": "mysql",
            "integrity": "ok",
            "missing_tables": [],
            "event_count": self.db.scalar(select(func.count(Event.id))) or 0,
            "report_count": self.db.scalar(select(func.count(AgentReport.id))) or 0,
            "log_count": self.db.scalar(select(func.count(SystemLog.id))) or 0,
        }

    def dashboard(self) -> dict[str, Any]:
        return {
            "title": "SixRabbits A股研究台",
            "market_status": "A股研究模式",
            "pipeline": ["数据源", "数据网关", "MySQL", "规则引擎", "Agent路由", "通知/日报"],
            "disclaimer": "仅供研究与复盘，不自动下单，不构成投资建议",
            "counts": {
                "open_positions": self.db.scalar(select(func.count(Position.id)).where(Position.status == "open")) or 0,
                "watchlist": self.db.scalar(select(func.count(WatchlistItem.id)).where(WatchlistItem.status == "active")) or 0,
                "l0_events": self.db.scalar(select(func.count(Event.id)).where(Event.level == "L0")) or 0,
                "reports": self.db.scalar(select(func.count(AgentReport.id))) or 0,
            },
            "recent_events": self.events(limit=8),
            "positions": self.positions(),
            "agents": self.agents(),
            "logs": self.system_logs(limit=8),
        }

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(Event, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == Event.symbol)
            .order_by(case((Event.level == "L0", 0), (Event.level == "L1", 1), else_=2), desc(Event.created_at))
            .limit(limit)
        ).all()
        return [self._event_out(event, symbol_name) for event, symbol_name in rows]

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            select(Event, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == Event.symbol)
            .where(Event.event_id == event_id)
        ).first()
        if row is None:
            return None

        event, symbol_name = row
        result = self._event_out(event, symbol_name)
        result["evidence"] = [
            {
                "evidence_type": item.evidence_type,
                "source_table": item.source_table,
                "source_id": item.source_id,
                "content": item.content,
                "created_at": dt(item.created_at),
            }
            for item in self.db.scalars(
                select(EventEvidence).where(EventEvidence.event_id == event.id).order_by(desc(EventEvidence.created_at))
            )
        ]
        result["reports"] = [
            {
                "report_id": item.report_id,
                "agent_name": item.agent_name,
                "title": item.title,
                "conclusion": item.conclusion,
                "action_suggestion": item.action_suggestion,
                "created_at": dt(item.created_at),
            }
            for item in self.db.scalars(
                select(AgentReport).where(AgentReport.event_id == event.event_id).order_by(desc(AgentReport.created_at))
            )
        ]
        return result

    def positions(self, account_id: int | None = None) -> list[dict[str, Any]]:
        # 按 (账户, 标的) 取最新快照，使同一 symbol 在不同券商账户各保留一条
        latest_ids = (
            select(
                func.coalesce(PositionHoldingSnapshot.account_id, 0).label("acc"),
                PositionHoldingSnapshot.symbol,
                func.max(PositionHoldingSnapshot.id).label("snapshot_id"),
            )
            .group_by(func.coalesce(PositionHoldingSnapshot.account_id, 0), PositionHoldingSnapshot.symbol)
            .subquery()
        )
        query = (
            select(Position, Instrument.name, PositionHoldingSnapshot, PositionAccount)
            .join(Instrument, Instrument.symbol == Position.symbol)
            .outerjoin(
                latest_ids,
                (latest_ids.c.acc == func.coalesce(Position.account_id, 0)) & (latest_ids.c.symbol == Position.symbol),
            )
            .outerjoin(PositionHoldingSnapshot, PositionHoldingSnapshot.id == latest_ids.c.snapshot_id)
            .outerjoin(PositionAccount, PositionAccount.id == Position.account_id)
            .order_by(
                Position.status,
                desc(func.coalesce(PositionHoldingSnapshot.position_pct, Position.position_pct)),
                desc(func.coalesce(PositionHoldingSnapshot.captured_at, Position.updated_at)),
            )
        )
        if account_id is not None:
            query = query.where(Position.account_id == account_id)
        rows = self.db.execute(query).all()
        result = []
        for item, name, snapshot, account in rows:
            quantity = snapshot.position_quantity if snapshot else item.quantity
            position_pct = snapshot.position_pct if snapshot and snapshot.position_pct is not None else item.position_pct
            result.append(
                {
                    "id": item.id,
                    "symbol": item.symbol,
                    "name": snapshot.symbol_name if snapshot and snapshot.symbol_name else name,
                    "position_pct": position_pct,
                    "cost_price": snapshot.cost_price if snapshot else item.cost_price,
                    "quantity": quantity,
                    "position_quantity": quantity,
                    "available_quantity": snapshot.available_quantity if snapshot else None,
                    "frozen_quantity": snapshot.frozen_quantity if snapshot else None,
                    "today_buy_quantity": snapshot.today_buy_quantity if snapshot else None,
                    "today_sell_quantity": snapshot.today_sell_quantity if snapshot else None,
                    "cost_amount": snapshot.cost_amount if snapshot else None,
                    "latest_price": snapshot.latest_price if snapshot else None,
                    "market_value": snapshot.market_value if snapshot else None,
                    "floating_pnl": snapshot.floating_pnl if snapshot else None,
                    "floating_pnl_pct": snapshot.floating_pnl_pct if snapshot else None,
                    "day_pnl": snapshot.day_pnl if snapshot else None,
                    "stop_price": snapshot.stop_price if snapshot and snapshot.stop_price is not None else item.stop_price,
                    "take_profit_price": (
                        snapshot.take_profit_price if snapshot and snapshot.take_profit_price is not None else item.take_profit_price
                    ),
                    "thesis": snapshot.thesis if snapshot and snapshot.thesis else item.thesis,
                    "risk_note": snapshot.risk_note if snapshot and snapshot.risk_note else item.risk_note,
                    "status": item.status,
                    "opened_at": dt(item.opened_at),
                    "updated_at": dt(snapshot.captured_at if snapshot else item.updated_at),
                    "as_of_date": snapshot.as_of_date if snapshot else None,
                    "source_type": snapshot.source_type if snapshot else "manual",
                    "confidence": snapshot.confidence if snapshot else None,
                    "account_id": item.account_id,
                    "broker_name": account.broker_name if account else None,
                    "broker_code": account.broker_code if account else None,
                    "account_name": account.account_name if account else None,
                    "account_no_masked": account.account_no_masked if account else None,
                }
            )
        return result

    def position_chart(self, symbol: str, limit: int = 240, refresh: bool = False, resolution: str = "1D") -> dict[str, Any]:
        normalized = normalize_a_share_symbol(symbol)
        instrument = self.db.get(Instrument, normalized)
        position = next((item for item in self.positions() if item["symbol"] == normalized), None)
        normalized_resolution = self._normalize_chart_resolution(resolution)
        is_daily = normalized_resolution == "1D"
        bars = [] if refresh or not is_daily else self._local_daily_bars(normalized, limit)
        source = "local.daily_bars" if bars else "empty"
        remote_error = None

        if not is_daily:
            freq = MINUTE_RESOLUTIONS[normalized_resolution]
            remote = get_tushare_minutes(normalized, freq=freq, limit=limit)
            if "error" in remote:
                remote_error = remote["error"]
            else:
                remote_bars = self._chart_bars_from_tushare_minutes(remote.get("bars") or [])
                if remote_bars:
                    bars = remote_bars
                    source = remote.get("source") or f"tushare.stk_mins.{freq}"
        elif refresh or len(bars) < min(limit, 60):
            remote = get_tushare_daily(normalized, limit=limit)
            if "error" in remote:
                remote_error = remote["error"]
            else:
                remote_bars = self._chart_bars_from_tushare(remote.get("bars") or [])
                if remote_bars:
                    bars = remote_bars
                    source = remote.get("source") or "tushare.daily"

        if not bars:
            empty_message = (
                "暂无可用分钟 K 线数据，请检查 TuShare 历史分钟权限或切回日线。"
                if not is_daily
                else "暂无可用 K 线数据，请检查 TuShare 数据源或先同步日线。"
            )
            return {
                "symbol": normalized,
                "name": position.get("name") if position else (instrument.name if instrument else normalized),
                "source": source,
                "resolution": normalized_resolution,
                "error": remote_error or empty_message,
                "bars": [],
                "overlays": {},
                "position": position,
            }

        overlays = self._chart_overlays(bars)
        return {
            "symbol": normalized,
            "name": position.get("name") if position else (instrument.name if instrument else normalized),
            "source": source,
            "resolution": normalized_resolution,
            "error": remote_error,
            "bars": bars[-limit:],
            "overlays": overlays,
            "position": position,
        }

    @staticmethod
    def _normalize_chart_resolution(value: str | None) -> str:
        text = str(value or "1D").strip().upper()
        if text in {"D", "DAY", "1DAY", "1D"}:
            return "1D"
        text = text.replace("MIN", "").replace("M", "")
        if text in MINUTE_RESOLUTIONS:
            return text
        raise ValueError("不支持的图表周期，仅支持日线、1分、5分、15分、30分、60分")

    def _local_daily_bars(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(DailyBar)
            .where(DailyBar.symbol == symbol)
            .order_by(desc(DailyBar.trade_date), desc(DailyBar.id))
            .limit(limit)
        ).all()
        bars = [
            {
                "time": self._chart_time(row.trade_date),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "change_pct": row.change_pct,
            }
            for row in reversed(rows)
        ]
        return [row for row in bars if row["time"] and row["open"] is not None and row["high"] is not None and row["low"] is not None and row["close"] is not None]

    def _chart_bars_from_tushare(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bars = []
        for row in rows:
            trade_date = self._chart_time(str(row.get("trade_date") or ""))
            if not trade_date:
                continue
            open_price = number_or_none(row.get("open"))
            high = number_or_none(row.get("high"))
            low = number_or_none(row.get("low"))
            close = number_or_none(row.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            bars.append(
                {
                    "time": trade_date,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": number_or_none(row.get("vol") or row.get("volume")),
                    "amount": number_or_none(row.get("amount")),
                    "change_pct": number_or_none(row.get("pct_chg") or row.get("change_pct")),
                }
            )
        return bars

    def _chart_bars_from_tushare_minutes(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bars = []
        previous_close = None
        for row in rows:
            trade_time = self._chart_time(str(row.get("trade_time") or ""))
            if not trade_time:
                continue
            open_price = number_or_none(row.get("open"))
            high = number_or_none(row.get("high"))
            low = number_or_none(row.get("low"))
            close = number_or_none(row.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            change_pct = (close / previous_close - 1) * 100 if previous_close else None
            bars.append(
                {
                    "time": trade_time,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": number_or_none(row.get("vol") or row.get("volume")),
                    "amount": number_or_none(row.get("amount")),
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                }
            )
            previous_close = close
        return bars

    @staticmethod
    def _chart_time(value: str | None) -> str | int | None:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{8}", text):
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            dt_value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
            return int(dt_value.timestamp())
        return None

    @staticmethod
    def _chart_overlays(bars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        closes = [number_or_none(row.get("close")) for row in bars]

        def ma(window: int) -> list[dict[str, Any]]:
            points = []
            for index, row in enumerate(bars):
                if index + 1 < window:
                    continue
                segment = [value for value in closes[index + 1 - window : index + 1] if value is not None]
                if len(segment) != window:
                    continue
                points.append({"time": row["time"], "value": round(sum(segment) / window, 4)})
            return points

        return {
            "ma5": ma(5),
            "ma10": ma(10),
            "ma20": ma(20),
            "ma60": ma(60),
        }

    def save_position_holding(self, payload: dict[str, Any], source_type: str = "manual") -> dict[str, Any]:
        saved = self._save_position_holding(payload, source_type=source_type, commit=True)
        return saved

    # ---- merge_position_holding ----

    # 合并动作，不做数量/成本的拆分计算
    _MERGE_OVERWRITE_ACTIONS: set[str] = {"replace", "update_field"}
    # 累加动作（买入/加仓），保存后自动加入自选
    _MERGE_ACCUMULATE_ACTIONS: set[str] = {"buy", "add"}
    # 扣减动作（卖出/减仓）
    _MERGE_DECREMENT_ACTIONS: set[str] = {"sell", "reduce"}

    def merge_position_holding(
        self,
        payload: dict[str, Any],
        *,
        action: str | None = None,
        source_type: str = "manual",
        commit: bool = True,
    ) -> dict[str, Any]:
        """按交易动作合并持仓，买入/加仓自动同步自选。

        与 _save_position_holding 的关系：
        - replace / None → 直接调 _save_position_holding（向后兼容）
        - buy / add    → 查现有 open 持仓 → 累加数量 + 加权平均成本 → 调 _save_position_holding
        - sell / reduce → 查现有 open 持仓 → 扣减数量 → 调 _save_position_holding
        - close        → status=closed, quantity=0
        - update_field → 调 update_position_field（不改数量）
        """

        norm = normalize_a_share_symbol(
            str(payload.get("symbol") or payload.get("ts_code") or payload.get("code") or "")
        )

        # ---- update_field 走独立路径 ----
        if action == "update_field":
            field = str(payload.get("field") or "").strip()
            value = payload.get("value")
            if not field:
                return {"error": "update_field 必须提供 field"}
            return self.update_position_field(norm, field, value, account_id=payload.get("account_id"))

        # ---- replace / 无 action → 直接保存（向后兼容）----
        if not action or action == "replace":
            return self._save_position_holding(payload, source_type=source_type, commit=commit)

        # ---- close → 强制关仓 ----
        if action == "close":
            # 解析账号（与买方/卖方共享逻辑）
            if "account_id" in payload and payload["account_id"] is not None:
                acc_id = int(payload["account_id"])
            else:
                _, acc_id = self._unique_open_account_for(norm)
            position = self.db.scalar(
                select(Position)
                .where(Position.symbol == norm, Position.status == "open", _account_eq(acc_id))
                .order_by(desc(Position.id))
                .limit(1)
            )
            if position is None:
                position = self.db.scalar(
                    select(Position).where(Position.symbol == norm, _account_eq(acc_id)).order_by(desc(Position.id)).limit(1)
                )
            if position is not None:
                position.status = "closed"
                position.quantity = 0
                position.closed_at = datetime.now()
                self.db.flush()
                if commit:
                    self.db.commit()
                return {"symbol": norm, "action": "close", "closed": True, "position_id": position.id}
            # 没有持仓也返回成功（幂等）
            return {"symbol": norm, "action": "close", "closed": False, "note": "无 open 持仓可关闭"}

        # ---- 需要合并的买方/卖方动作 ----
        new_qty_raw = float(payload.get("position_quantity") or payload.get("quantity") or 0)
        new_cost_price = float(payload.get("cost_price") or 0) or None

        # 解析账号：优先用 payload 传入的 account_id，其次按标的自动匹配已有持仓的账户
        if "account_id" in payload and payload["account_id"] is not None:
            acc_id: int | None = int(payload["account_id"])
        else:
            _, acc_id = self._unique_open_account_for(norm)
        # 让 _ensure_position_account 拿到正确的 account_id，避免重复建账户
        if acc_id is not None:
            payload["account_id"] = acc_id

        if action in self._MERGE_ACCUMULATE_ACTIONS:
            # 查现有 open position 做累加
            position = self.db.scalar(
                select(Position)
                .where(Position.symbol == norm, Position.status == "open", _account_eq(acc_id))
                .order_by(desc(Position.id))
                .limit(1)
            )
            if position is not None and position.quantity is not None and position.quantity > 0:
                old_qty = position.quantity
                old_cost = position.cost_price or 0
                total_qty = old_qty + new_qty_raw
                # 加权平均成本
                if new_cost_price and new_cost_price > 0 and old_cost > 0:
                    weighted_cost = round((old_qty * old_cost + new_qty_raw * new_cost_price) / total_qty, 4)
                elif new_cost_price and new_cost_price > 0:
                    weighted_cost = new_cost_price
                else:
                    weighted_cost = old_cost or 0
                payload["position_quantity"] = total_qty
                payload["cost_price"] = weighted_cost
            else:
                payload["position_quantity"] = new_qty_raw
                if new_cost_price:
                    payload["cost_price"] = new_cost_price

            payload["status"] = "open"
            saved = self._save_position_holding(payload, source_type=source_type, commit=commit)

            # 买入/加仓 → 自动加入自选
            symbol_name = str(payload.get("name") or "").strip()
            try:
                self.add_watchlist_item(
                    symbol=norm,
                    group_name="默认",
                    reason=f"持仓买入 - {symbol_name}" if symbol_name else "持仓买入",
                )
            except ValueError:
                pass  # instrument 不存在时安全跳过

            return saved

        if action in self._MERGE_DECREMENT_ACTIONS:
            # 账号解析复用买方逻辑（payload 无 account_id 时自动匹配已有持仓的账户）
            if "account_id" in payload and payload["account_id"] is not None:
                acc_id = int(payload["account_id"])
            else:
                _, acc_id = self._unique_open_account_for(norm)
            if acc_id is not None:
                payload["account_id"] = acc_id
            position = self.db.scalar(
                select(Position)
                .where(Position.symbol == norm, Position.status == "open", _account_eq(acc_id))
                .order_by(desc(Position.id))
                .limit(1)
            )
            if position is not None and position.quantity is not None and position.quantity > 0:
                remaining = max(0.0, position.quantity - new_qty_raw)
                payload["position_quantity"] = remaining
                payload["status"] = "closed" if remaining == 0 else "open"
                # 卖出时不重复传成本价就沿用旧成本，避免覆盖成 None
                if "cost_price" not in payload or not payload["cost_price"]:
                    payload["cost_price"] = position.cost_price
            else:
                # 无持仓可卖
                return {"error": f"卖出失败: {norm} 无可卖出持仓", "symbol": norm, "action": action}

            saved = self._save_position_holding(payload, source_type=source_type, commit=commit)
            return saved

        # 未知 action → 退化到覆盖
        return self._save_position_holding(payload, source_type=source_type, commit=commit)

    def import_position_rows(
        self,
        rows: list[dict[str, Any]],
        batch: PositionImportBatch,
        default_account: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        saved_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            merged = {**(default_account or {}), **row}
            try:
                saved_rows.append(self._save_position_holding(merged, source_type="ai_ocr", import_batch=batch, commit=False))
            except ValueError as exc:
                saved_rows.append({"error": str(exc), "raw": row})
        return saved_rows

    def create_position_import_batch(
        self,
        *,
        source_name: str,
        image_mime_type: str,
        image_sha256: str | None,
        raw_text: str | None,
        parsed_json: str | None,
        created_by: str | None,
    ) -> PositionImportBatch:
        batch = PositionImportBatch(
            source_type="ai_ocr",
            source_name=source_name,
            status="pending",
            image_mime_type=image_mime_type,
            image_sha256=image_sha256,
            raw_text=raw_text,
            parsed_json=parsed_json,
            created_by=created_by,
        )
        self.db.add(batch)
        self.db.flush()
        return batch

    def finish_position_import_batch(self, batch: PositionImportBatch, saved_rows: list[dict[str, Any]], error: str | None = None) -> None:
        batch.status = "failed" if error else ("done" if any("error" not in row for row in saved_rows) else "empty")
        batch.error = error
        if not batch.parsed_json:
            batch.parsed_json = json.dumps(saved_rows, ensure_ascii=False)
        self.db.add(
            SystemLog(
                level="INFO" if not error else "ERROR",
                module="positions",
                message="position screenshot import finished",
                context_json=json.dumps(
                    {"batch_id": batch.id, "status": batch.status, "saved": len([row for row in saved_rows if "error" not in row])},
                    ensure_ascii=False,
                ),
            )
        )
        self.db.commit()

    def update_position_field(
        self,
        symbol: str,
        field: str,
        value: Any,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """字段级微调：更新某标的最新持仓快照的单个字段并同步 Position 摘要。

        仅允许改记录类字段（止损/止盈/持仓逻辑/风险说明/仓位），不碰数量/成本/价格等行情字段。
        """
        allowed = {"stop_price", "take_profit_price", "thesis", "risk_note", "position_pct"}
        if field not in allowed:
            return {"error": f"不允许修改字段 {field}，仅支持 {', '.join(sorted(allowed))}"}
        norm = normalize_a_share_symbol(symbol)
        # 优先命中该标的现有 open 持仓所在账户；无法唯一归属才用调用方传的 account_id
        matched, matched_id = self._unique_open_account_for(norm)
        effective_id = matched_id if matched else account_id
        snap = self.db.scalar(
            select(PositionHoldingSnapshot)
            .where(PositionHoldingSnapshot.symbol == norm, _account_eq(effective_id))
            .order_by(desc(PositionHoldingSnapshot.id))
            .limit(1)
        )
        if snap is None and matched and effective_id != account_id:
            effective_id = account_id
            snap = self.db.scalar(
                select(PositionHoldingSnapshot)
                .where(PositionHoldingSnapshot.symbol == norm, _account_eq(effective_id))
                .order_by(desc(PositionHoldingSnapshot.id))
                .limit(1)
            )
        if snap is None:
            return {"error": f"未找到 {norm} 的持仓快照"}
        if field in {"stop_price", "take_profit_price", "position_pct"}:
            try:
                coerced: Any = float(value) if value is not None else None
            except (TypeError, ValueError):
                return {"error": f"字段 {field} 需数值: {value!r}"}
        else:
            coerced = str(value).strip() if value is not None else None
        setattr(snap, field, coerced)
        position = self.db.scalar(
            select(Position).where(Position.symbol == norm, _account_eq(effective_id)).order_by(desc(Position.id)).limit(1)
        )
        if position is not None and hasattr(position, field):
            setattr(position, field, coerced)
        self.db.commit()
        return {"symbol": norm, "field": field, "value": coerced, "snapshot_id": snap.id}

    def rollback_last_position_batch(self) -> dict[str, Any]:
        """撤销最近一次持仓导入批次：删其快照行（物理），批次置 rolled_back。

        Position 摘要不自动回滚（保留，用户可后续 update_position_field 手动修正）。
        """
        batch = self.db.scalar(
            select(PositionImportBatch).order_by(desc(PositionImportBatch.id)).limit(1)
        )
        if batch is None:
            return {"error": "无导入批次可撤销"}
        snaps = self.db.scalars(
            select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.import_batch_id == batch.id)
        ).all()
        for snap in snaps:
            self.db.delete(snap)
        batch.status = "rolled_back"
        self.db.commit()
        return {"batch_id": batch.id, "rolled_back_count": len(snaps)}

    def _save_position_holding(
        self,
        payload: dict[str, Any],
        *,
        source_type: str,
        import_batch: PositionImportBatch | None = None,
        commit: bool,
    ) -> dict[str, Any]:
        symbol = normalize_a_share_symbol(str(payload.get("symbol") or payload.get("ts_code") or payload.get("code") or ""))
        symbol_name = str(payload.get("name") or payload.get("symbol_name") or payload.get("stock_name") or "").strip()
        # 名字只含 ?/?? 等问号占位符的值无效（DbVisualizer/mojibake），不更新 instrument.name
        if symbol_name and symbol_name.replace("?", "").strip():
            pass  # valid non-empty name
        else:
            symbol_name = ""
        exchange = str(payload.get("exchange") or exchange_from_symbol(symbol))
        instrument = self.db.get(Instrument, symbol)
        if instrument is None:
            instrument = Instrument(symbol=symbol, name=symbol_name or symbol, market=exchange, type="stock", status="active")
            self.db.add(instrument)
            self.db.flush()
        else:
            # 只在 payload 明确传了名字且与现有不同时才更新 instrument.name；
            # 空名字不再覆盖已有的中文名（防止卖出/减仓不带 name 时把名称清成代码 "?"）。
            if symbol_name and symbol_name != symbol and symbol_name != instrument.name:
                instrument.name = symbol_name
            if exchange:
                instrument.market = exchange
            # 兜底 name：没传名字时沿用 instruments 表已有名字
            if not symbol_name:
                symbol_name = instrument.name or symbol

        account = self._ensure_position_account(payload, symbol=symbol)
        quantity = number_or_none(payload.get("position_quantity") or payload.get("quantity")) or 0.0
        latest_price = number_or_none(payload.get("latest_price") or payload.get("price"))
        if latest_price is None:
            # 兜底 latest_price：查 daily_bars 最新 close（API 录入时自动补行情）
            latest_row = self.db.scalar(
                select(DailyBar.close)
                .where(DailyBar.symbol == symbol, DailyBar.close.isnot(None))
                .order_by(desc(DailyBar.trade_date), desc(DailyBar.id))
                .limit(1)
            )
            if latest_row is not None:
                latest_price = float(latest_row)
        cost_price = number_or_none(payload.get("cost_price") or payload.get("avg_cost"))
        cost_amount = number_or_none(payload.get("cost_amount"))
        market_value = number_or_none(payload.get("market_value"))
        if cost_amount is None and cost_price is not None:
            cost_amount = round(cost_price * quantity, 4)
        if market_value is None and latest_price is not None:
            market_value = round(latest_price * quantity, 4)
        floating_pnl = number_or_none(payload.get("floating_pnl") or payload.get("profit_loss"))
        if floating_pnl is None and market_value is not None and cost_amount is not None:
            floating_pnl = round(market_value - cost_amount, 4)
        floating_pnl_pct = number_or_none(payload.get("floating_pnl_pct") or payload.get("profit_loss_pct"))
        if floating_pnl_pct is None and floating_pnl is not None and cost_amount:
            floating_pnl_pct = round(floating_pnl / cost_amount * 100, 4)
        position_pct = number_or_none(payload.get("position_pct") or payload.get("holding_pct"))
        raw_status = str(payload.get("status") or "").strip().lower()
        status = raw_status if raw_status in {"open", "closed", "active", "inactive"} else ("open" if quantity > 0 else "closed")
        if status == "active":
            status = "open"
        if status == "inactive":
            status = "closed"

        snapshot = PositionHoldingSnapshot(
            account_id=account.id if account else None,
            import_batch_id=import_batch.id if import_batch else None,
            symbol=symbol,
            symbol_name=symbol_name,
            exchange=exchange,
            as_of_date=str(payload.get("as_of_date") or "")[:16] or None,
            position_quantity=quantity,
            available_quantity=number_or_none(payload.get("available_quantity") or payload.get("available")),
            frozen_quantity=number_or_none(payload.get("frozen_quantity") or payload.get("frozen")),
            today_buy_quantity=number_or_none(payload.get("today_buy_quantity")),
            today_sell_quantity=number_or_none(payload.get("today_sell_quantity")),
            cost_price=cost_price,
            cost_amount=cost_amount,
            latest_price=latest_price,
            market_value=market_value,
            floating_pnl=floating_pnl,
            floating_pnl_pct=floating_pnl_pct,
            day_pnl=number_or_none(payload.get("day_pnl")),
            position_pct=position_pct,
            stop_price=number_or_none(payload.get("stop_price")),
            take_profit_price=number_or_none(payload.get("take_profit_price")),
            thesis=payload.get("thesis"),
            risk_note=payload.get("risk_note"),
            source_type=source_type,
            confidence=number_or_none(payload.get("confidence")),
            raw_json=json.dumps(payload, ensure_ascii=False),
            status=status,
        )
        self.db.add(snapshot)

        acc_id = account.id if account else None
        position = self.db.scalar(
            select(Position)
            .where(Position.symbol == symbol, Position.status == "open", _account_eq(acc_id))
            .limit(1)
        )
        if position is None:
            position = self.db.scalar(
                select(Position).where(Position.symbol == symbol, _account_eq(acc_id)).order_by(desc(Position.id)).limit(1)
            )
        if position is None:
            position = Position(symbol=symbol, account_id=acc_id, position_pct=position_pct or 0, status=status, opened_at=datetime.now())
            self.db.add(position)
        position.account_id = acc_id
        position.position_pct = float(position_pct or position.position_pct or 0)
        position.cost_price = cost_price
        position.quantity = quantity
        position.stop_price = snapshot.stop_price
        position.take_profit_price = snapshot.take_profit_price
        position.thesis = snapshot.thesis or position.thesis
        position.risk_note = snapshot.risk_note or position.risk_note
        position.status = status
        if status == "open" and not position.opened_at:
            position.opened_at = datetime.now()
        if status == "open":
            position.closed_at = None
        if status != "open":
            position.closed_at = datetime.now()

        self.db.add(
            SystemLog(
                level="INFO",
                module="positions",
                message="position holding saved",
                context_json=json.dumps({"symbol": symbol, "source_type": source_type, "quantity": quantity}, ensure_ascii=False),
            )
        )
        self.db.flush()
        if account is not None:
            self._recompute_account_funds(account)
        result = self._position_snapshot_out(snapshot, position_id=position.id, account=account)
        if commit:
            self.db.commit()
        return result

    def _unique_open_account_for(self, symbol: str) -> tuple[bool, int | None]:
        """该标的现有 open 持仓归属的账户。恰好一个账户持有 → (True, account_id 可为 None)；否则 (False, None)。"""
        accounts = {
            p.account_id
            for p in self.db.scalars(
                select(Position).where(Position.symbol == symbol, Position.status == "open")
            ).all()
        }
        if len(accounts) == 1:
            return True, next(iter(accounts))
        return False, None

    def _ensure_position_account(self, payload: dict[str, Any], symbol: str | None = None) -> PositionAccount | None:
        # 1) 显式 account_id 直取（前端选定目标账户 / 用户明说时透传）
        acc_id = payload.get("account_id")
        if acc_id:
            existing = self.db.get(PositionAccount, int(acc_id))
            if existing is not None:
                return existing
        # 2) 未给任何账户信息时，自动匹配该标的现有 open 持仓的账户——清仓/卖出/调仓必须命中
        #    原持仓，否则会在默认/manual 账户下另起一条记录，真实持仓纹丝不动（数据"没更新"）。
        has_explicit_account = any(
            payload.get(key)
            for key in ("broker_name", "broker", "broker_code", "account_no_masked", "account_no", "account_name")
        )
        if not has_explicit_account and symbol:
            matched, matched_id = self._unique_open_account_for(symbol)
            if matched:
                if matched_id is None:
                    return None  # 原持仓本就未挂账户，跟随之，不落默认/manual 账户
                existing = self.db.get(PositionAccount, matched_id)
                if existing is not None:
                    return existing
        # 3) 数字人链路注入的默认账户（弱提示）：仅在无法匹配现有持仓时兜底
        fallback_id = payload.get("default_account_id")
        if fallback_id:
            existing = self.db.get(PositionAccount, int(fallback_id))
            if existing is not None:
                return existing
        broker_code = payload.get("broker_code")
        broker_name = str(
            payload.get("broker_name") or payload.get("broker") or broker_name_for(broker_code) or "manual"
        ).strip() or "manual"
        account_type = str(payload.get("account_type") or "cash").strip() or "cash"
        account_no_masked = payload.get("account_no_masked") or payload.get("account_no")
        account_name = payload.get("account_name")
        query = select(PositionAccount).where(PositionAccount.broker_name == broker_name, PositionAccount.account_type == account_type)
        if account_no_masked:
            query = query.where(PositionAccount.account_no_masked == str(account_no_masked))
        else:
            query = query.where(PositionAccount.account_no_masked.is_(None))
        account = self.db.scalar(query.limit(1))
        if account is None:
            account = PositionAccount(
                broker_name=broker_name,
                broker_code=broker_code or broker_code_for_name(broker_name),
                account_name=str(account_name) if account_name else None,
                account_no_masked=str(account_no_masked) if account_no_masked else None,
                account_type=account_type,
                market="A-share",
                currency="CNY",
            )
            self.db.add(account)
            self.db.flush()
        elif account_name:
            account.account_name = str(account_name)
        return account

    # ---- 券商账户与资金 ------------------------------------------------ #
    def _recompute_account_funds(self, account: PositionAccount) -> None:
        """按该账户各标的最新快照重算持仓市值/浮盈/当日盈亏；有总资产时推算可用资金。"""
        latest_ids = (
            select(func.max(PositionHoldingSnapshot.id))
            .where(PositionHoldingSnapshot.account_id == account.id)
            .group_by(PositionHoldingSnapshot.symbol)
        )
        snaps = self.db.scalars(
            select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.id.in_(latest_ids))
        ).all()
        open_snaps = [s for s in snaps if (s.status or "open") == "open" and (s.position_quantity or 0) > 0]
        hmv = round(sum(s.market_value or 0 for s in open_snaps), 4)
        account.holding_market_value = hmv
        account.total_pnl = round(sum(s.floating_pnl or 0 for s in open_snaps), 4)
        account.day_pnl = round(sum(s.day_pnl or 0 for s in open_snaps), 4)
        if account.total_asset is not None:
            account.available_cash = round(account.total_asset - hmv, 4)

    def _account_out(self, account: PositionAccount) -> dict[str, Any]:
        holding_count = (
            self.db.scalar(
                select(func.count(func.distinct(Position.symbol))).where(
                    Position.account_id == account.id, Position.status == "open"
                )
            )
            or 0
        )
        return {
            "id": account.id,
            "broker_code": account.broker_code,
            "broker_name": account.broker_name,
            "account_name": account.account_name,
            "account_no_masked": account.account_no_masked,
            "account_type": account.account_type,
            "currency": account.currency,
            "status": account.status,
            "is_default": bool(account.is_default),
            "sort_order": account.sort_order,
            "total_asset": account.total_asset,
            "available_cash": account.available_cash,
            "holding_market_value": account.holding_market_value,
            "total_pnl": account.total_pnl,
            "day_pnl": account.day_pnl,
            "holding_count": holding_count,
            "fund_updated_at": dt(account.fund_updated_at),
            "remark": account.remark,
            "updated_at": dt(account.updated_at),
        }

    def list_position_accounts(self) -> list[dict[str, Any]]:
        accounts = self.db.scalars(
            select(PositionAccount)
            .where(PositionAccount.status != "archived")
            .order_by(PositionAccount.sort_order, PositionAccount.id)
        ).all()
        for account in accounts:
            self._recompute_account_funds(account)
        self.db.commit()
        return [self._account_out(account) for account in accounts]

    def create_position_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        broker_code = str(payload.get("broker_code") or "manual").strip() or "manual"
        broker_name = str(payload.get("broker_name") or broker_name_for(broker_code) or "其他券商").strip()
        account_type = str(payload.get("account_type") or "cash").strip() or "cash"
        account_no = payload.get("account_no_masked")
        query = select(PositionAccount).where(
            PositionAccount.broker_name == broker_name, PositionAccount.account_type == account_type
        )
        query = query.where(
            PositionAccount.account_no_masked == str(account_no) if account_no else PositionAccount.account_no_masked.is_(None)
        )
        existing = self.db.scalar(query.limit(1))
        if existing is not None:
            existing.status = "active"
            existing.broker_code = broker_code
            if payload.get("account_name"):
                existing.account_name = str(payload["account_name"])
            self.db.commit()
            return self._account_out(existing)
        if payload.get("is_default"):
            self.db.execute(update(PositionAccount).values(is_default=False))
        account = PositionAccount(
            broker_code=broker_code,
            broker_name=broker_name,
            account_name=str(payload["account_name"]) if payload.get("account_name") else None,
            account_no_masked=str(account_no) if account_no else None,
            account_type=account_type,
            currency=str(payload.get("currency") or "CNY"),
            is_default=bool(payload.get("is_default")),
            remark=payload.get("remark"),
        )
        self.db.add(account)
        self.db.commit()
        return self._account_out(account)

    def update_position_account(self, account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        account = self.db.get(PositionAccount, account_id)
        if account is None:
            raise ValueError("账户不存在")
        for field in ("account_name", "account_no_masked", "account_type", "status", "remark", "sort_order"):
            if payload.get(field) is not None:
                setattr(account, field, payload[field])
        if payload.get("is_default"):
            self.db.execute(update(PositionAccount).where(PositionAccount.id != account_id).values(is_default=False))
            account.is_default = True
        self.db.commit()
        return self._account_out(account)

    def update_account_funds(self, account_id: int, total_asset: float | None) -> dict[str, Any]:
        account = self.db.get(PositionAccount, account_id)
        if account is None:
            raise ValueError("账户不存在")
        account.total_asset = float(total_asset) if total_asset is not None else None
        self._recompute_account_funds(account)
        account.fund_updated_at = datetime.now()
        self.db.add(
            SystemLog(
                level="INFO",
                module="positions",
                message="account funds updated",
                context_json=json.dumps({"account_id": account_id, "total_asset": account.total_asset}, ensure_ascii=False),
            )
        )
        self.db.commit()
        return self._account_out(account)

    def positions_summary(self, account_id: int | None = None) -> dict[str, Any]:
        query = select(PositionAccount).where(PositionAccount.status != "archived")
        if account_id is not None:
            query = query.where(PositionAccount.id == account_id)
        accounts = self.db.scalars(query).all()
        for account in accounts:
            self._recompute_account_funds(account)
        self.db.commit()
        count_query = select(func.count(func.distinct(Position.symbol))).where(Position.status == "open")
        if account_id is not None:
            count_query = count_query.where(Position.account_id == account_id)
        return {
            "account_count": len(accounts),
            "total_asset": round(sum(a.total_asset or 0 for a in accounts), 2),
            "available_cash": round(sum(a.available_cash or 0 for a in accounts), 2),
            "holding_market_value": round(sum(a.holding_market_value or 0 for a in accounts), 2),
            "total_pnl": round(sum(a.total_pnl or 0 for a in accounts), 2),
            "day_pnl": round(sum(a.day_pnl or 0 for a in accounts), 2),
            "holding_count": self.db.scalar(count_query) or 0,
        }

    def _position_snapshot_out(
        self,
        snapshot: PositionHoldingSnapshot,
        *,
        position_id: int | None = None,
        account: PositionAccount | None = None,
    ) -> dict[str, Any]:
        return {
            "id": position_id or snapshot.id,
            "snapshot_id": snapshot.id,
            "symbol": snapshot.symbol,
            "name": snapshot.symbol_name,
            "position_pct": snapshot.position_pct,
            "cost_price": snapshot.cost_price,
            "quantity": snapshot.position_quantity,
            "position_quantity": snapshot.position_quantity,
            "available_quantity": snapshot.available_quantity,
            "frozen_quantity": snapshot.frozen_quantity,
            "today_buy_quantity": snapshot.today_buy_quantity,
            "today_sell_quantity": snapshot.today_sell_quantity,
            "cost_amount": snapshot.cost_amount,
            "latest_price": snapshot.latest_price,
            "market_value": snapshot.market_value,
            "floating_pnl": snapshot.floating_pnl,
            "floating_pnl_pct": snapshot.floating_pnl_pct,
            "day_pnl": snapshot.day_pnl,
            "stop_price": snapshot.stop_price,
            "take_profit_price": snapshot.take_profit_price,
            "thesis": snapshot.thesis,
            "risk_note": snapshot.risk_note,
            "status": snapshot.status,
            "opened_at": None,
            "updated_at": dt(snapshot.captured_at),
            "as_of_date": snapshot.as_of_date,
            "source_type": snapshot.source_type,
            "confidence": snapshot.confidence,
            "account_id": account.id if account else None,
            "broker_name": account.broker_name if account else None,
            "broker_code": account.broker_code if account else None,
            "account_name": account.account_name if account else None,
            "account_no_masked": account.account_no_masked if account else None,
        }

    def watchlist(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(WatchlistItem, Instrument.name, WatchlistGroup.name.label("group_name"))
            .join(Instrument, Instrument.symbol == WatchlistItem.symbol)
            .join(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
            .order_by(WatchlistItem.status, desc(WatchlistItem.updated_at))
        ).all()
        return [
            {
                "id": item.id,
                "symbol": item.symbol,
                "name": instrument_name,
                "group_name": group_name,
                "reason": item.reason,
                "status": item.status,
                "added_at": dt(item.added_at),
                "updated_at": dt(item.updated_at),
            }
            for item, instrument_name, group_name in rows
        ]

    def add_watchlist_item(self, symbol: str, group_name: str, reason: str | None) -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        normalized_group = group_name.strip() or "默认"
        if self.db.get(Instrument, normalized_symbol) is None:
            raise ValueError(f"unknown instrument: {normalized_symbol}")

        group = self.db.scalar(select(WatchlistGroup).where(WatchlistGroup.name == normalized_group))
        if group is None:
            group = WatchlistGroup(name=normalized_group, description="用户创建的自选池分组")
            self.db.add(group)
            self.db.flush()

        item = self.db.scalar(
            select(WatchlistItem).where(WatchlistItem.symbol == normalized_symbol, WatchlistItem.group_id == group.id)
        )
        if item is None:
            self.db.add(WatchlistItem(symbol=normalized_symbol, group_id=group.id, reason=reason, status="active"))
        else:
            item.reason = reason
            item.status = "active"
            item.removed_at = None

        self.db.add(
            SystemLog(
                level="INFO",
                module="watchlist",
                message="watchlist item upserted",
                context_json=json.dumps({"symbol": normalized_symbol, "group": normalized_group}, ensure_ascii=False),
            )
        )
        self.db.commit()
        return {"symbol": normalized_symbol, "group_name": normalized_group, "reason": reason}

    def agents(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(AgentProfile)).all()
        counts = dict(self.db.execute(select(AgentReport.agent_name, func.count(AgentReport.id)).group_by(AgentReport.agent_name)).all())
        pending = dict(
            self.db.execute(
                select(AgentTask.agent_name, func.count(AgentTask.id)).where(AgentTask.status == "pending").group_by(AgentTask.agent_name)
            ).all()
        )
        running = dict(
            self.db.execute(
                select(AgentTask.agent_name, func.count(AgentTask.id)).where(AgentTask.status == "running").group_by(AgentTask.agent_name)
            ).all()
        )
        order = {"pm": 0, "sentinel": 1, "intelligence": 2, "risk": 3, "researcher": 4}
        rows = sorted(rows, key=lambda item: (not item.enabled, order.get(item.agent_name, 9), item.agent_name))
        return [
            {
                "agent_name": item.agent_name,
                "display_name": item.display_name,
                "enabled": item.enabled,
                "role_description": item.role_description,
                "model_primary": item.model_primary,
                "model_backup": item.model_backup,
                "pending_count": pending.get(item.agent_name, 0),
                "running_count": running.get(item.agent_name, 0),
                "report_count": counts.get(item.agent_name, 0),
                "last_update": dt(item.updated_at),
                "status": "running" if running.get(item.agent_name, 0) else ("ready" if item.enabled else "disabled"),
            }
            for item in rows
        ]

    def reports(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(AgentReport, AgentProfile.display_name, Instrument.name.label("symbol_name"))
            .outerjoin(AgentProfile, AgentProfile.agent_name == AgentReport.agent_name)
            .outerjoin(Instrument, Instrument.symbol == AgentReport.symbol)
            .order_by(desc(AgentReport.created_at))
            .limit(limit)
        ).all()
        return [
            {
                "report_id": item.report_id,
                "agent_name": item.agent_name,
                "display_name": display_name,
                "symbol": item.symbol,
                "symbol_name": symbol_name,
                "title": item.title,
                "conclusion": item.conclusion,
                "risk_points": item.risk_points,
                "action_suggestion": item.action_suggestion,
                "model_name": item.model_name,
                "created_at": dt(item.created_at),
            }
            for item, display_name, symbol_name in rows
        ]

    def risk_checks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(RiskCheck, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == RiskCheck.symbol)
            .order_by(desc(RiskCheck.created_at))
            .limit(limit)
        ).all()
        return [
            {
                "id": item.id,
                "symbol": item.symbol,
                "symbol_name": symbol_name,
                "risk_level": item.risk_level,
                "risk_type": item.risk_type,
                "finding": item.finding,
                "required_action": item.required_action,
                "position_limit": item.position_limit,
                "stop_condition": item.stop_condition,
                "cool_down_required": int(item.cool_down_required),
                "created_at": dt(item.created_at),
            }
            for item, symbol_name in rows
        ]

    def system_logs(
        self, limit: int = 100, module: str | None = None, level: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(SystemLog)
        if module:
            query = query.where(SystemLog.module == module)
        if level:
            query = query.where(SystemLog.level == level.upper())
        rows = self.db.scalars(
            query.order_by(desc(SystemLog.created_at), desc(SystemLog.id)).limit(limit)
        ).all()
        return [system_log_out(row) for row in rows]

    def system_log_modules(self) -> list[str]:
        rows = self.db.scalars(select(SystemLog.module).distinct()).all()
        return sorted({str(r) for r in rows if r})

    def _event_out(self, event: Event, symbol_name: str | None) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "level": event.level,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "symbol_name": symbol_name,
            "title": event.title,
            "summary": event.summary,
            "facts": parse_json(event.facts_json),
            "source_type": event.source_type,
            "status": event.status,
            "created_at": dt(event.created_at),
            "updated_at": dt(event.updated_at),
        }
