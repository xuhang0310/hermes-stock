"""数字人 run_sql 沙箱：多用户隔离、越权绕过、只读与结果格式回归。

默认跑在 SQLite 文件库上；设置 STOCK_SQL_SANDBOX_TEST_DATABASE_URL 指向一个以 _test
结尾的独立 MySQL 库时，同一组用例再在 MySQL 上跑一遍（会清空并重建该库的表）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.agents import sql_sandbox, stock_agent
from backend.app.core.database import Base
from backend.app.core.models import (
    DailyBar,
    Document,
    Event,
    EventEvidence,
    Instrument,
    LlmModelPlatform,
    Position,
    User,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.data.user_scope import data_user_scope

SECRET_HASH = "pbkdf2$super-secret-hash"
MYSQL_TEST_URL_ENV = "STOCK_SQL_SANDBOX_TEST_DATABASE_URL"


class AgentSqlSandboxTests(unittest.TestCase):
    """全部用例只读，类级别建库造数一次。"""

    @classmethod
    def database_url(cls) -> str:
        cls._tmp = tempfile.TemporaryDirectory()
        return f"sqlite+pysqlite:///{cls._tmp.name}/sandbox.db"

    @classmethod
    def prepare_schema(cls, writer) -> None:
        Base.metadata.create_all(writer)

    @classmethod
    def setUpClass(cls) -> None:
        url = cls.database_url()
        writer = create_engine(url)
        cls.prepare_schema(writer)
        cls.seed(writer)
        writer.dispose()
        cls.engine = sql_sandbox.create_sandbox_engine_for_tests(url)
        cls._audit_dir = tempfile.TemporaryDirectory()
        cls.audit_log = Path(cls._audit_dir.name) / "agent_audit.log"
        cls._patchers = [
            patch.object(sql_sandbox, "sandbox_engine", return_value=cls.engine),
            patch.object(stock_agent, "AUDIT_LOG", cls.audit_log),
        ]
        for patcher in cls._patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in cls._patchers:
            patcher.stop()
        cls._audit_dir.cleanup()
        cls.engine.dispose()
        tmp = getattr(cls, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    @classmethod
    def seed(cls, writer) -> None:
        with sessionmaker(bind=writer)() as db:
            user_a = User(username="sql-a", display_name="A", password_hash=SECRET_HASH, role="researcher")
            user_b = User(username="sql-b", display_name="B", password_hash=SECRET_HASH, role="researcher")
            db.add_all([
                user_a,
                user_b,
                Instrument(symbol="600000.SH", name="浦发银行", market="SH", type="stock"),
                Instrument(symbol="600519.SH", name="贵州茅台", market="SH", type="stock"),
                LlmModelPlatform(name="百炼", base_url="https://example.invalid", api_key="sk-live-secret"),
            ])
            db.flush()
            cls.user_a, cls.user_b = user_a.id, user_b.id
            group_a = WatchlistGroup(owner_user_id=user_a.id, name="A 自选")
            group_b = WatchlistGroup(owner_user_id=user_b.id, name="B 自选")
            db.add_all([group_a, group_b])
            db.flush()
            event_a = Event(owner_user_id=user_a.id, event_id="ev-a", level="L0", event_type="stop", title="A 止损", source_type="rule_engine")
            event_b = Event(owner_user_id=user_b.id, event_id="ev-b", level="L0", event_type="stop", title="B 止损", source_type="rule_engine")
            event_public = Event(owner_user_id=None, event_id="ev-public", level="L2", event_type="macro", title="宏观", source_type="market")
            db.add_all([
                Position(owner_user_id=user_a.id, symbol="600000.SH", position_pct=5, quantity=100, cost_price=10, status="open"),
                Position(owner_user_id=user_b.id, symbol="600519.SH", position_pct=9, quantity=200, cost_price=1500, status="open"),
                WatchlistItem(owner_user_id=user_a.id, symbol="600519.SH", group_id=group_a.id, status="active"),
                WatchlistItem(owner_user_id=user_b.id, symbol="600000.SH", group_id=group_b.id, status="active"),
                event_a,
                event_b,
                event_public,
                Document(doc_type="news", title="公开新闻", source="cls"),
                Document(doc_type="report", title="B 的私人报告镜像", source="agent:risk"),
            ])
            db.flush()
            db.add_all([
                EventEvidence(event_id=event_a.id, evidence_type="price", content="A 证据"),
                EventEvidence(event_id=event_b.id, evidence_type="price", content="B 证据"),
                EventEvidence(event_id=event_public.id, evidence_type="news", content="公共证据"),
            ])
            db.add_all(
                DailyBar(symbol="600000.SH", trade_date=f"2026{month:02d}{day:02d}", close=10.0 + day / 10, source="tushare")
                for month in range(1, 6)
                for day in range(1, 31)
            )
            db.commit()

    def query(self, sql: str, owner: int | None = -1) -> dict:
        return stock_agent.run_sql(sql, owner_user_id=self.user_a if owner == -1 else owner)

    def values(self, sql: str, owner: int | None = -1) -> list:
        result = self.query(sql, owner)
        self.assertNotIn("error", result, result.get("error"))
        return [row[0] for row in result["rows"]]

    # ------------------------------------------------ 隔离：个人表只返回当前用户的行

    def test_personal_tables_only_return_the_callers_rows(self) -> None:
        result = self.query("SELECT symbol, quantity FROM positions")
        self.assertEqual(result["rows"], [["600000.SH", 100]])
        self.assertEqual(result["scoped_tables"], ["positions"])
        self.assertIn("只返回当前用户", result["scope_note"])
        self.assertEqual(self.values("SELECT symbol FROM positions", self.user_b), ["600519.SH"])

    def test_rewriting_survives_aliases_joins_subqueries_ctes_and_unions(self) -> None:
        bypasses = [
            "SELECT p.symbol FROM positions p",
            "SELECT p.symbol FROM positions AS p JOIN instruments i ON i.symbol = p.symbol",
            "SELECT symbol FROM (SELECT * FROM positions) AS t",
            "WITH mine AS (SELECT * FROM positions) SELECT symbol FROM mine",
            "SELECT symbol FROM positions WHERE owner_user_id <> 0 OR 1 = 1",
            "SELECT symbol FROM positions UNION SELECT symbol FROM positions WHERE quantity > 0",
            "SELECT (SELECT MAX(symbol) FROM positions) AS s",
            "SELECT symbol FROM POSITIONS",
            "SELECT symbol FROM instruments WHERE symbol IN (SELECT symbol FROM positions)",
        ]
        for sql in bypasses:
            with self.subTest(sql=sql):
                self.assertEqual(self.values(sql), ["600000.SH"])

    def test_explicitly_targeting_another_user_returns_nothing(self) -> None:
        self.assertEqual(self.values(f"SELECT symbol FROM positions WHERE owner_user_id = {self.user_b}"), [])
        self.assertEqual(self.values(f"SELECT COUNT(*) FROM watchlist_items WHERE owner_user_id = {self.user_b}"), [0])

    def test_public_events_and_their_evidence_stay_visible(self) -> None:
        self.assertEqual(sorted(self.values("SELECT event_id FROM events")), ["ev-a", "ev-public"])
        evidence = self.values("SELECT content FROM event_evidence ORDER BY id")
        self.assertEqual(evidence, ["A 证据", "公共证据"])
        self.assertIn("events 只返回当前用户的记录和公共记录", self.query("SELECT event_id FROM events")["scope_note"])
        self.assertIn("events（个人数据，只返回当前用户的记录和公共记录）", stock_agent._system_prompt(None, None))

    def test_private_report_mirrors_are_hidden_from_shared_documents(self) -> None:
        self.assertEqual(self.values("SELECT title FROM documents"), ["公开新闻"])

    def test_shared_tables_need_no_identity(self) -> None:
        self.assertEqual(self.values("SELECT COUNT(*) FROM daily_bars WHERE symbol LIKE '%SH'", None), [150])
        result = self.query("SELECT symbol FROM positions", None)
        self.assertIn("没有用户身份", result["error"])

    def test_owner_falls_back_to_data_user_scope(self) -> None:
        with data_user_scope(self.user_b):
            result = stock_agent.run_sql("SELECT symbol FROM positions")
        self.assertEqual(result["rows"], [["600519.SH"]])

    # ------------------------------------------------ 拒绝：敏感表、跨库、写入与副作用

    def test_sensitive_and_unknown_tables_are_rejected(self) -> None:
        for sql in (
            "SELECT password_hash FROM users",
            "SELECT api_key FROM llm_model_platforms",
            "SELECT * FROM notify_channels",
            "SELECT * FROM system_logs",
            "SELECT * FROM operation_logs",
            "SELECT symbol FROM daily_bars UNION SELECT password_hash FROM users",
            "SELECT * FROM daily_bars WHERE symbol IN (SELECT username FROM users)",
        ):
            with self.subTest(sql=sql):
                self.assertIn("不在可查询范围", self.query(sql)["error"])

    def test_cross_database_and_system_schema_references_are_rejected(self) -> None:
        for sql in (
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM mysql.user",
            "SELECT * FROM `hermes-stock`.positions",
        ):
            with self.subTest(sql=sql):
                self.assertIn("带库名", self.query(sql)["error"])

    def test_non_select_and_side_effects_are_rejected(self) -> None:
        cases = {
            "DELETE FROM positions": "只允许 SELECT",
            "UPDATE positions SET quantity = 0": "只允许 SELECT",
            "INSERT INTO positions (symbol, position_pct) SELECT symbol, 1 FROM instruments": "只允许 SELECT",
            "DROP TABLE positions": "只允许 SELECT",
            "SHOW TABLES": "只允许 SELECT",
            "EXPLAIN SELECT * FROM positions": "只允许 SELECT",
            "SELECT 1; DELETE FROM positions": "单条",
            "SELECT 1 INTO @x": "INTO",
            "SELECT * FROM positions FOR UPDATE": "加锁",
            "SELECT @@version": "系统变量",
            "SELECT @a := 1": "赋值",
            "SELECT @a": "用户变量",
            "SELECT SLEEP(10)": "SLEEP",
            "SELECT BENCHMARK(1000000, MD5('a'))": "BENCHMARK",
            "SELECT LOAD_FILE('/etc/passwd')": "LOAD_FILE",
            "SELECT CURRENT_USER()": "CURRENT_USER",
            "SELECT sys.format_bytes(1)": "带库名的函数",
            "WITH positions AS (SELECT 1 AS symbol) SELECT symbol FROM positions": "同名",
            "SELECT * FROM JSON_TABLE('[1]', '$[*]' COLUMNS(a INT PATH '$')) jt": "表函数",
            "SELECT 1 FROM DUAL": "DUAL",
            "": "为空",
        }
        for sql, message in cases.items():
            with self.subTest(sql=sql):
                self.assertIn(message, self.query(sql)["error"])

    def test_stored_functions_are_rejected(self) -> None:
        with self.assertRaisesRegex(sql_sandbox.SqlRejected, "存储函数"):
            sql_sandbox.prepare_query(
                "SELECT leak_all() FROM instruments",
                owner_user_id=self.user_a,
                max_rows=10,
                known_routines={"leak_all"},
            )

    def test_mysql_executable_comments_are_not_executed(self) -> None:
        result = self.query("SELECT symbol FROM instruments /*! UNION SELECT password_hash FROM users */")
        self.assertNotIn("error", result)
        self.assertNotIn(SECRET_HASH, str(result["rows"]))
        self.assertNotIn("/*", stock_agent.sql_sandbox.prepare_query(
            "SELECT 1 /* note */ FROM instruments", owner_user_id=None, max_rows=1,
        ).sql)

    def test_database_session_is_read_only_even_without_the_parser(self) -> None:
        with self.engine.connect() as conn, self.assertRaises(DBAPIError):
            conn.execute(text("DELETE FROM positions"))

    # ------------------------------------------------ 结果格式与上限

    def test_results_are_capped_and_marked_truncated(self) -> None:
        result = self.query("SELECT trade_date FROM daily_bars")
        self.assertEqual(result["row_count"], stock_agent.MAX_ROWS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["columns"], ["trade_date"])
        self.assertNotIn("executed_sql", result)
        self.assertIn("已截断", stock_agent._observation_preview(result))

    def test_explicit_limit_is_respected(self) -> None:
        result = self.query("SELECT trade_date FROM daily_bars ORDER BY trade_date LIMIT 3")
        self.assertEqual(result["row_count"], 3)
        self.assertFalse(result["truncated"])

    def test_execution_errors_are_reported_without_crashing(self) -> None:
        result = self.query("SELECT no_such_column FROM daily_bars")
        self.assertIn("SQL 执行失败", result["error"])

    def test_audit_records_user_and_the_scoped_sql_actually_executed(self) -> None:
        marker = "audit_marker_7731"
        self.query(f"SELECT symbol AS {marker} FROM positions")
        self.query(f"SELECT password_hash AS {marker} FROM users")
        records = [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
            if marker in line
        ]
        executed, rejected = records[-2], records[-1]
        self.assertEqual(executed["user_id"], self.user_a)
        self.assertIn(f"owner_user_id = {self.user_a}", executed["executed_sql"])
        self.assertEqual(executed["scoped_tables"], ["positions"])
        self.assertIn("不在可查询范围", rejected["rejected"])

    def test_validate_sql_is_a_static_check(self) -> None:
        self.assertIsNone(stock_agent.validate_sql("SELECT symbol FROM daily_bars"))
        self.assertIn("不在可查询范围", stock_agent.validate_sql("SELECT * FROM users"))

    # ------------------------------------------------ 工具声明与表结构字典

    def test_tool_schema_and_get_schema_follow_the_sandbox_policy(self) -> None:
        self.assertIn("run_sql", {item["function"]["name"] for item in stock_agent.TOOLS})
        listing = {item["name"]: item["scope"] for item in stock_agent.get_schema()["tables"]}
        self.assertEqual(listing["positions"], "personal")
        self.assertEqual(listing["daily_bars"], "shared")
        self.assertTrue(all(sql_sandbox.is_queryable(name) for name in listing))
        self.assertIn("不开放查询", stock_agent.get_schema("users")["error"])
        self.assertEqual(stock_agent.get_schema("positions")["scope"], "personal")
        undocumented = stock_agent.get_schema("paper_orders")
        self.assertIn("realized_pnl", undocumented["fields"])
        for table, meta in stock_agent.SCHEMA_DICTIONARY.items():
            columns = {column.name for column in Base.metadata.tables[table].columns}
            self.assertLessEqual(set(meta["fields"]), columns, table)

    def test_research_prompt_points_to_run_sql_and_marks_personal_tables(self) -> None:
        prompt = stock_agent._system_prompt(None, None)
        self.assertIn("run_sql", prompt)
        self.assertIn("positions（个人数据，只返回当前用户的记录）", prompt)
        self.assertNotIn("system_logs", prompt)

    def test_policy_only_names_real_tables(self) -> None:
        known = set(Base.metadata.tables)
        policy = {
            *sql_sandbox.SHARED_TABLES,
            *sql_sandbox.SHARED_ROW_FILTERS,
            *sql_sandbox.PERSONAL_TABLES,
            *sql_sandbox.PERSONAL_CHILD_TABLES,
        }
        self.assertLessEqual(policy, known)
        for table, column in sql_sandbox.PERSONAL_TABLES.items():
            self.assertIn(column, Base.metadata.tables[table].columns, table)


class MysqlAgentSqlSandboxTests(AgentSqlSandboxTests):
    """同一组用例跑在真实 MySQL 上：验证改写后的 SQL 方言、只读会话、超时与存储函数拦截。"""

    @classmethod
    def database_url(cls) -> str:
        raw = os.getenv(MYSQL_TEST_URL_ENV, "").strip()
        if not raw:
            raise unittest.SkipTest(f"未配置 {MYSQL_TEST_URL_ENV}，跳过沙箱 MySQL 测试")
        url = make_url(raw)
        if url.drivername != "mysql+pymysql" or not (url.database or "").endswith(("_test", "-test")):
            raise RuntimeError(f"{MYSQL_TEST_URL_ENV} 必须是 mysql+pymysql，且库名以 _test 或 -test 结尾")
        return raw

    @classmethod
    def prepare_schema(cls, writer) -> None:
        Base.metadata.drop_all(writer)
        Base.metadata.create_all(writer)
        with writer.begin() as conn:
            conn.exec_driver_sql("DROP FUNCTION IF EXISTS leak_user_count")
            conn.exec_driver_sql(
                "CREATE FUNCTION leak_user_count() RETURNS INT READS SQL DATA "
                "RETURN (SELECT COUNT(*) FROM users)"
            )

    def test_real_stored_function_is_rejected(self) -> None:
        self.assertIn("存储函数", self.query("SELECT leak_user_count() AS n")["error"])

    def test_session_is_read_only_with_statement_timeout(self) -> None:
        with self.engine.connect() as conn:
            row = conn.exec_driver_sql("SELECT @@transaction_read_only, @@max_execution_time").one()
        self.assertEqual(tuple(row), (1, sql_sandbox.STATEMENT_TIMEOUT_MS))


class AgentScopeRunSqlOwnerTests(unittest.TestCase):
    def test_agentscope_dispatch_passes_the_run_owner(self) -> None:
        try:
            from backend.app.agent_runtime import agentscope_engine
        except ImportError as exc:  # pragma: no cover - 依赖缺失时跳过
            self.skipTest(f"agentscope_engine 不可导入：{exc}")
        payload = {"user_id": 42, "chat_mode": "research"}
        with (
            patch.object(agentscope_engine.transaction_state_service, "before_tool", return_value=({"sql": "SELECT 1"}, None)),
            patch.object(stock_agent, "run_sql", return_value={"rows": []}) as run_sql,
        ):
            agentscope_engine._execute_tool("run_sql", {"sql": "SELECT 1"}, payload, set())
        run_sql.assert_called_once_with("SELECT 1", owner_user_id=42)


if __name__ == "__main__":
    unittest.main()
