from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql")
BIGINT_ID = BigInteger().with_variant(Integer(), "sqlite")


class Instrument(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    list_date: Mapped[str | None] = mapped_column(String(16))
    list_status: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str | None] = mapped_column(String(120))
    delist_date: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class WatchlistGroup(Base):
    __tablename__ = "watchlist_groups"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_watchlist_group_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    max_items: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    items: Mapped[list["WatchlistItem"]] = relationship(back_populates="group")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "symbol", "group_id", name="uq_watchlist_owner_symbol_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("watchlist_groups.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(String(80), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    instrument: Mapped[Instrument] = relationship()
    group: Mapped[WatchlistGroup] = relationship(back_populates="items")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("position_accounts.id"))
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    position_pct: Mapped[float] = mapped_column(Float, nullable=False)
    cost_price: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    take_profit_price: Mapped[float | None] = mapped_column(Float)
    thesis: Mapped[str | None] = mapped_column(Text)
    risk_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    instrument: Mapped[Instrument] = relationship()


class PositionAccount(Base):
    __tablename__ = "position_accounts"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "broker_name",
            "account_no_masked",
            "account_type",
            name="uq_position_account_owner",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    broker_name: Mapped[str] = mapped_column(String(120), nullable=False, default="manual")
    account_name: Mapped[str | None] = mapped_column(String(120))
    account_no_masked: Mapped[str | None] = mapped_column(String(80))
    account_type: Mapped[str] = mapped_column(String(40), nullable=False, default="cash")
    market: Mapped[str] = mapped_column(String(40), nullable=False, default="A-share")
    currency: Mapped[str] = mapped_column(String(12), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    broker_code: Mapped[str | None] = mapped_column(String(40))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_asset: Mapped[float | None] = mapped_column(Float)
    available_cash: Mapped[float | None] = mapped_column(Float)
    holding_market_value: Mapped[float | None] = mapped_column(Float)
    total_pnl: Mapped[float | None] = mapped_column(Float)
    day_pnl: Mapped[float | None] = mapped_column(Float)
    fund_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PositionImportBatch(Base):
    __tablename__ = "position_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    source_name: Mapped[str | None] = mapped_column(String(160))
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("position_accounts.id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    image_mime_type: Mapped[str | None] = mapped_column(String(80))
    image_sha256: Mapped[str | None] = mapped_column(String(80))
    raw_text: Mapped[str | None] = mapped_column(Text)
    parsed_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[PositionAccount | None] = relationship()


class PositionHoldingSnapshot(Base):
    __tablename__ = "position_holding_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("position_accounts.id"))
    import_batch_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("position_import_batches.id"))
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    symbol_name: Mapped[str | None] = mapped_column(String(120))
    exchange: Mapped[str | None] = mapped_column(String(24))
    as_of_date: Mapped[str | None] = mapped_column(String(16))
    position_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    available_quantity: Mapped[float | None] = mapped_column(Float)
    frozen_quantity: Mapped[float | None] = mapped_column(Float)
    today_buy_quantity: Mapped[float | None] = mapped_column(Float)
    today_sell_quantity: Mapped[float | None] = mapped_column(Float)
    cost_price: Mapped[float | None] = mapped_column(Float)
    cost_amount: Mapped[float | None] = mapped_column(Float)
    latest_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    floating_pnl: Mapped[float | None] = mapped_column(Float)
    floating_pnl_pct: Mapped[float | None] = mapped_column(Float)
    day_pnl: Mapped[float | None] = mapped_column(Float)
    position_pct: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    take_profit_price: Mapped[float | None] = mapped_column(Float)
    thesis: Mapped[str | None] = mapped_column(Text)
    risk_note: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    confidence: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    captured_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[PositionAccount | None] = relationship()
    import_batch: Mapped[PositionImportBatch | None] = relationship()
    instrument: Mapped[Instrument] = relationship()


class TradeReport(Base):
    __tablename__ = "trade_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    position_pct: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    expectation: Mapped[str | None] = mapped_column(Text)
    stop_condition: Mapped[str | None] = mapped_column(Text)
    review_date: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PositionTrade(Base):
    """买卖交易流水（每次买/卖/加/减/平仓记一条，与持仓状态快照分离）。可删错单，不回滚持仓。"""

    __tablename__ = "position_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("position_accounts.id"))
    position_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("positions.id"))
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(24), nullable=False)  # buy/sell/add/reduce/close
    quantity: Mapped[float | None] = mapped_column(Float)  # 本次变动量（正数）
    price: Mapped[float | None] = mapped_column(Float)  # 本次成交价
    amount: Mapped[float | None] = mapped_column(Float)  # quantity * price
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    account: Mapped[PositionAccount | None] = relationship()


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (UniqueConstraint("symbol", "ts", "source", name="uq_market_snapshot_symbol_ts_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    quality: Mapped[str] = mapped_column(String(24), nullable=False, default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DailyBar(Base):
    __tablename__ = "daily_bars"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", "source", name="uq_daily_bar_symbol_date_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    trade_date: Mapped[str] = mapped_column(String(16), nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketTradingCalendar(Base):
    __tablename__ = "market_trading_calendar"
    __table_args__ = (
        UniqueConstraint("exchange", "cal_date", name="uq_market_trading_calendar_exchange_date"),
        Index("ix_market_trading_calendar_open_date", "exchange", "is_open", "cal_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="SSE")
    cal_date: Mapped[str] = mapped_column(String(8), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pretrade_date: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BacktestRun(Base):
    """Immutable strategy backtest result used by chat cards and full reports."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_owner_created", "owner_user_id", "created_at"),
        Index("ix_backtest_runs_symbol_created", "symbol", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="completed")
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    strategy_code: Mapped[str] = mapped_column(String(80), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(24), nullable=False, default="1D")
    start_date: Mapped[str] = mapped_column(String(16), nullable=False)
    end_date: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False, default="insufficient")
    config_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    metrics_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    result_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    summary_markdown: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    source_code: Mapped[str | None] = mapped_column(LONG_TEXT)
    data_fingerprint: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockLatestPrice(Base):
    """全体 Agent 共用的最新价缓存：开盘时段按 TTL 从 TuShare 刷新，非开盘时间只读缓存不外呼。"""

    __tablename__ = "stock_latest_prices"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    price: Mapped[float | None] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    trade_date: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class StockDailyBasic(Base):
    """全市场每日指标快照（TuShare daily_basic 落库）：换手率/官方量比/估值/市值。

    唯一写入方是 market/market_sync.py（trade_date 恒为 YYYYMMDD），选股器的
    换手率/量比/市值/PE 条件在这张表上做 SQL 过滤。市值单位为**万元**。
    """

    __tablename__ = "stock_daily_basic"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_stock_daily_basic_symbol_date"),
        Index("ix_stock_daily_basic_trade_date", "trade_date"),
        # 既有表全是 utf8mb4_unicode_ci（库默认是 0900_ai_ci），不一致会导致 FK 建不出来
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False, index=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    turnover_rate: Mapped[float | None] = mapped_column(Float)
    turnover_rate_f: Mapped[float | None] = mapped_column(Float)  # 自由流通换手率，模型 1 的特征
    volume_ratio: Mapped[float | None] = mapped_column(Float)
    pe_ttm: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    circ_mv: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockAdjFactor(Base):
    """全市场复权因子（TuShare adj_factor 落库）：后复权价 = 不复权价 × adj_factor。

    唯一写入方是 market/market_sync.py（trade_date 恒为 YYYYMMDD）。daily_bars 存的是
    不复权价，凡跨交易日比较价格（动量、波动、均线、前瞻收益）都必须先乘同日因子，
    否则送转/分红的除权缺口会被当成真实涨跌。不加外键：因子行与标的生命周期解耦。
    """

    __tablename__ = "stock_adj_factors"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_stock_adj_factor_symbol_date"),
        Index("ix_stock_adj_factors_trade_date", "trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    adj_factor: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScreenerTemplateRecommendation(Base):
    """每用户一份的智能选股模板推荐：LLM 依据持仓风格生成，「换一批」整行覆盖。

    user_id 不加外键（只增表，与用户生命周期解耦）；templates_json 存
    [{name, desc, query, universe}]，basis 为一句话持仓风格总结。
    """

    __tablename__ = "screener_template_recommendations"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_screener_tpl_recommendation_user"),
        Index("ix_screener_tpl_recommendation_user", "user_id"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    templates_json: Mapped[str] = mapped_column(Text, nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScreenerPromptHistory(Base):
    """用户级选股提示词历史：自动保存、收藏并记录最近一次命中数。

    user_id 不加外键，与用户生命周期解耦；prompt_hash 用于同一用户下去重，
    重复使用相同提示词时只更新最近使用时间和使用次数。
    """

    __tablename__ = "screener_prompt_history"
    __table_args__ = (
        UniqueConstraint("user_id", "prompt_hash", name="uq_screener_prompt_history_user_hash"),
        Index("ix_screener_prompt_history_user_last_used", "user_id", "last_used_at"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_query_json: Mapped[str | None] = mapped_column(Text)
    parse_summary: Mapped[str | None] = mapped_column(Text)
    last_result_count: Mapped[int | None] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScreenerSavedStrategy(Base):
    """用户保存的选股规则，可跨设备复用并记录最近一次命中数。"""

    __tablename__ = "screener_saved_strategies"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_screener_saved_strategy_user_name"),
        Index("ix_screener_saved_strategy_user_updated", "user_id", "updated_at"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    query_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_result_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    trust_level: Mapped[str] = mapped_column(String(24), nullable=False, default="normal")
    is_external_untrusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), index=True)


class DocumentSymbol(Base):
    __tablename__ = "document_symbols"
    __table_args__ = (UniqueConstraint("doc_id", "symbol", name="uq_document_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    relevance: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reason: Mapped[str | None] = mapped_column(Text)


class DocumentChunk(Base):
    """RAG 知识库切块：documents 正文切分后的最小检索单元（向量在向量库，正文在此表）。"""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vector_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    embed_model: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="indexed")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    document: Mapped[Document] = relationship()


class KnowledgeUpload(Base):
    """知识库上传批次（P2 上传解析用，P1 先建表不长逻辑）。"""

    __tablename__ = "knowledge_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    doc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("documents.id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="parsing")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(80))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BackgroundTask(Base):
    """Durable state for background work that must survive a process restart."""

    __tablename__ = "background_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class WorkspaceDocument(Base):
    """Agent 工作区生成文档的登记表：文件系统是存在性权威，本表补"关联对话 + 入库状态"元数据。

    path 唯一（相对工作区根）。session_id 关联生成该文档的对话；ingested_doc_id 非空表示
    已加入知识库（指向 documents.id）。文件被覆盖时按 path upsert。
    """

    __tablename__ = "workspace_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # path is stored with a user namespace (for example users/42/notes/a.md).
    # Keeping it globally unique also protects older deployments that already have
    # a unique index on this column.
    path: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"))
    digital_human_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    ingested_doc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("documents.id"))


class Rule(Base):
    __tablename__ = "rules"

    rule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    applies_to: Mapped[str] = mapped_column(String(40), nullable=False, default="watchlist")
    params_json: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    facts_json: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(120))
    rule_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("rules.rule_id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    instrument: Mapped[Instrument | None] = relationship()
    evidence: Mapped[list["EventEvidence"]] = relationship(back_populates="event")


class EventEvidence(Base):
    __tablename__ = "event_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_table: Mapped[str | None] = mapped_column(String(80))
    source_id: Mapped[str | None] = mapped_column(String(120))
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="evidence")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="viewer")
    permission_scope: Mapped[str] = mapped_column(String(500), nullable=False, default="dashboard:view,agent:use,data:read")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


class McpAccessToken(Base):
    __tablename__ = "mcp_access_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    scopes: Mapped[str] = mapped_column(String(500), nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class McpMutationReceipt(Base):
    __tablename__ = "mcp_mutation_receipts"
    __table_args__ = (
        UniqueConstraint("user_id", "tool_name", "idempotency_key", name="uq_mcp_mutation_user_tool_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LlmModelPlatform(Base):
    __tablename__ = "llm_model_platforms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(80), nullable=False, default="OpenAI compatible")
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    model_configs: Mapped[list["LlmModelConfig"]] = relationship(back_populates="platform")


class LlmModelConfig(Base):
    __tablename__ = "llm_model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_model_platforms.id"))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(500))
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    # 推理强度分级：auto=不下发参数跟随服务商默认；off/low/medium/high/xhigh/max 按服务商映射
    # （OpenAI 系透传顶层 reasoning_effort，off→none、max→xhigh；通义/DashScope 系映射 enable_thinking，off=关闭）
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    platform: Mapped[LlmModelPlatform | None] = relationship(back_populates="model_configs")


class DataSourceConfig(Base):
    __tablename__ = "data_source_configs"

    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(80), nullable=False, default="market_data")
    base_url: Mapped[str | None] = mapped_column(String(255))
    api_key: Mapped[str | None] = mapped_column(String(500))
    model: Mapped[str | None] = mapped_column(String(120))  # 嵌入/重排等需要独立模型名
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DigitalHumanProfile(Base):
    __tablename__ = "digital_human_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    avatar_tone: Mapped[str] = mapped_column(String(80), nullable=False, default="calm")
    target_roles: Mapped[str] = mapped_column(String(255), nullable=False, default="*")
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    quick_prompts_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    feishu_app_id: Mapped[str | None] = mapped_column(String(80))
    feishu_app_secret_cipher: Mapped[str | None] = mapped_column(Text)
    feishu_bot_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_write_positions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delegation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delegation_description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiChatSession(Base):
    __tablename__ = "ai_chat_sessions"
    __table_args__ = (
        UniqueConstraint(
            "digital_human_id",
            "source_type",
            "external_conversation_id",
            name="uq_ai_chat_session_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    digital_human_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"))
    model_config_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_model_configs.id"))
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    chat_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="research")
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, default="web")
    external_conversation_id: Mapped[str | None] = mapped_column(String(160))
    source_label: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship()
    digital_human: Mapped[DigitalHumanProfile | None] = relationship()
    model_config: Mapped[LlmModelConfig | None] = relationship()
    messages: Mapped[list["AiChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class AiChatMessage(Base):
    __tablename__ = "ai_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    # Agent Team / Workflow 会把上游节点结果拼入后续节点提示词，正文可能超过
    # MySQL TEXT 的 64 KiB 上限。使用 LONGTEXT 避免运行时长上下文把 Worker 打断。
    content: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    attachments_json: Mapped[str | None] = mapped_column(Text)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[AiChatSession] = relationship(back_populates="messages")


class AgentToolApproval(Base):
    __tablename__ = "agent_tool_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(160))
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(24), nullable=False)
    arguments_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentRun(Base):
    """Durable execution record; browser connections are not run ownership."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_agent_run_idempotency"),
        Index("ix_agent_runs_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_agent_runs_session_created", "session_id", "created_at"),
        Index("ix_agent_run_board", "board_visible", "status", "updated_at"),
        Index("ix_agent_run_agent_queue", "assignee_profile_id", "status", "priority", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy")
    chat_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="research")
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, default="web")
    source_ref: Mapped[str | None] = mapped_column(String(160))
    parent_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    root_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    team_id: Mapped[str | None] = mapped_column(String(64), index=True)
    orchestration_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    request_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    result_message_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("ai_chat_messages.id"))
    completion_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_applicable"
    )
    completion_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    last_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(String(160))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 任务看板字段
    assignee_profile_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"))
    task_title: Mapped[str | None] = mapped_column(String(240))
    board_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ResearchGoal(Base):
    """Evidence-driven objective attached to one durable Agent run."""

    __tablename__ = "research_goals"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_goal_run"),
        Index("ix_research_goals_status_created", "status", "created_at"),
    )

    goal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    objective: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    selected_skills_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ResearchGoalCriterion(Base):
    """A deterministic completion gate for a research goal."""

    __tablename__ = "research_goal_criteria"
    __table_args__ = (
        UniqueConstraint("goal_id", "criterion_code", name="uq_research_criterion_code"),
        Index("ix_research_criteria_goal_required", "goal_id", "required"),
    )

    criterion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), ForeignKey("research_goals.goal_id"), nullable=False)
    criterion_code: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_evidence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ResearchClaim(Base):
    """A material conclusion produced by an Agent and linked to evidence."""

    __tablename__ = "research_claims"
    __table_args__ = (Index("ix_research_claims_goal_created", "goal_id", "created_at"),)

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), ForeignKey("research_goals.goal_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    statement: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(40), nullable=False, default="conclusion")
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ResearchEvidence(Base):
    """Immutable provenance for one tool result used by a research run."""

    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint(
            "goal_id",
            "tool_call_id",
            "criterion_id",
            name="uq_research_evidence_tool_criterion",
        ),
        Index("ix_research_evidence_goal_criterion", "goal_id", "criterion_id"),
    )

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), ForeignKey("research_goals.goal_id"), nullable=False)
    criterion_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("research_goal_criteria.criterion_id")
    )
    claim_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("research_claims.claim_id"))
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    tool_execution_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_tool_executions.execution_id")
    )
    tool_call_id: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_provider: Mapped[str | None] = mapped_column(String(120))
    source_ref: Mapped[str | None] = mapped_column(String(500))
    source_type: Mapped[str | None] = mapped_column(String(80))
    source_uri: Mapped[str | None] = mapped_column(String(1000))
    symbol_universe_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    timeframe: Mapped[str | None] = mapped_column(String(120))
    method: Mapped[str | None] = mapped_column(String(240))
    assumptions_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    data_as_of: Mapped[str | None] = mapped_column(String(80))
    freshness_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unknown"
    )
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unverified"
    )
    confidence: Mapped[str | None] = mapped_column(String(24))
    caveat: Mapped[str | None] = mapped_column(LONG_TEXT)
    validation_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ResearchCompletionAudit(Base):
    """Immutable snapshot explaining why a run is complete or incomplete."""

    __tablename__ = "research_completion_audits"
    __table_args__ = (
        UniqueConstraint("goal_id", "sequence", name="uq_research_audit_goal_sequence"),
        Index("ix_research_audits_run_created", "run_id", "created_at"),
    )

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(64), ForeignKey("research_goals.goal_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_state: Mapped[str] = mapped_column(String(24), nullable=False)
    required_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    satisfied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    summary_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentTeamRun(Base):
    """One durable fan-out/fan-in operation owned by a leader Agent run."""

    __tablename__ = "agent_team_runs"
    __table_args__ = (
        UniqueConstraint("leader_run_id", "tool_call_id", name="uq_agent_team_leader_call"),
        Index("ix_agent_team_status_deadline", "status", "deadline_at"),
    )

    team_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    leader_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    max_members: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    result_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 任务看板字段
    execution_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="merge_by_agent")
    completion_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="all_required")


class AgentDelegation(Base):
    """A child Agent assignment within a durable Team run."""

    __tablename__ = "agent_delegations"
    __table_args__ = (
        UniqueConstraint("team_id", "member_index", name="uq_agent_delegation_member"),
        UniqueConstraint("child_run_id", name="uq_agent_delegation_child_run"),
        UniqueConstraint("team_id", "action_key", name="uq_team_action"),
        UniqueConstraint("action_key", name="uq_delegation_action_key"),
        Index("ix_agent_delegations_team_status", "team_id", "status"),
    )

    delegation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_team_runs.team_id"), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    child_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    member_index: Mapped[int] = mapped_column(Integer, nullable=False)
    target_profile_id: Mapped[int] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"), nullable=False)
    target_profile_code: Mapped[str] = mapped_column(String(80), nullable=False)
    target_profile_name: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    expected_output: Mapped[str | None] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    result_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 任务看板字段
    title: Mapped[str | None] = mapped_column(String(240))
    action_key: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    extraction_source: Mapped[str] = mapped_column(String(24), nullable=False, default="tool_call")


class AgentWorkflowTemplate(Base):
    """Stable identity for a reviewed, reusable Agent workflow."""

    __tablename__ = "agent_workflow_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="research")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentWorkflowTemplateVersion(Base):
    """Immutable DAG definition for one workflow template version."""

    __tablename__ = "agent_workflow_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_agent_workflow_template_version"),
    )

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_workflow_templates.template_id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentWorkflowRun(Base):
    """Durable execution of a versioned DAG, owned by a leader Agent run."""

    __tablename__ = "agent_workflow_runs"
    __table_args__ = (
        UniqueConstraint("leader_run_id", "tool_call_id", name="uq_agent_workflow_leader_call"),
        Index("ix_agent_workflow_runs_status_deadline", "status", "deadline_at"),
    )

    workflow_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_workflow_templates.template_id"), nullable=False
    )
    template_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_workflow_template_versions.version_id"), nullable=False
    )
    template_code: Mapped[str] = mapped_column(String(80), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    leader_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    variables_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    result_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentWorkflowNodeRun(Base):
    """One durable node in a workflow run; child AgentRun is created when dependencies unlock."""

    __tablename__ = "agent_workflow_node_runs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "node_key", name="uq_agent_workflow_node"),
        UniqueConstraint("child_run_id", name="uq_agent_workflow_child_run"),
        Index("ix_agent_workflow_nodes_status", "workflow_run_id", "status"),
    )

    node_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_workflow_runs.workflow_run_id"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(80), nullable=False)
    node_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role_name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_profile_code: Mapped[str] = mapped_column(String(80), nullable=False)
    target_profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("digital_human_profiles.id")
    )
    instruction_template: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    expected_output: Mapped[str | None] = mapped_column(LONG_TEXT)
    dependencies_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_runs.run_id")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting_dependency")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    result_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentRunEvent(Base):
    """Ordered, replayable events for a durable Agent run."""

    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_run_event_seq"),
        Index("ix_agent_run_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentRunCheckpoint(Base):
    """Latest AgentScope state used to resume after approval or worker loss."""

    __tablename__ = "agent_run_checkpoints"

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), primary_key=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    state_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    waiting_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentRunStep(Base):
    """A durable, queryable stage within an Agent run."""

    __tablename__ = "agent_run_steps"
    __table_args__ = (
        Index("ix_agent_run_steps_run_seq", "run_id", "seq"),
        Index("ix_agent_run_steps_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="stage")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    output_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentToolExecution(Base):
    """Auditable lifecycle of one model-issued tool call."""

    __tablename__ = "agent_tool_executions"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_agent_tool_run_call"),
        Index("ix_agent_tool_exec_status", "status", "updated_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id"), nullable=False)
    step_id: Mapped[int | None] = mapped_column(BIGINT_ID, ForeignKey("agent_run_steps.id"))
    tool_call_id: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False, default="v1")
    execution_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="inline")
    risk_level: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    idempotency_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    arguments_sha256: Mapped[str | None] = mapped_column(String(64))
    arguments_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    result_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    has_side_effects: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    available_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentOutbox(Base):
    """Transactional messages for future external tool workers."""

    __tablename__ = "agent_outbox"
    __table_args__ = (Index("ix_agent_outbox_dispatch", "status", "available_at", "id"),)

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(160), nullable=False)
    topic: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_ref: Mapped[str | None] = mapped_column(String(160))
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentArtifact(Base):
    """Stable identity for a workspace artifact across edits and runs."""

    __tablename__ = "agent_artifacts"
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_path", name="uq_agent_artifact_user_path"),
        Index("ix_agent_artifacts_session", "session_id", "updated_at"),
    )

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"))
    digital_human_code: Mapped[str | None] = mapped_column(String(80))
    workspace_path: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    active_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentArtifactVersion(Base):
    """Immutable snapshot and lineage for one artifact revision."""

    __tablename__ = "agent_artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_agent_artifact_version"),
        Index("ix_agent_artifact_versions_run", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_artifacts.artifact_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    tool_execution_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_tool_executions.execution_id"))
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BIGINT_ID, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    validation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    validation_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentTransactionState(Base):
    """Structured slots for position/watchlist agents across turns and channels."""

    __tablename__ = "agent_transaction_states"
    __table_args__ = (
        UniqueConstraint("session_id", "agent_code", name="uq_agent_transaction_session_code"),
        Index("ix_agent_transaction_expiry", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_chat_sessions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    agent_code: Mapped[str] = mapped_column(String(80), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(80))
    slots_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="{}")
    evidence_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="[]")
    missing_fields_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="[]")
    pending_action_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="collecting")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiPromptRecommendation(Base):
    __tablename__ = "ai_prompt_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    digital_human_id: Mapped[int] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"), nullable=False)
    digital_human_code: Mapped[str] = mapped_column(String(80), nullable=False)
    chat_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="research")
    prompt_text: Mapped[str] = mapped_column(String(500), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="history")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship()
    digital_human: Mapped[DigitalHumanProfile] = relationship()


class UserMemory(Base):
    """长期用户记忆 — 从对话中异步提取的结构化事实/偏好/决策/缺口。"""

    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    digital_human_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"))
    memory_type: Mapped[str] = mapped_column(String(24), nullable=False, default="fact")
    # fact | preference | decision | gap | interest
    content: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    # 来源引用，如 "session:123, 2026-07-11"
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    # active | superseded | archived
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship()
    digital_human: Mapped[DigitalHumanProfile | None] = relationship()


class ExperienceEntry(Base):
    """L2 经验记忆 — 按用户隔离的复盘经验条目。

    与 UserMemory 严格分库：UserMemory 是用户画像，本表是用户自己的统计经验，
    同样按 user_id 隔离并按 tags/market_env/status 过滤。每条带 sample_size +
    statistical_strength，防小样本过拟合。
    状态：observing(观察模式，新条目观察期满才激活) → active → archived(长期无效自动降权)。
    """

    __tablename__ = "experience_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str | None] = mapped_column(Text)        # JSON：行业/形态/信号类型
    market_env_json: Mapped[str | None] = mapped_column(Text)  # JSON：牛熊/大小盘风格，防跨环境错配
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    statistical_strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="observing")
    source_review_id: Mapped[int | None] = mapped_column(Integer)
    injected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_post_injection_return: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


class ExperienceInjection(Base):
    """经验注入留痕 — 每次把经验注入分析师决策时记录，后填成绩用于自动降权。

    成绩回填（post_return）依赖 L3 台账知道"注入后该决策赚没赚"；L3 未上线前 post_return 留空，
    接口已就位，L3 上线后回填流程接通即可。
    """

    __tablename__ = "experience_injections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("experience_entries.id"), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(120))  # 关联的决策/指令标识（L3 上线后填）
    injected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    relevance_score: Mapped[float | None] = mapped_column(Float)
    post_return: Mapped[float | None] = mapped_column(Float)       # 注入后该决策超额收益，L3 回填
    human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship()
    entry: Mapped["ExperienceEntry"] = relationship()


class ExperienceRevision(Base):
    """经验条目版本演变 — 借鉴 clawd fork 血缘，每次修改记 parent_revision_id，可追溯。"""

    __tablename__ = "experience_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("experience_entries.id"), nullable=False)
    parent_revision_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("experience_revisions.id"))
    diff: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship()
    entry: Mapped["ExperienceEntry"] = relationship(foreign_keys=[entry_id])
    # parent 自引用关系留待需要时用 SQL 查 parent_revision_id，不在 model 定义以避开自引用复杂度


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    agent_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    role_description: Mapped[str | None] = mapped_column(Text)
    permission_scope_json: Mapped[str | None] = mapped_column(Text)
    model_primary: Mapped[str | None] = mapped_column(String(120))
    model_backup: Mapped[str | None] = mapped_column(String(120))
    memory_policy: Mapped[str | None] = mapped_column(Text)
    output_schema_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    event_id: Mapped[str | None] = mapped_column(String(120))
    agent_name: Mapped[str] = mapped_column(String(80), ForeignKey("agent_profiles.agent_name"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(120))
    input_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class AgentReport(Base):
    __tablename__ = "agent_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    report_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    task_id: Mapped[str | None] = mapped_column(String(120))
    event_id: Mapped[str | None] = mapped_column(String(120))
    agent_name: Mapped[str] = mapped_column(String(80), ForeignKey("agent_profiles.agent_name"), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    conclusion: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    counter_arguments: Mapped[str | None] = mapped_column(Text)
    risk_points: Mapped[str | None] = mapped_column(Text)
    position_limit: Mapped[str | None] = mapped_column(Text)
    stop_condition: Mapped[str | None] = mapped_column(Text)
    action_suggestion: Mapped[str] = mapped_column(String(40), nullable=False, default="observe")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    instrument: Mapped[Instrument | None] = relationship()
    profile: Mapped[AgentProfile] = relationship()


class RiskCheck(Base):
    __tablename__ = "risk_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    position_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("positions.id"))
    event_id: Mapped[str | None] = mapped_column(String(120))
    report_id: Mapped[str | None] = mapped_column(String(120))
    risk_level: Mapped[str] = mapped_column(String(24), nullable=False)
    risk_type: Mapped[str] = mapped_column(String(80), nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    required_action: Mapped[str | None] = mapped_column(Text)
    position_limit: Mapped[float | None] = mapped_column(Float)
    stop_condition: Mapped[str | None] = mapped_column(Text)
    cool_down_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    instrument: Mapped[Instrument | None] = relationship()


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"), nullable=False)
    trade_report_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("trade_reports.id"))
    related_report_id: Mapped[str | None] = mapped_column(String(120))
    review_date: Mapped[str | None] = mapped_column(String(16))
    user_decision: Mapped[str | None] = mapped_column(Text)
    system_advice: Mapped[str | None] = mapped_column(Text)
    actual_result: Mapped[str | None] = mapped_column(Text)
    lesson: Mapped[str | None] = mapped_column(Text)
    system_blindspot: Mapped[str | None] = mapped_column(Text)
    rule_improvement: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(80), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(120))
    report_id: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ApiAuditLog(Base):
    __tablename__ = "api_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(120))
    status_code: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cost_units: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    module: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    detail_json: Mapped[str | None] = mapped_column(Text)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    job_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="agent_report")
    schedule_type: Mapped[str] = mapped_column(String(24), nullable=False, default="daily")
    run_time: Mapped[str | None] = mapped_column(String(5))
    weekday_mask: Mapped[str | None] = mapped_column(String(20))
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    window_start: Mapped[str | None] = mapped_column(String(5))
    window_end: Mapped[str | None] = mapped_column(String(5))
    run_once_at: Mapped[datetime | None] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    trading_day_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digital_human_code: Mapped[str | None] = mapped_column(String(80))
    agent_name: Mapped[str | None] = mapped_column(String(80))
    prompt_template: Mapped[str | None] = mapped_column(Text)
    push_channel_key: Mapped[str | None] = mapped_column(String(80))
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    created_by_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    created_via: Mapped[str] = mapped_column(String(24), nullable=False, default="admin")
    allow_code_execution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="full")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(24))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # 任务看板字段
    delegation_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="execute_and_wait")
    task_extraction_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="structured_tool")
    completion_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="all_required")
    child_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    max_delegated_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=20)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("job_key", "fire_key", name="uq_job_run_fire"),
        Index("ix_job_run_leader", "leader_agent_run_id"),
        Index("ix_job_run_orchestration", "orchestration_type", "orchestration_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    job_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    fire_key: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False, default="schedule")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    report_id: Mapped[str | None] = mapped_column(String(120))
    notification_id: Mapped[int | None] = mapped_column(Integer)
    output_preview: Mapped[str | None] = mapped_column(Text)
    output_content: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    # 任务看板字段
    leader_agent_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_runs.run_id"))
    orchestration_type: Mapped[str | None] = mapped_column(String(24))
    orchestration_id: Mapped[str | None] = mapped_column(String(64))
    handoff_status: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    handoff_plan_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    handoff_error: Mapped[str | None] = mapped_column(Text)


class NotifyChannel(Base):
    __tablename__ = "notify_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False, default="feishu_webhook")
    purpose: Mapped[str] = mapped_column(String(24), nullable=False, default="daily")
    webhook_url_cipher: Mapped[str | None] = mapped_column(Text)
    secret_cipher: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    remark: Mapped[str | None] = mapped_column(Text)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class OperationLog(Base):
    """写操作审计：谁在何时对哪个接口做了什么变更。

    user_id 不做外键（日志只增、与用户生命周期解耦，删用户不牵连历史审计）。
    """

    __tablename__ = "operation_logs"
    __table_args__ = (Index("ix_operation_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str | None] = mapped_column(String(120))
    status_code: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LoginLog(Base):
    """登录审计：谁在何时从哪个 IP 登录成功/失败。

    只增表、user_id 不做外键（与用户生命周期解耦，删用户不牵连历史登录审计）。
    失败登录（用户名不存在/密码错/限流）也记录，username 保留输入值，user_id 可空。
    """

    __tablename__ = "login_logs"
    __table_args__ = (Index("ix_login_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    role: Mapped[str | None] = mapped_column(String(40))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(String(40))  # bad_credentials/disabled/rate_limited
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TushareFactorCatalog(Base):
    """TuShare 官方因子库目录（付费接口）：``factor_list`` 同步的因子元信息。

    与 ``tushare_factor_values`` 配合：目录回答"有哪些因子"，值表缓存按需摄入的因子值。
    """

    __tablename__ = "tushare_factor_catalog"
    __table_args__ = (
        # 既有表全是 utf8mb4_unicode_ci（库默认是 0900_ai_ci），不一致会导致跨表字符串比较报 1267
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    factor_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(8), nullable=False, default="STK")
    factor_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    factor_desc: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TushareFactorValue(Base):
    """TuShare 因子值缓存：按 (因子, 交易日) 截面按需摄入，重复摄入幂等覆盖。

    TuShare 因子值由官方基于全市场计算（部分为截面排名），本地按股票池过滤行不改变
    保留值本身的含义。``trade_date`` 存 YYYYMMDD 字符串（与 TuShare 口径一致）。
    """

    __tablename__ = "tushare_factor_values"
    __table_args__ = (
        UniqueConstraint("factor_name", "symbol", "trade_date", name="uq_tushare_factor_values"),
        Index("ix_tushare_factor_values_factor_date", "factor_name", "trade_date"),
        # 与 instruments 等既有表跨表 JOIN symbol，排序规则必须一致（见 TushareFactorCatalog）
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # 000001.SZ
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    factor_value: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionStrategy(Base):
    __tablename__ = "automatic_selection_strategies"
    __table_args__ = (
        UniqueConstraint("strategy_key", "version", name="uq_auto_selection_strategy_version"),
        UniqueConstraint("official_slot", name="uq_auto_selection_official_slot"),
        Index("ix_auto_selection_strategy_state", "lifecycle_state", "enabled"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, default="shadow")
    official_slot: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    parent_strategy_id: Mapped[int | None] = mapped_column(Integer)
    config_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer)
    promoted_by_user_id: Mapped[int | None] = mapped_column(Integer)
    promotion_note: Mapped[str | None] = mapped_column(Text)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionStrategyEvent(Base):
    __tablename__ = "automatic_selection_strategy_events"
    __table_args__ = (
        Index("ix_auto_selection_strategy_event", "strategy_id", "created_at"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str | None] = mapped_column(String(24))
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionStockSnapshot(Base):
    __tablename__ = "automatic_selection_stock_snapshots"
    __table_args__ = (
        UniqueConstraint("observed_trade_date", "symbol", name="uq_auto_selection_stock_snapshot"),
        Index("ix_auto_selection_stock_snapshot_date", "observed_trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    observed_trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    market: Mapped[str | None] = mapped_column(String(24))
    stock_type: Mapped[str] = mapped_column(String(24), nullable=False, default="stock")
    list_status: Mapped[str] = mapped_column(String(8), nullable=False, default="L")
    list_date: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare.stock_basic")
    raw_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionRun(Base):
    __tablename__ = "automatic_selection_runs"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "target_trade_date",
            "config_hash",
            "data_fingerprint",
            name="uq_auto_selection_run_inputs",
        ),
        Index("ix_auto_selection_run_date_status", "target_trade_date", "status"),
        Index("ix_auto_selection_run_strategy_date", "strategy_id", "target_trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False, default="schedule")
    job_run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="quality_gate")
    config_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    data_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_run_id: Mapped[str | None] = mapped_column(String(64))
    sync_summary_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    quality_gate_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    exclusion_summary_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_turnover: Mapped[float | None] = mapped_column(Float)
    llm_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    llm_model: Mapped[str | None] = mapped_column(String(160))
    llm_explanation: Mapped[str | None] = mapped_column(LONG_TEXT)
    llm_error: Mapped[str | None] = mapped_column(LONG_TEXT)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionCandidate(Base):
    __tablename__ = "automatic_selection_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", name="uq_auto_selection_candidate"),
        Index("ix_auto_selection_candidate_rank", "run_id", "eligible", "rank"),
        Index("ix_auto_selection_candidate_selected", "run_id", "selected"),
        Index("ix_auto_selection_candidate_change", "run_id", "change_state"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    market: Mapped[str | None] = mapped_column(String(24))
    list_status: Mapped[str | None] = mapped_column(String(8))
    list_date: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str | None] = mapped_column(String(120))
    market_cap_bucket: Mapped[str | None] = mapped_column(String(16))
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_codes_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="[]")
    constraint_codes_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="[]")
    close_price: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    pe_ttm: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    circ_mv: Mapped[float | None] = mapped_column(Float)
    avg_amount_20: Mapped[float | None] = mapped_column(Float)
    momentum_20: Mapped[float | None] = mapped_column(Float)
    volatility_20: Mapped[float | None] = mapped_column(Float)
    factor_values_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    input_evidence_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    z_scores_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    composite_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    percentile: Mapped[float | None] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    research_weight: Mapped[float | None] = mapped_column(Float)
    change_state: Mapped[str] = mapped_column(String(24), nullable=False, default="unchanged")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomaticSelectionEvaluation(Base):
    __tablename__ = "automatic_selection_evaluations"
    __table_args__ = (
        UniqueConstraint("run_id", "horizon", name="uq_auto_selection_evaluation"),
        Index("ix_auto_selection_evaluation_status", "status", "horizon"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    entry_trade_date: Mapped[str | None] = mapped_column(String(8))
    exit_trade_date: Mapped[str | None] = mapped_column(String(8))
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    covered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_ratio: Mapped[float | None] = mapped_column(Float)
    benchmark_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    benchmark_covered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    benchmark_coverage_ratio: Mapped[float | None] = mapped_column(Float)
    basket_return: Mapped[float | None] = mapped_column(Float)
    benchmark_return: Mapped[float | None] = mapped_column(Float)
    excess_return: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)
    sharpe: Mapped[float | None] = mapped_column(Float)
    information_ratio: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    commission_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0003)
    slippage_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0005)
    daily_series_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    per_symbol_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    limitations: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(LONG_TEXT)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class InboxNotification(Base):
    """站内通知（用户收件箱）：系统/智能体/报告等事件推送给指定用户，按用户隔离可见。

    与既有 ``Notification``（外部渠道推送流水：channel/status/sent_at）职责不同：
    那个记录"对外发了没"，这个记录"用户在站内看到了什么"。
    user_id 不做外键：通知随用户删除一并清理即可，无需外键约束牵连。
    """

    __tablename__ = "inbox_notifications"
    __table_args__ = (Index("ix_inbox_notifications_user_unread", "user_id", "is_read"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")  # info/success/warning/error
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="system")  # system/agent/report/risk/scheduler
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(255))  # 前端跳转标识（页面名或 URL）
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PaperAccount(Base):
    """模拟盘账户：与真实持仓域完全隔离的研究用虚拟账户，每用户一个。"""

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PaperPosition(Base):
    """模拟盘持仓：(user_id, symbol) 唯一；卖空即删行，不留零持仓。"""

    __tablename__ = "paper_positions"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_paper_position_user_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PaperOrder(Base):
    """模拟盘成交流水：市价单即时成交（成交价=最新价缓存），只增不改。"""

    __tablename__ = "paper_orders"
    __table_args__ = (Index("ix_paper_orders_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Float)  # 仅卖出单有值
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RuntimeConfigOverride(Base):
    """运行时配置覆盖：白名单参数的 key/value，免重启生效。"""

    __tablename__ = "runtime_config_overrides"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReportShare(Base):
    """飞书推送附带的手机版网页：推送时存正文快照，链接本身就是凭证。

    不登记进 sql_sandbox（数字人查不到分享 ID）；只增表，与用户、报告的生命周期解耦，不加外键。
    时间按 UTC 裸时间存，和 created_at 的 NOW() 一致。
    """

    __tablename__ = "report_shares"
    __table_args__ = (
        Index("ix_report_shares_source", "source_type", "source_ref"),
        Index("ix_report_shares_owner_created", "owner_user_id", "created_at"),
    )

    share_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_md: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ShadowAccount(Base):
    """影子账户：真实账户开始时的持仓和现金复制一份，之后由代码挂单、模拟成交（shadow/service）。"""

    __tablename__ = "shadow_accounts"
    __table_args__ = (UniqueConstraint("real_account_id", "strategy", name="uq_shadow_account_strategy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    real_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="risk")  # risk 影子 A / risk_rank 影子 B
    account_label: Mapped[str] = mapped_column(String(160), nullable=False)
    started_on: Mapped[str] = mapped_column(String(8), nullable=False)
    idle_cash: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # 开始时的现金，一直不动，只算进净值
    pool: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # 卖出款，只用来买回
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ShadowPosition(Base):
    __tablename__ = "shadow_positions"
    __table_args__ = (UniqueConstraint("shadow_account_id", "symbol", name="uq_shadow_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shadow_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)  # 除权后可能不是整数


class ShadowOrder(Base):
    __tablename__ = "shadow_orders"
    __table_args__ = (Index("ix_shadow_order_account_status", "shadow_account_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shadow_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    basis_date: Mapped[str] = mapped_column(String(8), nullable=False)  # 用哪天收盘算的，下一个交易日挂
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(120), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # sell / buy
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")  # pending / filled / expired
    fill_price: Mapped[float | None] = mapped_column(Float)
    settled_on: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockModelSignal(Base):
    """模型每天收盘后算的信号。影子账户重跑和出计划都读这张表，不用再扫全市场。"""

    __tablename__ = "stock_model_signals"
    __table_args__ = (UniqueConstraint("trade_date", "symbol", name="uq_stock_model_signal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    model_rank: Mapped[float | None] = mapped_column(Float)  # 0 到 1，越大越好
    buy_k: Mapped[float | None] = mapped_column(Float)  # 昨收下方几倍 ATR 挂买单；空表示模型说不挂


class ShadowNav(Base):
    """每个交易日收盘，影子账户和真实账户的总资产。"""

    __tablename__ = "shadow_nav"
    __table_args__ = (UniqueConstraint("shadow_account_id", "trade_date", name="uq_shadow_nav"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shadow_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    shadow_value: Mapped[float] = mapped_column(Float, nullable=False)
    real_value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketEvent(Base):
    """持仓和自选的一条消息（公告或新闻）：规则分级、模型解读、推送或并入报告（events/service）。

    每位用户、每只股票的同一条消息只有一行，所以重复拉取不会重复推送。
    """

    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "symbol", "content_hash", name="uq_market_event"),
        Index("ix_market_event_status", "status"),
        Index("ix_market_event_owner_symbol", "owner_user_id", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # announcement / news
    doc_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)  # 北京时间；公告只有日期
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)  # critical / major / minor
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="new")  # new / ready / queued / pushed / merged
    direction: Mapped[str | None] = mapped_column(String(12))  # positive / negative / neutral，模型判断
    reliability: Mapped[str | None] = mapped_column(String(4))
    summary: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str | None] = mapped_column(Text)
    plan_effect: Mapped[str | None] = mapped_column(Text)
    interpreted_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TechSignalRecord(Base):
    """技术信号：固定规则按收盘价算出的方向，一只股票每个交易日一行（quant/tech_signal）。

    能由 daily_bars 重新算出来。规则还没通过验证，目前不写入，也没登记进 sql_sandbox。
    不加外键：信号行与标的生命周期解耦。
    """

    __tablename__ = "tech_signals"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", "rule_version", name="uq_tech_signal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long 红 / flat 绿 / unknown K 线不够
    close: Mapped[float] = mapped_column(Float, nullable=False)
    bars_used: Mapped[int] = mapped_column(Integer, nullable=False)
    flipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flip_date: Mapped[str | None] = mapped_column(String(8))
    days_in_state: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flip_price: Mapped[float | None] = mapped_column(Float)  # 下一交易日收盘高于它是红，否则是绿
    dif: Mapped[float | None] = mapped_column(Float)
    dea: Mapped[float | None] = mapped_column(Float)
    macd_hist: Mapped[float | None] = mapped_column(Float)
    ma5: Mapped[float | None] = mapped_column(Float)
    ma10: Mapped[float | None] = mapped_column(Float)
    ma20: Mapped[float | None] = mapped_column(Float)
    ma60: Mapped[float | None] = mapped_column(Float)
    rsi14: Mapped[float | None] = mapped_column(Float)
    atr14: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TradePlan(Base):
    """一份操作计划（早盘、午盘、晚间、周、月、选股）。

    status：draft（数字人存好，报告还没定稿）/ active / superseded（被新计划替代）。
    草稿不会提醒：客户先看到报告，才会收到这份计划的提醒。时间按 UTC 裸时间存。
    """

    __tablename__ = "trade_plans"
    __table_args__ = (
        UniqueConstraint("job_run_id", name="uq_trade_plans_job_run"),
        Index("ix_trade_plans_owner_date", "owner_user_id", "plan_date"),
        Index("ix_trade_plans_status_date", "status", "plan_date"),
    )

    plan_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_date: Mapped[str] = mapped_column(String(10), nullable=False)  # 开始生效的交易日 YYYY-MM-DD
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    job_key: Mapped[str | None] = mapped_column(String(120))
    job_run_id: Mapped[str] = mapped_column(String(120), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    report_id: Mapped[str | None] = mapped_column(String(160))
    digital_human_code: Mapped[str | None] = mapped_column(String(80))
    push_channel_key: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TradePlanItem(Base):
    """计划里的一条操作。状态和成交结果由 plans.service.reconcile_owner 从成交流水推出来。

    status：pending / triggered / partially_executed / executed / expired / superseded。
    """

    __tablename__ = "trade_plan_items"
    __table_args__ = (
        Index("ix_trade_plan_items_plan", "plan_id", "sort_order"),
        Index("ix_trade_plan_items_owner", "owner_user_id", "symbol"),
        Index("ix_trade_plan_items_notify", "triggered_at", "notified_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # buy/add/sell/reduce/hold/stop_loss/cancel
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, default="none")  # limit/rise_to/fall_to/none
    trigger_price: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[float | None] = mapped_column(Float)
    funding_note: Mapped[str | None] = mapped_column(Text)
    advised_quantity: Mapped[float | None] = mapped_column(Float)  # 代码建议：正数卖，负数可补，0 不动，空表示没有建议
    size_note: Mapped[str | None] = mapped_column(Text)  # 股数和建议不同的理由
    valid_until: Mapped[str] = mapped_column(String(10), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    text_matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 和报告正文对得上才盯价
    baseline_high: Mapped[float | None] = mapped_column(Float)  # 第一次盯价时的当日最高价，之前的高点不算
    baseline_low: Mapped[float | None] = mapped_column(Float)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    triggered_price: Mapped[float | None] = mapped_column(Float)
    trigger_via: Mapped[str | None] = mapped_column(String(8))  # price / spike
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    executed_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    trade_ids: Mapped[str | None] = mapped_column(Text)  # 对账匹配到的 position_trades.id，JSON 数组
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketDailyReturn(Base):
    """全市场 A 股当天涨跌的等权平均。成绩单拿它当基准，每天收盘数据到齐后算一次。"""

    __tablename__ = "market_daily_returns"

    trade_date: Mapped[str] = mapped_column(String(8), primary_key=True)
    mean_return: Mapped[float] = mapped_column(Float, nullable=False)
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdviceOutcome(Base):
    """一条建议在若干个交易日之后的结果（成绩单）。规则见 scorecard/rules.py。

    一位用户的一个账户、一个交易日、一只股票、一个方向只记一条建议，同一天早盘和午盘都提到时取最后那份计划里的。
    账户之间不合并：同一位用户名下的几个账户各算各的。account_id 为 0 表示计划条目上没有写账户。
    verdict：空（还没到期）/ right / wrong / flat（差不多）/ no_data（这段时间没有行情）。
    """

    __tablename__ = "advice_outcomes"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "account_id", "advice_date", "symbol", "direction", "horizon_days", name="uq_advice_outcome_account"),
        Index("ix_advice_outcomes_owner_date", "owner_user_id", "account_id", "advice_date"),
        Index("ix_advice_outcomes_pending", "verdict", "advice_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    advice_date: Mapped[str] = mapped_column(String(8), nullable=False)  # 建议生效的交易日 YYYYMMDD
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # up：建议买或拿着；down：建议卖
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(32))
    plan_item_id: Mapped[int | None] = mapped_column(Integer)
    plan_source: Mapped[str | None] = mapped_column(String(16))
    digital_human_code: Mapped[str | None] = mapped_column(String(80))
    trigger_price: Mapped[float | None] = mapped_column(Float)
    followed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 客户有没有照做（有成交）
    size_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 数字人改了代码建议的股数
    model_rank: Mapped[float | None] = mapped_column(Float)  # 建议前一个交易日的模型排名，0 到 1
    due_date: Mapped[str | None] = mapped_column(String(8))
    stock_return: Mapped[float | None] = mapped_column(Float)
    market_return: Mapped[float | None] = mapped_column(Float)
    excess_return: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str | None] = mapped_column(String(8))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IndexDailyBar(Base):
    """宽基指数日线（如 000300.SH 沪深300、000852.SH 中证1000），供 L1 市场环境趋势与均线计算。
    不设 instruments 外键，避免与个股目录耦合。
    """

    __tablename__ = "index_daily_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_index_daily_bar_symbol_date"),
        Index("ix_index_daily_bars_date", "trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    pre_close: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    vol: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockEventSignal(Base):
    """全市场公共公告与事件排雷表（L6 事件与窗口底座）。
    不局限于持仓与自选，全市场 5000+ 只股票的立案调查、退市风险警示、财务造假、业绩预告等客观记录在此。
    """

    __tablename__ = "stock_event_signals"
    __table_args__ = (
        UniqueConstraint("symbol", "content_hash", name="uq_stock_event_signal_sym_hash"),
        Index("ix_stock_event_signal_sym_date", "symbol", "ann_date"),
        Index("ix_stock_event_signal_date_sev", "ann_date", "severity"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    ann_date: Mapped[str] = mapped_column(String(8), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")  # veto / negative / positive / neutral
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare:anns_d")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StockFinancialPit(Base):
    """按真实披露日（ann_date）严格对齐的 PIT 财务指标快照，杜绝按报告期（end_date）提前偷看未来财报。"""

    __tablename__ = "stock_financial_pit"
    __table_args__ = (
        UniqueConstraint("symbol", "end_date", "ann_date", name="uq_stock_fin_pit_sym_end_ann"),
        Index("ix_stock_fin_pit_sym_ann", "symbol", "ann_date"),
        Index("ix_stock_fin_pit_ann", "ann_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    end_date: Mapped[str] = mapped_column(String(8), nullable=False)
    ann_date: Mapped[str] = mapped_column(String(8), nullable=False)
    roe: Mapped[float | None] = mapped_column(Float)
    grossprofit_margin: Mapped[float | None] = mapped_column(Float)
    netprofit_margin: Mapped[float | None] = mapped_column(Float)
    debt_to_assets: Mapped[float | None] = mapped_column(Float)
    tr_yoy: Mapped[float | None] = mapped_column(Float)
    netprofit_yoy: Mapped[float | None] = mapped_column(Float)
    q_sales_yoy: Mapped[float | None] = mapped_column(Float)
    q_profit_yoy: Mapped[float | None] = mapped_column(Float)
    tr_yoy_accel: Mapped[float | None] = mapped_column(Float)
    profit_yoy_accel: Mapped[float | None] = mapped_column(Float)
    ocf_yoy: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="tushare.fina_indicator")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketRegimeDaily(Base):
    """L1 A 股本土市场环境温度计（只输出总仓位上限与风格象限，绝不掺入个股综合分）。"""

    __tablename__ = "market_regime_daily"
    __table_args__ = (
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    trade_date: Mapped[str] = mapped_column(String(8), primary_key=True)
    breadth_ma20: Mapped[float | None] = mapped_column(Float)
    breadth_ma60: Mapped[float | None] = mapped_column(Float)
    up_down_ratio: Mapped[float | None] = mapped_column(Float)
    total_amount_yi: Mapped[float | None] = mapped_column(Float)
    amount_pct_120: Mapped[float | None] = mapped_column(Float)
    hs300_close: Mapped[float | None] = mapped_column(Float)
    hs300_ma60: Mapped[float | None] = mapped_column(Float)
    hs300_ma200: Mapped[float | None] = mapped_column(Float)
    hs300_trend: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    zz1000_close: Mapped[float | None] = mapped_column(Float)
    zz1000_ma60: Mapped[float | None] = mapped_column(Float)
    zz1000_ma200: Mapped[float | None] = mapped_column(Float)
    zz1000_trend: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    regime_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    stance: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")  # attack / neutral / defense
    max_position_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.60)
    style_quadrant: Mapped[str] = mapped_column(String(32), nullable=False, default="balanced")
    summary: Mapped[str | None] = mapped_column(Text)
    metrics_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class StockSelectionDailySnapshot(Base):
    """每日预计算的 L2–L6 分层选股截面宽表（按 plan_type 存储 swing / long / event 三档结论）。"""

    __tablename__ = "stock_selection_daily_snapshot"
    __table_args__ = (
        UniqueConstraint("trade_date", "symbol", "plan_type", name="uq_selection_snap_date_sym_plan"),
        Index("ix_selection_snap_query", "trade_date", "plan_type", "verdict", "total_score"),
        Index("ix_selection_snap_symbol", "symbol", "trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    market: Mapped[str | None] = mapped_column(String(24))
    industry: Mapped[str | None] = mapped_column(String(120))
    plan_type: Mapped[str] = mapped_column(String(16), nullable=False)  # swing / long / event
    total_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    raw_score: Mapped[float | None] = mapped_column(Float)
    coverage_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="pass")  # action / watch / pass
    vetoed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    veto_codes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    l2_industry_score: Mapped[float | None] = mapped_column(Float)
    l3_fundamental_score: Mapped[float | None] = mapped_column(Float)
    l4_expectation_score: Mapped[float | None] = mapped_column(Float)
    l5_timing_score: Mapped[float | None] = mapped_column(Float)
    l6_event_score: Mapped[float | None] = mapped_column(Float)
    model_rank: Mapped[float | None] = mapped_column(Float)
    buy_k: Mapped[float | None] = mapped_column(Float)
    close_price: Mapped[float | None] = mapped_column(Float)
    pct_chg: Mapped[float | None] = mapped_column(Float)
    atr14: Mapped[float | None] = mapped_column(Float)
    pe_ttm: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    total_mv_yi: Mapped[float | None] = mapped_column(Float)
    turnover_rate: Mapped[float | None] = mapped_column(Float)
    suggested_buy_price: Mapped[float | None] = mapped_column(Float)
    stop_ref_price: Mapped[float | None] = mapped_column(Float)
    expectation_thesis: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SelectionShadowNav(Base):
    """选股策略连续模拟净值账本：用于 model-rank-30 与 L1–L6 分层策略无偏并排对账。"""

    __tablename__ = "selection_shadow_nav"
    __table_args__ = (
        UniqueConstraint("trade_date", "strategy_key", name="uq_selection_shadow_nav_date_key"),
        Index("ix_selection_shadow_nav_key_date", "strategy_key", "trade_date"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    nav: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    daily_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    benchmark_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    excess_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    holdings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turnover: Mapped[float | None] = mapped_column(Float)
    holdings_json: Mapped[str | None] = mapped_column(LONG_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
