from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    pass


def _require_mysql_url(database_url: str) -> None:
    if not database_url.startswith("mysql+pymysql"):
        raise ValueError("SixRabbits Stock uses MySQL only. Set DATABASE_URL or STOCK_DB_* to a mysql+pymysql URL.")


def create_engine_for_url(database_url: str) -> Engine:
    _require_mysql_url(database_url)
    return create_engine(
        database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_recycle=600,
        connect_args={"connect_timeout": 20, "read_timeout": 300, "write_timeout": 300},
    )


def ensure_mysql_database(database_url: str) -> None:
    _require_mysql_url(database_url)

    url = make_url(database_url)
    db_name = url.database
    if not db_name:
        return

    server_url = url.set(database=None)
    engine = create_engine_for_url(str(server_url))
    quoted_name = db_name.replace("`", "``")
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{quoted_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    engine.dispose()


def init_database(engine: Engine) -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_schema_upgrades(engine)


def _ensure_schema_upgrades(engine: Engine) -> None:
    if engine.dialect.name != "mysql":
        return
    with engine.begin() as connection:
        has_chat_mode = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'ai_chat_sessions' "
                "AND column_name = 'chat_mode'"
            )
        ).scalar()
        if not has_chat_mode:
            connection.execute(
                text(
                    "ALTER TABLE ai_chat_sessions "
                    "ADD COLUMN chat_mode VARCHAR(24) NOT NULL DEFAULT 'research' AFTER status"
                )
            )
        has_prompt_recommendation_unique = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'ai_prompt_recommendations' "
                "AND index_name = 'uq_prompt_recommendation_active'"
            )
        ).scalar()
        if has_prompt_recommendation_unique:
            for index_name, column_name in (
                ("ix_ai_prompt_recommendations_user_id", "user_id"),
                ("ix_ai_prompt_recommendations_digital_human_id", "digital_human_id"),
            ):
                has_index = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.statistics "
                        "WHERE table_schema = DATABASE() "
                        "AND table_name = 'ai_prompt_recommendations' "
                        "AND index_name = :index_name"
                    ),
                    {"index_name": index_name},
                ).scalar()
                if not has_index:
                    connection.execute(
                        text(f"CREATE INDEX {index_name} ON ai_prompt_recommendations ({column_name})")
                    )
            connection.execute(text("ALTER TABLE ai_prompt_recommendations DROP INDEX uq_prompt_recommendation_active"))

        # 持仓：账户资金字段 + positions.account_id（多券商账户重构，加列不破坏现有数据）
        def _add_column(table: str, column: str, ddl: str) -> None:
            exists = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
            if not exists:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        for column, ddl in (
            ("broker_code", "broker_code VARCHAR(40) NULL"),
            ("is_default", "is_default TINYINT(1) NOT NULL DEFAULT 0"),
            ("sort_order", "sort_order INT NOT NULL DEFAULT 0"),
            ("total_asset", "total_asset DOUBLE NULL"),
            ("available_cash", "available_cash DOUBLE NULL"),
            ("holding_market_value", "holding_market_value DOUBLE NULL"),
            ("total_pnl", "total_pnl DOUBLE NULL"),
            ("day_pnl", "day_pnl DOUBLE NULL"),
            ("fund_updated_at", "fund_updated_at DATETIME NULL"),
        ):
            _add_column("position_accounts", column, ddl)
        _add_column("positions", "account_id", "account_id INT NULL")

        # 回填 broker_code（按中文券商名反查），positions.account_id（关联该 symbol 最新快照账户）
        connection.execute(
            text(
                "UPDATE position_accounts SET broker_code = CASE broker_name "
                "WHEN '方正证券' THEN 'fangzheng' WHEN '广发证券' THEN 'gf' WHEN '中信证券' THEN 'citic' "
                "WHEN '华泰证券' THEN 'huatai' WHEN '国泰君安' THEN 'gtja' WHEN '中信建投' THEN 'csc' "
                "WHEN '招商证券' THEN 'cmschina' WHEN '银河证券' THEN 'galaxy' WHEN '东方财富证券' THEN 'eastmoney' "
                "ELSE 'manual' END WHERE broker_code IS NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE positions p SET p.account_id = ("
                "SELECT s.account_id FROM position_holding_snapshots s "
                "WHERE s.symbol = p.symbol AND s.account_id IS NOT NULL "
                "ORDER BY s.id DESC LIMIT 1) "
                "WHERE p.account_id IS NULL"
            )
        )


engine = create_engine_for_url(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def initialize_default_database(create_database: bool = False) -> None:
    if create_database:
        ensure_mysql_database(settings.database_url)
    init_database(engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(session_factory: sessionmaker[Session] = SessionLocal) -> Generator[Session, None, None]:
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
