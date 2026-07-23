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
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


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
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments_json: Mapped[str | None] = mapped_column(Text)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
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


class AgentDelegation(Base):
    """A child Agent assignment within a durable Team run."""

    __tablename__ = "agent_delegations"
    __table_args__ = (
        UniqueConstraint("team_id", "member_index", name="uq_agent_delegation_member"),
        UniqueConstraint("child_run_id", name="uq_agent_delegation_child_run"),
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
    """L2 经验记忆 — 跨用户共享的复盘经验条目。

    与 UserMemory 严格分库：UserMemory 是用户画像（按 user_id 隔离），本表是统计经验（跨用户共享，
    按 tags/market_env/status 过滤）。每条带 sample_size + statistical_strength，防小样本过拟合。
    状态：observing(观察模式，新条目观察期满才激活) → active → archived(长期无效自动降权)。
    """

    __tablename__ = "experience_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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


class ExperienceInjection(Base):
    """经验注入留痕 — 每次把经验注入分析师决策时记录，后填成绩用于自动降权。

    成绩回填（post_return）依赖 L3 台账知道"注入后该决策赚没赚"；L3 未上线前 post_return 留空，
    接口已就位，L3 上线后回填流程接通即可。
    """

    __tablename__ = "experience_injections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("experience_entries.id"), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(120))  # 关联的决策/指令标识（L3 上线后填）
    injected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    relevance_score: Mapped[float | None] = mapped_column(Float)
    post_return: Mapped[float | None] = mapped_column(Float)       # 注入后该决策超额收益，L3 回填
    human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    entry: Mapped["ExperienceEntry"] = relationship()


class ExperienceRevision(Base):
    """经验条目版本演变 — 借鉴 clawd fork 血缘，每次修改记 parent_revision_id，可追溯。"""

    __tablename__ = "experience_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("experience_entries.id"), nullable=False)
    parent_revision_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("experience_revisions.id"))
    diff: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

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


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job_key", "fire_key", name="uq_job_run_fire"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    job_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
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
