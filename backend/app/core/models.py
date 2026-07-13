from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    max_items: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    items: Mapped[list["WatchlistItem"]] = relationship(back_populates="group")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("symbol", "group_id", name="uq_watchlist_symbol_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    __table_args__ = (UniqueConstraint("broker_name", "account_no_masked", "account_type", name="uq_position_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AiChatSession(Base):
    __tablename__ = "ai_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    digital_human_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("digital_human_profiles.id"))
    model_config_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("llm_model_configs.id"))
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    chat_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="research")
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
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[AiChatSession] = relationship(back_populates="messages")


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
    trading_day_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digital_human_code: Mapped[str | None] = mapped_column(String(80))
    agent_name: Mapped[str | None] = mapped_column(String(80))
    prompt_template: Mapped[str | None] = mapped_column(Text)
    push_channel_key: Mapped[str | None] = mapped_column(String(80))
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
