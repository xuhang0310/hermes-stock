from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.core.settings import settings


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
    from backend.app.core import models  # noqa: F401

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
        # 数字人 ↔ 飞书自建应用（机器人）绑定字段
        for column_name, ddl in (
            ("feishu_app_id", "ADD COLUMN feishu_app_id VARCHAR(80) NULL AFTER sort_order"),
            ("feishu_app_secret_cipher", "ADD COLUMN feishu_app_secret_cipher TEXT NULL AFTER feishu_app_id"),
            ("feishu_bot_enabled", "ADD COLUMN feishu_bot_enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER feishu_app_secret_cipher"),
            ("can_write_positions", "ADD COLUMN can_write_positions TINYINT(1) NOT NULL DEFAULT 0 AFTER feishu_bot_enabled"),
            ("delegation_enabled", "ADD COLUMN delegation_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER can_write_positions"),
            ("delegation_description", "ADD COLUMN delegation_description VARCHAR(500) NULL AFTER delegation_enabled"),
        ):
            has_column = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'digital_human_profiles' "
                    "AND column_name = :column_name"
                ),
                {"column_name": column_name},
            ).scalar()
            if not has_column:
                connection.execute(text(f"ALTER TABLE digital_human_profiles {ddl}"))
                from backend.app.core.applog import get_logger

                get_logger("db").info(f"迁移：digital_human_profiles 新增列 {column_name}")

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

        _add_column("users", "token_version", "token_version INT NOT NULL DEFAULT 1")
        # Legacy L2 experience rows were shared and cannot be safely attributed
        # to one user. Keep them unowned (and therefore invisible) instead of
        # assigning private history to the administrator by guesswork.
        for table in (
            "experience_entries",
            "experience_injections",
            "experience_revisions",
        ):
            _add_column(table, "user_id", "user_id INT NULL")

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
        _add_column("job_runs", "owner_user_id", "owner_user_id INT NULL")
        _add_column("events", "owner_user_id", "owner_user_id INT NULL")
        for table in (
            "watchlist_groups",
            "watchlist_items",
            "positions",
            "position_accounts",
            "position_import_batches",
            "position_holding_snapshots",
            "position_trades",
            "agent_tasks",
            "agent_reports",
            "risk_checks",
        ):
            _add_column(table, "owner_user_id", "owner_user_id INT NULL")

        legacy_owner_sql = (
            "COALESCE("
            "(SELECT id FROM users WHERE username = 'admin' ORDER BY id LIMIT 1),"
            "(SELECT id FROM users WHERE is_enabled = 1 ORDER BY id LIMIT 1),"
            "(SELECT MIN(id) FROM users)"
            ")"
        )
        for table in (
            "watchlist_groups",
            "watchlist_items",
            "positions",
            "position_accounts",
            "position_import_batches",
            "position_holding_snapshots",
            "position_trades",
            "agent_tasks",
            "agent_reports",
            "risk_checks",
        ):
            connection.execute(
                text(
                    f"UPDATE {table} SET owner_user_id = {legacy_owner_sql} "
                    "WHERE owner_user_id IS NULL"
                )
            )

        # 规则引擎事件来自个人持仓/自选；市场、政策等公共事件继续保持无主共享。
        connection.execute(
            text(
                f"UPDATE events SET owner_user_id = {legacy_owner_sql} "
                "WHERE owner_user_id IS NULL AND source_type = 'rule_engine'"
            )
        )

        # 旧版本把个人 Agent 报告镜像进共享 documents。清除这批历史镜像及其
        # 关系行；向量库里的孤立 id 不再能通过数据库 JOIN 命中。
        private_report_doc = "d.doc_type = 'report' AND d.source LIKE 'agent:%'"
        connection.execute(
            text(
                "UPDATE knowledge_uploads ku JOIN documents d ON d.id = ku.doc_id "
                f"SET ku.doc_id = NULL WHERE {private_report_doc}"
            )
        )
        connection.execute(
            text(
                "UPDATE workspace_documents wd JOIN documents d ON d.id = wd.ingested_doc_id "
                f"SET wd.ingested_doc_id = NULL WHERE {private_report_doc}"
            )
        )
        connection.execute(
            text(
                "DELETE ds FROM document_symbols ds JOIN documents d ON d.id = ds.doc_id "
                f"WHERE {private_report_doc}"
            )
        )
        connection.execute(
            text(
                "DELETE dc FROM document_chunks dc JOIN documents d ON d.id = dc.doc_id "
                f"WHERE {private_report_doc}"
            )
        )
        connection.execute(
            text(f"DELETE d FROM documents d WHERE {private_report_doc}")
        )

        # Merge legacy Agent-created `default` groups into the canonical
        # per-user group. Duplicates are soft-removed before moving rows.
        owners = list(
            connection.execute(
                text(
                    "SELECT DISTINCT owner_user_id FROM watchlist_groups "
                    "WHERE owner_user_id IS NOT NULL"
                )
            ).scalars()
        )
        for owner_user_id in owners:
            target_id = connection.execute(
                text(
                    "SELECT id FROM watchlist_groups "
                    "WHERE owner_user_id = :owner_user_id AND name = '我的自选' LIMIT 1"
                ),
                {"owner_user_id": owner_user_id},
            ).scalar()
            legacy_ids = list(
                connection.execute(
                    text(
                        "SELECT id FROM watchlist_groups "
                        "WHERE owner_user_id = :owner_user_id AND name IN ('default', '默认')"
                    ),
                    {"owner_user_id": owner_user_id},
                ).scalars()
            )
            if target_id is None and legacy_ids:
                target_id = legacy_ids.pop(0)
                connection.execute(
                    text("UPDATE watchlist_groups SET name = '我的自选' WHERE id = :group_id"),
                    {"group_id": target_id},
                )
                connection.execute(
                    text(
                        "UPDATE watchlist_items SET "
                        "reason = COALESCE(NULLIF(TRIM(reason), ''), 'Agent录入') "
                        "WHERE group_id = :group_id AND status = 'active'"
                    ),
                    {"group_id": target_id},
                )
            if target_id is None:
                continue
            for legacy_id in legacy_ids:
                connection.execute(
                    text(
                        "UPDATE watchlist_items source "
                        "JOIN watchlist_items target "
                        "ON target.owner_user_id = source.owner_user_id "
                        "AND target.group_id = :target_id "
                        "AND target.symbol = source.symbol "
                        "SET source.status = 'removed', source.removed_at = COALESCE(source.removed_at, NOW()) "
                        "WHERE source.group_id = :legacy_id AND source.status = 'active'"
                    ),
                    {"target_id": target_id, "legacy_id": legacy_id},
                )
                connection.execute(
                    text(
                        "UPDATE watchlist_items SET group_id = :target_id, "
                        "reason = COALESCE(NULLIF(TRIM(reason), ''), 'Agent录入') "
                        "WHERE group_id = :legacy_id AND status = 'active'"
                    ),
                    {"target_id": target_id, "legacy_id": legacy_id},
                )
                connection.execute(
                    text(
                        "UPDATE watchlist_groups SET status = 'archived' "
                        "WHERE id = :legacy_id"
                    ),
                    {"legacy_id": legacy_id},
                )
        # Workspace documents used to share one global filesystem namespace. New
        # records are scoped to an application user; legacy rows remain unowned
        # and are intentionally not exposed through user-facing endpoints.
        _add_column("workspace_documents", "user_id", "user_id INT NULL")
        _add_column("ai_chat_messages", "attachments_json", "attachments_json TEXT NULL")
        _add_column("ai_chat_messages", "duration_ms", "duration_ms INT NULL")
        _add_column("agent_tool_approvals", "run_id", "run_id VARCHAR(64) NULL")
        _add_column("agent_tool_approvals", "tool_call_id", "tool_call_id VARCHAR(160) NULL")
        _add_column("agent_runs", "priority", "priority INT NOT NULL DEFAULT 100")
        _add_column("agent_runs", "source_type", "source_type VARCHAR(24) NOT NULL DEFAULT 'web'")
        _add_column("agent_runs", "source_ref", "source_ref VARCHAR(160) NULL")
        _add_column("agent_runs", "parent_run_id", "parent_run_id VARCHAR(64) NULL")
        _add_column("agent_runs", "root_run_id", "root_run_id VARCHAR(64) NULL")
        _add_column("agent_runs", "team_id", "team_id VARCHAR(64) NULL")
        _add_column("agent_runs", "orchestration_depth", "orchestration_depth INT NOT NULL DEFAULT 0")
        _add_column("agent_runs", "deadline_at", "deadline_at DATETIME NULL")
        _add_column(
            "agent_runs",
            "completion_state",
            "completion_state VARCHAR(24) NOT NULL DEFAULT 'not_applicable'",
        )
        _add_column("agent_runs", "completion_json", "completion_json LONGTEXT NULL")
        for column, ddl in (
            ("source_type", "source_type VARCHAR(80) NULL"),
            ("source_uri", "source_uri VARCHAR(1000) NULL"),
            ("symbol_universe_json", "symbol_universe_json LONGTEXT NULL"),
            ("timeframe", "timeframe VARCHAR(120) NULL"),
            ("method", "method VARCHAR(240) NULL"),
            ("assumptions_json", "assumptions_json LONGTEXT NULL"),
            ("data_as_of", "data_as_of VARCHAR(80) NULL"),
            ("freshness_status", "freshness_status VARCHAR(24) NOT NULL DEFAULT 'unknown'"),
            (
                "verification_status",
                "verification_status VARCHAR(24) NOT NULL DEFAULT 'unverified'",
            ),
            ("confidence", "confidence VARCHAR(24) NULL"),
            ("caveat", "caveat LONGTEXT NULL"),
            ("validation_json", "validation_json LONGTEXT NULL"),
        ):
            _add_column("research_evidence", column, ddl)
        for column, ddl in (
            ("execution_mode", "execution_mode VARCHAR(24) NOT NULL DEFAULT 'inline'"),
            ("max_attempts", "max_attempts INT NOT NULL DEFAULT 1"),
            ("timeout_seconds", "timeout_seconds INT NOT NULL DEFAULT 30"),
            ("available_at", "available_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP"),
            ("next_retry_at", "next_retry_at DATETIME NULL"),
            ("lease_owner", "lease_owner VARCHAR(160) NULL"),
            ("lease_expires_at", "lease_expires_at DATETIME NULL"),
            ("heartbeat_at", "heartbeat_at DATETIME NULL"),
            ("error_code", "error_code VARCHAR(80) NULL"),
        ):
            _add_column("agent_tool_executions", column, ddl)
        _add_column("agent_outbox", "published_ref", "published_ref VARCHAR(160) NULL")
        has_approval_run_index = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'agent_tool_approvals' "
                "AND index_name = 'ix_agent_tool_approvals_run_id'"
            )
        ).scalar()
        if not has_approval_run_index:
            connection.execute(
                text("CREATE INDEX ix_agent_tool_approvals_run_id ON agent_tool_approvals (run_id)")
            )
        _add_column("ai_chat_sessions", "source_type", "source_type VARCHAR(24) NOT NULL DEFAULT 'web'")
        _add_column(
            "ai_chat_sessions",
            "external_conversation_id",
            "external_conversation_id VARCHAR(160) NULL",
        )
        _add_column("ai_chat_sessions", "source_label", "source_label VARCHAR(80) NULL")
        has_external_session_index = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'ai_chat_sessions' "
                "AND index_name = 'uq_ai_chat_session_external'"
            )
        ).scalar()
        if not has_external_session_index:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX uq_ai_chat_session_external "
                    "ON ai_chat_sessions (digital_human_id, source_type, external_conversation_id)"
                )
            )
        has_workspace_user_index = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'workspace_documents' "
                "AND index_name = 'ix_workspace_documents_user_id'"
            )
        ).scalar()
        if not has_workspace_user_index:
            connection.execute(text("CREATE INDEX ix_workspace_documents_user_id ON workspace_documents (user_id)"))
        # 数据源配置：embedding/rerank 需要独立 model 字段（后台可配置嵌入/重排模型）
        _add_column("data_source_configs", "model", "model VARCHAR(120) NULL")

        for column, ddl in (
            ("run_once_at", "run_once_at DATETIME NULL"),
            ("timezone", "timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'"),
            ("owner_user_id", "owner_user_id INT NULL"),
            ("created_by_run_id", "created_by_run_id VARCHAR(64) NULL"),
            ("created_via", "created_via VARCHAR(24) NOT NULL DEFAULT 'admin'"),
            ("allow_code_execution", "allow_code_execution TINYINT(1) NOT NULL DEFAULT 0"),
            ("approval_policy", "approval_policy VARCHAR(24) NOT NULL DEFAULT 'full'"),
        ):
            _add_column("scheduled_jobs", column, ddl)
        _add_column("job_runs", "output_content", "output_content LONGTEXT NULL")

        # Legacy user-created Agent tasks predate ownership. Assign those to
        # the original administrator, while built-in maintenance/report jobs
        # remain unowned system infrastructure.
        connection.execute(
            text(
                f"UPDATE scheduled_jobs SET owner_user_id = {legacy_owner_sql} "
                "WHERE owner_user_id IS NULL "
                "AND (job_kind = 'agent_task' OR created_via = 'agent')"
            )
        )

        # Execution records inherit the owner of their scheduled job. Runs for
        # built-in maintenance jobs remain unowned and are never exposed by the
        # personal schedule API. This must run after the scheduled-job owner
        # column has been added for older installations.
        connection.execute(
            text(
                "UPDATE job_runs AS runs "
                "JOIN scheduled_jobs AS jobs ON jobs.job_key = runs.job_key "
                "SET runs.owner_user_id = jobs.owner_user_id "
                "WHERE runs.owner_user_id IS NULL AND jobs.owner_user_id IS NOT NULL"
            )
        )
        has_job_run_owner_index = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'job_runs' "
                "AND index_name = 'ix_job_runs_owner_user_id'"
            )
        ).scalar()
        if not has_job_run_owner_index:
            connection.execute(
                text("CREATE INDEX ix_job_runs_owner_user_id ON job_runs (owner_user_id)")
            )

        def _add_index(table: str, index_name: str, columns: str) -> None:
            exists = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
                ),
                {"t": table, "i": index_name},
            ).scalar()
            if not exists:
                connection.execute(text(f"CREATE INDEX {index_name} ON {table} ({columns})"))

        def _drop_index(table: str, index_name: str) -> None:
            exists = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
                ),
                {"t": table, "i": index_name},
            ).scalar()
            if exists:
                connection.execute(text(f"ALTER TABLE {table} DROP INDEX {index_name}"))

        def _add_unique_index(table: str, index_name: str, columns: str) -> None:
            exists = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
                ),
                {"t": table, "i": index_name},
            ).scalar()
            if not exists:
                connection.execute(
                    text(f"CREATE UNIQUE INDEX {index_name} ON {table} ({columns})")
                )

        _add_index("agent_runs", "ix_agent_runs_team_id", "team_id")
        _add_index("scheduled_jobs", "ix_scheduled_jobs_owner_user_id", "owner_user_id")
        _add_index("events", "ix_events_owner_user_id", "owner_user_id")
        for table in (
            "experience_entries",
            "experience_injections",
            "experience_revisions",
        ):
            _add_index(table, f"ix_{table}_user_id", "user_id")
        for table in (
            "watchlist_groups",
            "watchlist_items",
            "positions",
            "position_accounts",
            "position_import_batches",
            "position_holding_snapshots",
            "position_trades",
            "agent_tasks",
            "agent_reports",
            "risk_checks",
        ):
            _add_index(table, f"ix_{table}_owner_user_id", "owner_user_id")
        _add_index("watchlist_items", "ix_watchlist_items_symbol", "symbol")

        _drop_index("watchlist_groups", "name")
        _drop_index("watchlist_items", "uq_watchlist_symbol_group")
        _drop_index("position_accounts", "uq_position_account")
        _add_unique_index(
            "watchlist_groups",
            "uq_watchlist_group_owner_name",
            "owner_user_id, name",
        )
        _add_unique_index(
            "watchlist_items",
            "uq_watchlist_owner_symbol_group",
            "owner_user_id, symbol, group_id",
        )
        _add_unique_index(
            "position_accounts",
            "uq_position_account_owner",
            "owner_user_id, broker_name, account_no_masked, account_type",
        )

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
                "WHERE s.symbol = p.symbol AND s.owner_user_id = p.owner_user_id "
                "AND s.account_id IS NOT NULL "
                "ORDER BY s.id DESC LIMIT 1) "
                "WHERE p.account_id IS NULL"
            )
        )

        # === Agent 任务看板字段（v3.1） ===
        for column, ddl in (
            ("assignee_profile_id", "assignee_profile_id INT NULL"),
            ("task_title", "task_title VARCHAR(240) NULL"),
            ("board_visible", "board_visible TINYINT(1) NOT NULL DEFAULT 0"),
            ("queued_at", "queued_at DATETIME NULL"),
        ):
            _add_column("agent_runs", column, ddl)
        _add_index("agent_runs", "ix_agent_run_board", "board_visible, status, updated_at")
        _add_index("agent_runs", "ix_agent_run_agent_queue", "assignee_profile_id, status, priority, created_at")

        for column, ddl in (
            ("delegation_mode", "delegation_mode VARCHAR(24) NOT NULL DEFAULT 'execute_and_wait'"),
            ("task_extraction_mode", "task_extraction_mode VARCHAR(24) NOT NULL DEFAULT 'structured_tool'"),
            ("completion_policy", "completion_policy VARCHAR(24) NOT NULL DEFAULT 'all_required'"),
            ("child_timeout_seconds", "child_timeout_seconds INT NOT NULL DEFAULT 600"),
            ("max_delegated_tasks", "max_delegated_tasks INT NOT NULL DEFAULT 20"),
        ):
            _add_column("scheduled_jobs", column, ddl)

        for column, ddl in (
            ("leader_agent_run_id", "leader_agent_run_id VARCHAR(64) NULL"),
            ("orchestration_type", "orchestration_type VARCHAR(24) NULL"),
            ("orchestration_id", "orchestration_id VARCHAR(64) NULL"),
            ("handoff_status", "handoff_status VARCHAR(24) NOT NULL DEFAULT 'none'"),
            ("handoff_plan_json", "handoff_plan_json LONGTEXT NULL"),
            ("handoff_error", "handoff_error TEXT NULL"),
        ):
            _add_column("job_runs", column, ddl)
        _add_index("job_runs", "ix_job_run_leader", "leader_agent_run_id")
        _add_index("job_runs", "ix_job_run_orchestration", "orchestration_type, orchestration_id")

        for column, ddl in (
            ("execution_mode", "execution_mode VARCHAR(24) NOT NULL DEFAULT 'merge_by_agent'"),
            ("completion_policy", "completion_policy VARCHAR(24) NOT NULL DEFAULT 'all_required'"),
        ):
            _add_column("agent_team_runs", column, ddl)

        for column, ddl in (
            ("title", "title VARCHAR(240) NULL"),
            ("action_key", "action_key VARCHAR(120) NULL"),
            ("priority", "priority INT NOT NULL DEFAULT 100"),
            ("extraction_source", "extraction_source VARCHAR(24) NOT NULL DEFAULT 'tool_call'"),
        ):
            _add_column("agent_delegations", column, ddl)
        _add_unique_index("agent_delegations", "uq_team_action", "team_id, action_key")
        # action_key 全局唯一：防止并发 create_task_batch 产生重复
        _add_unique_index("agent_delegations", "uq_delegation_action_key", "action_key")


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
