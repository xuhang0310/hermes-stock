"""持仓同步向导的写库部分：变化清单、确认同步、撤销同步。

识别与合并在 data/position_sync.py；这里只和数据库打交道。
同步是“以截图为准的校正”：写持仓快照（source_type=screenshot_sync），不写交易流水；
只有用户勾选“这是一笔买入/卖出”的行才记成交。撤销时删掉本批快照、恢复同步前的资金。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from backend.app.core.models import (
    PositionAccount,
    PositionHoldingSnapshot,
    PositionImportBatch,
    PositionTrade,
    SystemLog,
)
from backend.app.data._helpers import dt
from backend.app.data.errors import PositionNotFoundError

SYNC_SOURCE_TYPE = "screenshot_sync"
# 截图市值合计和账户持仓市值相差超过这个比例，就当截图没截全，截图里没有的股票默认不清仓
_COMPLETENESS_TOLERANCE = 0.03


def _fmt_qty(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "-"


def _fmt_price(value: float | None) -> str:
    return f"{value:,.3f}".rstrip("0").rstrip(".") if value is not None else "-"


class PositionSyncMixin:
    # ------------------------------------------------------------------ 变化清单
    def _owned_account(self, account_id: int) -> PositionAccount:
        account = self.db.get(PositionAccount, account_id)
        if account is None or not self._owns(account) or account.status == "archived":
            raise PositionNotFoundError("账户不存在")
        return account

    def position_sync_preview(
        self,
        *,
        account_id: int,
        rows: list[dict[str, Any]],
        funds: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """把截图识别结果和账户当前持仓对比，列出新增 / 有变化 / 截图里没有 / 没变化 / 代码待确认。"""
        account = self._owned_account(account_id)
        current = {item["symbol"]: item for item in self.positions(account_id=account_id)}
        funds = funds or {}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        screenshot_mv = 0.0
        for row in rows:
            name = str(row.get("name") or row.get("symbol") or "").strip()
            quantity = float(row.get("position_quantity") or 0)
            if not row.get("valid"):
                items.append(
                    {
                        "key": row.get("key") or f"name:{name}",
                        "symbol": row.get("symbol") or "",
                        "name": name,
                        "kind": "invalid",
                        "apply": False,
                        "before": None,
                        "after": row,
                        "changes": [],
                        "note": row.get("validation_message") or "代码没通过校验，请改正后再同步。",
                        "trade_hint": None,
                    }
                )
                continue
            symbol = str(row["symbol"])
            seen.add(symbol)
            market_value = row.get("market_value")
            if market_value is None and row.get("latest_price") is not None:
                market_value = float(row["latest_price"]) * quantity
            screenshot_mv += float(market_value or 0)
            before_row = current.get(symbol)
            note = row.get("validation_message") if row.get("corrected") else None
            if before_row is None:
                if quantity <= 0:
                    continue
                items.append(
                    {
                        "key": symbol,
                        "symbol": symbol,
                        "name": name,
                        "kind": "new",
                        "apply": True,
                        "before": None,
                        "after": row,
                        "changes": [f"{_fmt_qty(quantity)} 股，成本 {_fmt_price(row.get('cost_price'))}"],
                        "note": note,
                        "trade_hint": {"action": "buy", "quantity": quantity, "price": row.get("latest_price")},
                    }
                )
                continue
            before = {
                "quantity": before_row.get("quantity"),
                "cost_price": before_row.get("cost_price"),
                "latest_price": before_row.get("latest_price"),
                "market_value": before_row.get("market_value"),
            }
            old_qty = float(before["quantity"] or 0)
            if quantity <= 0:
                seen.discard(symbol)  # 截图上数量为 0 等同于截图里没有，交给下面按“清仓”处理
                continue
            changes: list[str] = []
            if abs(quantity - old_qty) >= 0.5:
                changes.append(f"数量 {_fmt_qty(old_qty)} → {_fmt_qty(quantity)} 股")
            new_cost = row.get("cost_price")
            old_cost = before["cost_price"]
            if new_cost is not None and (old_cost is None or abs(float(new_cost) - float(old_cost)) >= 0.001):
                changes.append(f"成本 {_fmt_price(old_cost)} → {_fmt_price(new_cost)}")
            diff = quantity - old_qty
            items.append(
                {
                    "key": symbol,
                    "symbol": symbol,
                    "name": name,
                    "kind": "changed" if changes else "unchanged",
                    "apply": bool(changes),
                    "before": before,
                    "after": row,
                    "changes": changes,
                    "note": note,
                    "trade_hint": (
                        {
                            "action": "buy" if diff > 0 else "sell",
                            "quantity": abs(diff),
                            "price": row.get("latest_price") or before["latest_price"],
                        }
                        if abs(diff) >= 0.5
                        else None
                    ),
                }
            )

        holding_mv = funds.get("holding_market_value")
        complete: bool | None = None
        if holding_mv and screenshot_mv:
            complete = abs(screenshot_mv - float(holding_mv)) / float(holding_mv) <= _COMPLETENESS_TOLERANCE
        warnings: list[str] = []
        missing = [item for symbol, item in current.items() if symbol not in seen]
        if missing and complete is False:
            warnings.append(
                f"截图里的持仓市值合计约 {screenshot_mv:,.0f} 元，和截图上的持仓市值 {float(holding_mv):,.0f} 元对不上，"
                "可能没截全，所以截图里没有的股票默认不清仓。确认截全了再勾选。"
            )
        for before_row in missing:
            items.append(
                {
                    "key": before_row["symbol"],
                    "symbol": before_row["symbol"],
                    "name": before_row.get("name") or before_row["symbol"],
                    "kind": "missing",
                    "apply": complete is not False,
                    "before": {
                        "quantity": before_row.get("quantity"),
                        "cost_price": before_row.get("cost_price"),
                        "latest_price": before_row.get("latest_price"),
                        "market_value": before_row.get("market_value"),
                    },
                    "after": None,
                    "changes": [f"系统里有 {_fmt_qty(before_row.get('quantity'))} 股，截图里没有"],
                    "note": None,
                    "trade_hint": {
                        "action": "sell",
                        "quantity": before_row.get("quantity"),
                        "price": before_row.get("latest_price"),
                    },
                }
            )

        counts = {kind: 0 for kind in ("new", "changed", "missing", "unchanged", "invalid")}
        for item in items:
            counts[item["kind"]] += 1
        funds_after = {key: funds.get(key) for key in ("total_asset", "available_cash", "holding_market_value")}
        return {
            "account_id": account.id,
            "account_label": account.broker_name + (f" · {account.account_name}" if account.account_name else ""),
            "items": items,
            "counts": counts,
            "funds_before": {
                "available_cash": account.available_cash,
                "total_asset": account.total_asset,
                "holding_market_value": account.holding_market_value,
                "fund_updated_at": dt(account.fund_updated_at),
            },
            "funds_after": funds_after if any(value is not None for value in funds_after.values()) else None,
            "apply_funds_default": funds.get("available_cash") is not None,
            "completeness": {
                "screenshot_market_value": round(screenshot_mv, 2),
                "holding_market_value": holding_mv,
                "complete": complete,
            },
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ 确认同步
    def apply_position_sync(
        self,
        *,
        account_id: int,
        items: list[dict[str, Any]],
        funds: dict[str, Any] | None,
        as_of_date: str,
        image_sha256: str | None,
        account_no_masked: str | None,
        created_by: str | None,
        screenshot_date: str | None = None,
    ) -> dict[str, Any]:
        """一次事务写完：持仓快照（校正）、勾选的成交、可用资金；记下撤销需要的信息。"""
        account = self._owned_account(account_id)
        snapshot_watermark = self.db.scalar(select(func.max(PositionHoldingSnapshot.id))) or 0
        trade_watermark = self.db.scalar(select(func.max(PositionTrade.id))) or 0
        before_funds = {
            "available_cash": account.available_cash,
            "fund_updated_at": account.fund_updated_at.isoformat() if account.fund_updated_at else None,
        }
        batch = PositionImportBatch(
            owner_user_id=self.owner_user_id,
            source_type=SYNC_SOURCE_TYPE,
            source_name="持仓同步",
            account_id=account.id,
            status="pending",
            image_mime_type=None,
            image_sha256=image_sha256,
            created_by=created_by,
        )
        self.db.add(batch)
        self.db.flush()

        counts = {"added": 0, "updated": 0, "closed": 0, "trades": 0}
        trade_ids: list[int] = []
        warnings: list[str] = []
        settle_cash = funds is None  # 截图资金为准时，成交不再重复动可用资金
        for item in items:
            symbol = item["symbol"]
            previous = self._latest_open_snapshot(symbol, account.id)
            if item["kind"] in ("new", "changed"):
                after = item["after"]
                payload: dict[str, Any] = {
                    "account_id": account.id,
                    "symbol": symbol,
                    "name": item["name"],
                    "as_of_date": as_of_date,
                    "status": "open",
                    **{key: value for key, value in after.items() if value is not None},
                }
                if payload.get("cost_price") is None and previous is not None:
                    payload["cost_price"] = previous.cost_price
                # 止损止盈、持仓理由沿用旧快照，同步不能把它们清空
                self._carry_forward_holding_fields(payload, previous)
                self._save_position_holding(payload, source_type=SYNC_SOURCE_TYPE, import_batch=batch, commit=False)
                counts["added" if item["kind"] == "new" else "updated"] += 1
            elif item["kind"] == "missing":
                if previous is None:
                    continue
                self._save_position_holding(
                    {
                        "account_id": account.id,
                        "symbol": symbol,
                        "name": item["name"],
                        "as_of_date": as_of_date,
                        "position_quantity": 0,
                        "cost_price": previous.cost_price,
                        "status": "closed",
                    },
                    source_type=SYNC_SOURCE_TYPE,
                    import_batch=batch,
                    commit=False,
                )
                counts["closed"] += 1
            trade = item.get("trade")
            if trade:
                record = self._record_trade(
                    account_id=account.id,
                    symbol=symbol,
                    name=item["name"],
                    action=trade["action"],
                    quantity=trade["quantity"],
                    price=trade["price"],
                    source_type=SYNC_SOURCE_TYPE,
                    created_by=created_by,
                )
                self.db.flush()
                trade_ids.append(record.id)
                counts["trades"] += 1
                if settle_cash:
                    amount = float(trade["quantity"]) * float(trade["price"])
                    try:
                        self._settle_trade_cash(account.id, amount if trade["action"] == "sell" else -amount)
                    except ValueError:
                        warnings.append(f"{item['name']}这笔买入超过了系统里记的可用资金，可用资金没有扣减，请在“维护资金”里核对。")

        if funds is not None:
            account.available_cash = round(float(funds["available_cash"]), 2)
            account.fund_updated_at = datetime.now()
        learned_account_no = None
        if account_no_masked and not account.account_no_masked:
            account.account_no_masked = account_no_masked
            learned_account_no = account_no_masked
        self._recompute_account_funds(account)
        batch.status = "done"
        batch.parsed_json = json.dumps(
            {
                "version": 1,
                "before_funds": before_funds,
                "after_funds": {"available_cash": account.available_cash},
                "funds_applied": funds,
                "trade_ids": trade_ids,
                "watermarks": {"snapshot_id": int(snapshot_watermark), "trade_id": int(trade_watermark)},
                "counts": counts,
                "as_of_date": as_of_date,
                "screenshot_date": screenshot_date,
            },
            ensure_ascii=False,
        )
        self.db.add(
            SystemLog(
                level="INFO",
                module="positions",
                message="position screenshot sync applied",
                context_json=json.dumps({"batch_id": batch.id, "account_id": account.id, **counts}, ensure_ascii=False),
            )
        )
        self.db.commit()
        parts = [
            f"新增 {counts['added']} 只" if counts["added"] else "",
            f"更新 {counts['updated']} 只" if counts["updated"] else "",
            f"清仓 {counts['closed']} 只" if counts["closed"] else "",
            f"记了 {counts['trades']} 笔成交" if counts["trades"] else "",
            "更新了可用资金" if funds is not None else "",
        ]
        summary = "，".join(part for part in parts if part) or "没有变化"
        return {
            "batch_id": batch.id,
            "account": self._account_out(account),
            "counts": counts,
            "funds_applied": funds is not None,
            "learned_account_no": learned_account_no,
            "warnings": warnings,
            "message": f"{account.broker_name}已同步：{summary}。",
        }

    # ------------------------------------------------------------------ 撤销同步
    def undo_position_sync(self, batch_id: int) -> dict[str, Any]:
        """撤销一次同步：删掉本批快照、删掉本批记的成交、可用资金回到同步前。

        同步之后这个账户又有别的改动（再次同步、手动编辑、记成交）就不能整体撤销，避免把后来的改动一起抹掉。
        """
        batch = self.db.get(PositionImportBatch, batch_id)
        if batch is None or not self._owns(batch) or batch.source_type != SYNC_SOURCE_TYPE:
            raise PositionNotFoundError("同步记录不存在")
        if batch.status != "done":
            raise ValueError("这次同步已经撤销过了。")
        account = self._owned_account(int(batch.account_id))
        meta = json.loads(batch.parsed_json or "{}")
        watermarks = meta.get("watermarks") or {}
        trade_ids = [int(item) for item in meta.get("trade_ids") or []]

        later_batch = self.db.scalar(
            select(PositionImportBatch.id)
            .where(
                PositionImportBatch.account_id == account.id,
                PositionImportBatch.id > batch.id,
                PositionImportBatch.status.in_(("pending", "done")),
                self._owner_clause(PositionImportBatch),
            )
            .limit(1)
        )
        later_snapshot = self.db.scalar(
            select(PositionHoldingSnapshot.id)
            .where(
                PositionHoldingSnapshot.account_id == account.id,
                PositionHoldingSnapshot.id > int(watermarks.get("snapshot_id") or 0),
                (PositionHoldingSnapshot.import_batch_id.is_(None)) | (PositionHoldingSnapshot.import_batch_id != batch.id),
                PositionHoldingSnapshot.source_type != "price_sync",
                self._owner_clause(PositionHoldingSnapshot),
            )
            .limit(1)
        )
        later_trade_query = select(PositionTrade.id).where(
            PositionTrade.account_id == account.id,
            PositionTrade.id > int(watermarks.get("trade_id") or 0),
            self._owner_clause(PositionTrade),
        )
        if trade_ids:
            later_trade_query = later_trade_query.where(PositionTrade.id.notin_(trade_ids))
        later_trade = self.db.scalar(later_trade_query.limit(1))
        if later_batch or later_snapshot or later_trade:
            raise ValueError("这次同步之后，这个账户又有新的改动，不能整体撤销了。需要修改的话，请直接在持仓表里编辑。")

        # 行情刷新也会改 fund_updated_at，因此用实际现金核对，不能仅凭更新时间判断。
        # 缺少同步后资金基线的旧批次不能安全恢复现金。
        after_funds = meta.get("after_funds")
        if not isinstance(after_funds, dict) or "available_cash" not in after_funds:
            raise ValueError("这次同步缺少撤销所需的资金核对记录，请直接在持仓表里编辑。")
        expected_cash = after_funds["available_cash"]
        current_cash = account.available_cash
        cash_changed = (expected_cash is None) != (current_cash is None)
        if expected_cash is not None and current_cash is not None:
            cash_changed = abs(float(expected_cash) - float(current_cash)) > 0.005
        if cash_changed:
            raise ValueError("这次同步之后，这个账户的可用资金又有新的改动，不能整体撤销了。请先核对资金。")

        snapshots = self.db.scalars(
            select(PositionHoldingSnapshot).where(
                PositionHoldingSnapshot.import_batch_id == batch.id,
                self._owner_clause(PositionHoldingSnapshot),
            )
        ).all()
        pairs = {(snapshot.symbol, snapshot.account_id) for snapshot in snapshots}
        # 后续行情快照继承了本批的股数。一起移除这些派生快照，才能真正恢复同步前的持仓；
        # 只处理本批修改过的股票，不碰该账户其他股票的行情记录。
        price_snapshots = self.db.scalars(
            select(PositionHoldingSnapshot).where(
                PositionHoldingSnapshot.account_id == account.id,
                PositionHoldingSnapshot.symbol.in_([symbol for symbol, _ in pairs]),
                PositionHoldingSnapshot.id > int(watermarks.get("snapshot_id") or 0),
                PositionHoldingSnapshot.source_type == "price_sync",
                self._owner_clause(PositionHoldingSnapshot),
            )
        ).all() if pairs else []
        for snapshot in [*snapshots, *price_snapshots]:
            self.db.delete(snapshot)
        for trade in self.db.scalars(
            select(PositionTrade).where(PositionTrade.id.in_(trade_ids or [-1]), self._owner_clause(PositionTrade))
        ).all():
            self.db.delete(trade)
        self.db.flush()
        self._restore_positions_from_latest_snapshots(pairs)

        before_funds = meta.get("before_funds") or {}
        account.available_cash = before_funds.get("available_cash")
        restored_at = before_funds.get("fund_updated_at")
        account.fund_updated_at = datetime.fromisoformat(restored_at) if restored_at else None
        self._recompute_account_funds(account)
        batch.status = "rolled_back"
        self.db.add(
            SystemLog(
                level="WARNING",
                module="positions",
                message="position screenshot sync undone",
                context_json=json.dumps(
                    {
                        "batch_id": batch.id,
                        "account_id": account.id,
                        "snapshots": len(snapshots),
                        "price_snapshots": len(price_snapshots),
                        "trades": len(trade_ids),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self.db.commit()
        return {
            "batch_id": batch.id,
            "account": self._account_out(account),
            "restored": len(pairs),
            "message": f"已撤销这次同步，{account.broker_name}的持仓和可用资金回到了同步前。",
        }
