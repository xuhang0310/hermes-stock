"""持仓写入域 mixin：保存/合并/导入/删除持仓及流水。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select, update

from backend.app.core.models import (
    DailyBar,
    Instrument,
    Position,
    PositionAccount,
    PositionHoldingSnapshot,
    PositionImportBatch,
    PositionTrade,
    SystemLog,
)
from backend.app.data._helpers import _account_eq, dt, exchange_from_symbol, normalize_a_share_symbol, number_or_none


class PositionWriteMixin:
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
                .where(
                    Position.symbol == norm,
                    Position.status == "open",
                    _account_eq(acc_id),
                    self._owner_clause(Position),
                )
                .order_by(desc(Position.id))
                .limit(1)
            )
            if position is None:
                position = self.db.scalar(
                    select(Position)
                    .where(
                        Position.symbol == norm,
                        _account_eq(acc_id),
                        self._owner_clause(Position),
                    )
                    .order_by(desc(Position.id))
                    .limit(1)
                )
            if position is not None:
                # 已关仓的持仓 → 幂等返回，不重复关仓/记流水
                if position.status == "closed":
                    return {"symbol": norm, "action": "close", "closed": False, "note": "持仓已关仓"}
                close_qty = position.quantity or 0
                execution_price = number_or_none(payload.get("execution_price"))
                position.status = "closed"
                position.quantity = 0
                position.closed_at = datetime.now()
                # Closing must append a zero-quantity snapshot; otherwise reads
                # and account totals keep using the previous open snapshot.
                self._save_position_holding(
                    {
                        **payload,
                        "symbol": norm,
                        "account_id": acc_id,
                        "position_quantity": 0,
                        "cost_price": position.cost_price,
                        "status": "closed",
                    },
                    source_type=source_type,
                    commit=False,
                )
                self._record_trade(
                    account_id=acc_id,
                    position_id=position.id,
                    symbol=norm,
                    name=str(payload.get("name") or "").strip(),
                    action="close",
                    quantity=close_qty,
                    price=execution_price,
                    source_type=source_type,
                )
                self.db.flush()
                if commit:
                    self.db.commit()
                return {"symbol": norm, "action": "close", "closed": True, "position_id": position.id}
            # 没有持仓也返回成功（幂等）
            return {"symbol": norm, "action": "close", "closed": False, "note": "无 open 持仓可关闭"}

        # ---- 需要合并的买方/卖方动作 ----
        new_qty_raw = number_or_none(payload.get("position_quantity"))
        if new_qty_raw is None:
            new_qty_raw = number_or_none(payload.get("quantity"))
        if new_qty_raw is None or new_qty_raw <= 0:
            raise ValueError("trade quantity must be greater than zero")
        new_cost_price = number_or_none(payload.get("cost_price"))
        execution_price = number_or_none(payload.get("execution_price"))

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
                .where(
                    Position.symbol == norm,
                    Position.status == "open",
                    _account_eq(acc_id),
                    self._owner_clause(Position),
                )
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
            self._record_trade(
                account_id=acc_id,
                symbol=norm,
                name=str(payload.get("name") or "").strip(),
                action=action,
                quantity=new_qty_raw,
                price=execution_price,
                source_type=source_type,
            )
            saved = self._save_position_holding(payload, source_type=source_type, commit=commit)

            # 买入/加仓 → 自动加入自选
            symbol_name = str(payload.get("name") or "").strip()
            try:
                self.add_watchlist_item(
                    symbol=norm,
                    group_name="默认",
                    reason=f"持仓买入 - {symbol_name}" if symbol_name else "持仓买入",
                    commit=commit,
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
                .where(
                    Position.symbol == norm,
                    Position.status == "open",
                    _account_eq(acc_id),
                    self._owner_clause(Position),
                )
                .order_by(desc(Position.id))
                .limit(1)
            )
            if position is not None and position.quantity is not None and position.quantity > 0:
                if new_qty_raw > position.quantity:
                    raise ValueError(
                        f"sell quantity {new_qty_raw:g} exceeds current holding {position.quantity:g} for {norm}"
                    )
                remaining = max(0.0, position.quantity - new_qty_raw)
                payload["position_quantity"] = remaining
                payload["status"] = "closed" if remaining == 0 else "open"
                # 卖出时不重复传成本价就沿用旧成本，避免覆盖成 None
                if "cost_price" not in payload or not payload["cost_price"]:
                    payload["cost_price"] = position.cost_price
            else:
                # 无持仓可卖
                return {"error": f"卖出失败: {norm} 无可卖出持仓", "symbol": norm, "action": action}

            self._record_trade(
                account_id=acc_id,
                position_id=position.id,
                symbol=norm,
                name=str(payload.get("name") or "").strip(),
                action=action,
                quantity=new_qty_raw,
                price=execution_price,
                source_type=source_type,
            )
            saved = self._save_position_holding(payload, source_type=source_type, commit=commit)
            return saved

        # 未知 action → 退化到覆盖
        raise ValueError(f"unsupported position action: {action}")

    def _record_trade(
        self,
        *,
        account_id: int | None,
        symbol: str,
        name: str | None,
        action: str,
        quantity: float | None,
        price: float | None,
        source_type: str,
        position_id: int | None = None,
        created_by: str | None = None,
    ) -> None:
        """记一条买卖流水。只 add 不 commit，跟随调用方（_save_position_holding 或批量 finish）的 commit。"""
        qty = number_or_none(quantity) or 0.0
        pr = number_or_none(price)
        self.db.add(
            PositionTrade(
                owner_user_id=self.owner_user_id,
                account_id=account_id,
                position_id=position_id,
                symbol=symbol,
                name=(name or "").strip() or None,
                action=action,
                quantity=qty,
                price=pr,
                amount=round(qty * pr, 4) if pr is not None else None,
                source_type=source_type,
                created_by=created_by,
            )
        )

    def position_trades(
        self, symbol: str | None = None, account_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """买卖流水查询，按时间倒序。"""
        query = (
            select(PositionTrade, PositionAccount, Instrument)
            .outerjoin(PositionAccount, PositionAccount.id == PositionTrade.account_id)
            .outerjoin(Instrument, Instrument.symbol == PositionTrade.symbol)
            .where(self._owner_clause(PositionTrade))
            .order_by(desc(PositionTrade.created_at), desc(PositionTrade.id))
            .limit(limit)
        )
        if symbol is not None:
            query = query.where(PositionTrade.symbol == symbol)
        if account_id is not None:
            query = query.where(PositionTrade.account_id == account_id)
        rows = self.db.execute(query).all()
        result: list[dict[str, Any]] = []
        for trade, account, instrument in rows:
            result.append(
                {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "name": trade.name or (instrument.name if instrument else None) or trade.symbol,
                    "action": trade.action,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "amount": trade.amount,
                    "account_id": trade.account_id,
                    "broker_name": account.broker_name if account else None,
                    "source_type": trade.source_type,
                    "created_by": trade.created_by,
                    "position_id": trade.position_id,
                    "created_at": dt(trade.created_at),
                }
            )
        return result

    def delete_position_trade(self, trade_id: int) -> dict[str, Any]:
        """删单条流水（不回滚持仓，仅删流水行）。"""
        trade = self.db.get(PositionTrade, trade_id)
        if trade is None or not self._owns(trade):
            return {"deleted": False, "id": trade_id}
        self.db.add(
            SystemLog(
                level="WARNING",
                module="positions",
                message="position trade deleted manually",
                context_json=json.dumps(
                    {"id": trade.id, "symbol": trade.symbol, "action": trade.action, "quantity": trade.quantity},
                    ensure_ascii=False,
                ),
            )
        )
        self.db.delete(trade)
        self.db.commit()
        return {"deleted": True, "id": trade_id}

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
            owner_user_id=self.owner_user_id,
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

    def finish_position_import_batch(
        self, batch: PositionImportBatch, saved_rows: list[dict[str, Any]], error: str | None = None, *, commit: bool = True
    ) -> None:
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
        if commit:
            self.db.commit()
        else:
            self.db.flush()

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
        if account_id is not None:
            effective_id = account_id
        elif matched:
            effective_id = matched_id
        else:
            return {"error": f"multiple or no open accounts for {norm}; specify account_id"}
        snap = self.db.scalar(
            select(PositionHoldingSnapshot)
            .where(
                PositionHoldingSnapshot.symbol == norm,
                _account_eq(effective_id, PositionHoldingSnapshot),
                self._owner_clause(PositionHoldingSnapshot),
            )
            .order_by(desc(PositionHoldingSnapshot.as_of_date), desc(PositionHoldingSnapshot.captured_at), desc(PositionHoldingSnapshot.id))
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
            select(Position)
            .where(
                Position.symbol == norm,
                _account_eq(effective_id),
                self._owner_clause(Position),
            )
            .order_by(desc(Position.id))
            .limit(1)
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
            select(PositionImportBatch)
            .where(
                PositionImportBatch.status.in_(("done", "failed")),
                self._owner_clause(PositionImportBatch),
            )
            .order_by(desc(PositionImportBatch.id))
            .limit(1)
        )
        if batch is None:
            return {"error": "无导入批次可撤销"}
        snaps = self.db.scalars(
            select(PositionHoldingSnapshot).where(
                PositionHoldingSnapshot.import_batch_id == batch.id,
                self._owner_clause(PositionHoldingSnapshot),
            )
        ).all()
        affected_accounts = {snap.account_id for snap in snaps}
        affected_pairs = {(snap.symbol, snap.account_id) for snap in snaps}
        for snap in snaps:
            self.db.delete(snap)
        batch.status = "rolled_back"
        self.db.flush()
        for symbol, account_id in affected_pairs:
            latest = self.db.scalar(
                select(PositionHoldingSnapshot)
                .where(
                    PositionHoldingSnapshot.symbol == symbol,
                    PositionHoldingSnapshot.account_id == account_id,
                    self._owner_clause(PositionHoldingSnapshot),
                )
                .order_by(desc(PositionHoldingSnapshot.as_of_date), desc(PositionHoldingSnapshot.captured_at), desc(PositionHoldingSnapshot.id))
                .limit(1)
            )
            positions = self.db.scalars(
                select(Position).where(
                    Position.symbol == symbol,
                    _account_eq(account_id),
                    self._owner_clause(Position),
                )
            ).all()
            if latest is None:
                for position in positions:
                    self.db.delete(position)
                continue
            position = positions[0] if positions else Position(
                owner_user_id=self.owner_user_id,
                symbol=symbol,
                account_id=account_id,
                position_pct=latest.position_pct or 0,
                status=latest.status,
            )
            if not positions:
                self.db.add(position)
            position.position_pct = latest.position_pct or 0
            position.cost_price = latest.cost_price
            position.quantity = latest.position_quantity
            position.stop_price = latest.stop_price
            position.take_profit_price = latest.take_profit_price
            position.thesis = latest.thesis
            position.risk_note = latest.risk_note
            position.status = latest.status
            position.closed_at = datetime.now() if latest.status != "open" else None
        for account_id in affected_accounts:
            if account_id is not None:
                account = self.db.get(PositionAccount, account_id)
                if account is not None and self._owns(account):
                    self._recompute_account_funds(account)
        self.db.commit()
        return {"batch_id": batch.id, "rolled_back_count": len(snaps)}

    def delete_position(self, symbol: str, account_id: int | None = None) -> dict[str, Any]:
        """物理删除一条持仓记录及其全部历史快照（不可逆，留审计日志）。

        与 sell/close 的区别：sell/close 是逻辑关闭（status=closed，留审计快照）；
        delete_position 直接从库里删掉 Position 行 + 该 symbol+account 的所有
        PositionHoldingSnapshot 行，用于"录错了想删掉重来"的场景。instrument
        基础表不删（可能被自选/其他持仓引用）。删后重算账户资金。
        """
        norm = normalize_a_share_symbol(symbol)
        # 解析账号：优先传入，其次按标的自动匹配
        if account_id is not None:
            acc_id: int | None = int(account_id)
        else:
            _, acc_id = self._unique_open_account_for(norm)

        # 删该 symbol+account 的所有 Position 行
        positions = self.db.scalars(
            select(Position).where(
                Position.symbol == norm,
                _account_eq(acc_id),
                self._owner_clause(Position),
            )
        ).all()
        if not positions:
            return {"error": f"未找到 {norm} 的持仓记录", "symbol": norm}
        pos_count = len(positions)
        # 断开交易流水对该持仓的外键引用（流水作为历史保留，不随持仓物理删除）
        pos_ids = [p.id for p in positions]
        if pos_ids:
            self.db.execute(
                update(PositionTrade).where(PositionTrade.position_id.in_(pos_ids)).values(position_id=None)
            )
        for pos in positions:
            self.db.delete(pos)

        # 删该 symbol+account 的所有快照
        snaps = self.db.scalars(
            select(PositionHoldingSnapshot).where(
                PositionHoldingSnapshot.symbol == norm,
                _account_eq(acc_id, PositionHoldingSnapshot),
                self._owner_clause(PositionHoldingSnapshot),
            )
        ).all()
        snap_count = len(snaps)
        for snap in snaps:
            self.db.delete(snap)

        self.db.add(
            SystemLog(
                level="WARNING",
                module="positions",
                message="position physically deleted",
                context_json=json.dumps(
                    {"symbol": norm, "account_id": acc_id, "positions": pos_count, "snapshots": snap_count},
                    ensure_ascii=False,
                ),
            )
        )
        self.db.flush()
        # 重算受影响账户资金
        if acc_id is not None:
            account = self.db.get(PositionAccount, acc_id)
            if account is not None and self._owns(account):
                self._recompute_account_funds(account)
        self.db.commit()
        return {
            "symbol": norm,
            "account_id": acc_id,
            "deleted_positions": pos_count,
            "deleted_snapshots": snap_count,
        }

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
        quantity = number_or_none(payload.get("position_quantity"))
        if quantity is None:
            quantity = number_or_none(payload.get("quantity")) or 0.0
        latest_price = number_or_none(payload.get("latest_price"))
        if latest_price is None:
            latest_price = number_or_none(payload.get("price"))
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
            owner_user_id=self.owner_user_id,
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
        # Flush before choosing the authoritative snapshot. Imports can arrive
        # out of chronological order, so the just-created row is not
        # necessarily the position state that should drive later trades.
        self.db.flush()

        acc_id = account.id if account else None
        position = self.db.scalar(
            select(Position)
            .where(
                Position.symbol == symbol,
                Position.status == "open",
                _account_eq(acc_id),
                self._owner_clause(Position),
            )
            .limit(1)
        )
        if position is None:
            position = self.db.scalar(
                select(Position)
                .where(
                    Position.symbol == symbol,
                    _account_eq(acc_id),
                    self._owner_clause(Position),
                )
                .order_by(desc(Position.id))
                .limit(1)
            )
        latest_snapshot = self.db.scalar(
            select(PositionHoldingSnapshot)
            .where(
                PositionHoldingSnapshot.symbol == symbol,
                _account_eq(acc_id, PositionHoldingSnapshot),
                self._owner_clause(PositionHoldingSnapshot),
            )
            .order_by(
                desc(PositionHoldingSnapshot.as_of_date),
                desc(PositionHoldingSnapshot.captured_at),
                desc(PositionHoldingSnapshot.id),
            )
            .limit(1)
        ) or snapshot
        if position is None:
            position = Position(
                owner_user_id=self.owner_user_id,
                symbol=symbol,
                account_id=acc_id,
                position_pct=latest_snapshot.position_pct or 0,
                status=latest_snapshot.status,
                opened_at=datetime.now(),
            )
            self.db.add(position)
        position.account_id = acc_id
        position.position_pct = float(latest_snapshot.position_pct or position.position_pct or 0)
        position.cost_price = latest_snapshot.cost_price
        position.quantity = latest_snapshot.position_quantity
        position.stop_price = latest_snapshot.stop_price
        position.take_profit_price = latest_snapshot.take_profit_price
        position.thesis = latest_snapshot.thesis or position.thesis
        position.risk_note = latest_snapshot.risk_note or position.risk_note
        position.status = latest_snapshot.status
        if latest_snapshot.status == "open" and not position.opened_at:
            position.opened_at = datetime.now()
        if latest_snapshot.status == "open":
            position.closed_at = None
        if latest_snapshot.status != "open":
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
