"""券商账户域 mixin：账户 CRUD、资金重算、持仓快照输出。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select, update

from backend.app.data.broker_presets import broker_code_for_name, broker_name_for
from backend.app.core.models import DailyBar, Position, PositionAccount, PositionHoldingSnapshot, SystemLog
from backend.app.data._helpers import dt
from backend.app.data.errors import PositionNotFoundError


class PositionAccountMixin:
    def _unique_open_account_for(self, symbol: str) -> tuple[bool, int | None]:
        """该标的现有 open 持仓归属的账户。恰好一个账户持有 → (True, account_id 可为 None)；否则 (False, None)。"""
        accounts = {
            p.account_id
            for p in self.db.scalars(
                select(Position).where(Position.symbol == symbol, Position.status == "open")
                .where(self._owner_clause(Position))
            ).all()
        }
        if len(accounts) == 1:
            return True, next(iter(accounts))
        return False, None

    def _ensure_position_account(self, payload: dict[str, Any], symbol: str | None = None) -> PositionAccount | None:
        # 1) 显式 account_id 直取（前端选定目标账户 / 用户明说时透传）
        acc_id = payload.get("account_id")
        if acc_id is not None:
            existing = self.db.get(PositionAccount, int(acc_id))
            if existing is not None and self._owns(existing):
                return existing
            raise PositionNotFoundError(f"position account not found: {acc_id}")
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
                if existing is not None and self._owns(existing):
                    return existing
        # 3) 数字人链路注入的默认账户（弱提示）：仅在无法匹配现有持仓时兜底
        fallback_id = payload.get("default_account_id")
        if fallback_id:
            existing = self.db.get(PositionAccount, int(fallback_id))
            if existing is not None and self._owns(existing):
                return existing
        broker_code = payload.get("broker_code")
        broker_name = str(
            payload.get("broker_name") or payload.get("broker") or broker_name_for(broker_code) or "manual"
        ).strip() or "manual"
        account_type = str(payload.get("account_type") or "cash").strip() or "cash"
        account_no_masked = payload.get("account_no_masked") or payload.get("account_no")
        account_name = payload.get("account_name")
        query = select(PositionAccount).where(
            PositionAccount.broker_name == broker_name,
            PositionAccount.account_type == account_type,
            PositionAccount.status == "active",
            self._owner_clause(PositionAccount),
        )
        if account_no_masked:
            query = query.where(PositionAccount.account_no_masked == str(account_no_masked))
            account = self.db.scalar(query.limit(1))
        else:
            candidates = self.db.scalars(query.order_by(PositionAccount.id)).all()
            named = [
                item
                for item in candidates
                if account_name and (item.account_name or "").strip() == str(account_name).strip()
            ]
            if len(named) == 1:
                account = named[0]
            elif len(candidates) == 1:
                # A broker often has a masked account number in the database
                # while the user only says "方正证券账户". Reuse the sole
                # active account instead of silently creating a duplicate.
                account = candidates[0]
            elif len(candidates) > 1:
                raise ValueError(
                    f"{broker_name} 存在多个证券账户，请明确选择 account_id 或脱敏账号后再保存"
                )
            else:
                account = None
        if account is None:
            account = PositionAccount(
                owner_user_id=self.owner_user_id,
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
        """按该账户各标的最新快照重算持仓市值/累计浮盈/当日盈亏；总资产=可用现金+市值。

        - available_cash 由用户维护（update_account_funds 写入），是资金模型的输入
        - total_asset = available_cash + holding_market_value（系统算，买入后市值变→总资产联动）
        - day_pnl = Σ (daily_bars 最新 close − pre_close) × 持仓量（今日 vs 昨日收盘）
        """
        ordered = self.db.scalars(
            select(PositionHoldingSnapshot)
            .where(
                PositionHoldingSnapshot.account_id == account.id,
                self._owner_clause(PositionHoldingSnapshot),
            )
            .order_by(
                PositionHoldingSnapshot.symbol,
                desc(PositionHoldingSnapshot.as_of_date),
                desc(PositionHoldingSnapshot.captured_at),
                desc(PositionHoldingSnapshot.id),
            )
        ).all()
        latest_by_symbol: dict[str, PositionHoldingSnapshot] = {}
        for snap in ordered:
            latest_by_symbol.setdefault(snap.symbol, snap)
        snaps = list(latest_by_symbol.values())
        open_snaps = [s for s in snaps if (s.status or "open") == "open" and (s.position_quantity or 0) > 0]
        hmv = round(sum(s.market_value or 0 for s in open_snaps), 4)
        account.holding_market_value = hmv
        account.total_pnl = round(sum(s.floating_pnl or 0 for s in open_snaps), 4)
        # 实时同步已把 (最新价 - 昨收) × 持仓量写入快照；只对缺失值回退到日线。
        # 否则每分钟行情同步后，账户汇总会被旧的日线收盘价重新覆盖。
        day_pnl = 0.0
        for s in open_snaps:
            if s.day_pnl is not None:
                day_pnl += float(s.day_pnl)
                continue
            bar = self.db.scalar(
                select(DailyBar)
                .where(DailyBar.symbol == s.symbol)
                .order_by(desc(DailyBar.trade_date), desc(DailyBar.id))
                .limit(1)
            )
            if bar and bar.close is not None and bar.pre_close is not None:
                day_pnl += (float(bar.close) - float(bar.pre_close)) * (s.position_quantity or 0)
        account.day_pnl = round(day_pnl, 4)
        # 现金未知时仍以持仓市值作为账户资产下限，避免账户和总览资产被整项漏算。
        # 一旦用户维护可用现金，继续使用“现金 + 持仓市值”的完整口径。
        account.total_asset = round((account.available_cash or 0) + hmv, 4)

    def _account_out(self, account: PositionAccount) -> dict[str, Any]:
        holding_count = (
            self.db.scalar(
                select(func.count(func.distinct(Position.symbol))).where(
                    Position.account_id == account.id,
                    Position.status == "open",
                    self._owner_clause(Position),
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
            .where(
                PositionAccount.status != "archived",
                self._owner_clause(PositionAccount),
            )
            .order_by(PositionAccount.sort_order, PositionAccount.id)
        ).all()
        for account in accounts:
            self._recompute_account_funds(account)
        self.db.commit()
        return [self._account_out(account) for account in accounts]

    def position_account_exists(self, account_id: int) -> bool:
        """只读判定账户是否存在且属于当前用户，不触发资金重算/事务提交。"""
        account = self.db.get(PositionAccount, account_id)
        return account is not None and self._owns(account)

    def create_position_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        broker_code = str(payload.get("broker_code") or "manual").strip() or "manual"
        broker_name = str(payload.get("broker_name") or broker_name_for(broker_code) or "其他券商").strip()
        account_type = str(payload.get("account_type") or "cash").strip() or "cash"
        account_no = payload.get("account_no_masked")
        query = select(PositionAccount).where(
            PositionAccount.broker_name == broker_name,
            PositionAccount.account_type == account_type,
            self._owner_clause(PositionAccount),
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
            self.db.execute(
                update(PositionAccount)
                .where(self._owner_clause(PositionAccount))
                .values(is_default=False)
            )
        account = PositionAccount(
            owner_user_id=self.owner_user_id,
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
        if account is None or not self._owns(account):
            raise PositionNotFoundError("账户不存在")
        for field in ("account_name", "account_no_masked", "account_type", "status", "remark", "sort_order"):
            if payload.get(field) is not None:
                setattr(account, field, payload[field])
        if payload.get("is_default") is not None:
            if payload["is_default"]:
                self.db.execute(
                    update(PositionAccount)
                    .where(
                        PositionAccount.id != account_id,
                        self._owner_clause(PositionAccount),
                    )
                    .values(is_default=False)
                )
            account.is_default = bool(payload["is_default"])
        self.db.commit()
        return self._account_out(account)

    def update_account_funds(self, account_id: int, available_cash: float | None) -> dict[str, Any]:
        """维护"可用现金"（券商 App 现金数）。总资产由 _recompute 按 现金+市值 推算。"""
        account = self.db.get(PositionAccount, account_id)
        if account is None or not self._owns(account):
            raise PositionNotFoundError("账户不存在")
        account.available_cash = float(available_cash) if available_cash is not None else None
        self._recompute_account_funds(account)
        account.fund_updated_at = datetime.now()
        self.db.add(
            SystemLog(
                level="INFO",
                module="positions",
                message="account funds updated",
                context_json=json.dumps({"account_id": account_id, "available_cash": account.available_cash, "total_asset": account.total_asset}, ensure_ascii=False),
            )
        )
        self.db.commit()
        return self._account_out(account)

    def positions_summary(self, account_id: int | None = None) -> dict[str, Any]:
        query = select(PositionAccount).where(
            PositionAccount.status != "archived",
            self._owner_clause(PositionAccount),
        )
        if account_id is not None:
            query = query.where(PositionAccount.id == account_id)
        accounts = self.db.scalars(query).all()
        for account in accounts:
            self._recompute_account_funds(account)
        self.db.commit()
        count_query = select(func.count(func.distinct(Position.symbol))).where(
            Position.status == "open",
            self._owner_clause(Position),
        )
        if account_id is not None:
            count_query = count_query.where(Position.account_id == account_id)
        return {
            "account_count": len(accounts),
            "total_asset": round(
                sum(
                    a.total_asset
                    if a.total_asset is not None
                    else (a.holding_market_value or 0)
                    for a in accounts
                ),
                2,
            ),
            "available_cash": round(sum(a.available_cash for a in accounts if a.available_cash is not None), 2) if any(a.available_cash is not None for a in accounts) else None,
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
